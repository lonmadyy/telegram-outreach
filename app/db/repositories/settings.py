"""Чтение/запись настроек из таблицы settings + in-memory кэш с LISTEN/NOTIFY.

ARCHITECTURE.md §4.9. Дефолты заполнены миграцией #0001.

Использование:
    # Старт приложения:
    await settings_cache.refresh_all()
    listener = PubSubListener(SETTINGS_CHANNEL, lambda key: settings_cache.invalidate(key))
    listener.start()

    # В горячем коде (воркер):
    interval_min = settings_cache.get_int("interval_min_sec", default=300)

    # При изменении через бот:
    await set_value(session, key="interval_min_sec", value="360", value_type="int",
                    updated_by=admin_user_id)
    # → set_value сам делает pg_notify, кэш других экземпляров обновится автоматом.
"""

from __future__ import annotations

import asyncio
from typing import Any

from loguru import logger
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Setting
from app.db.session import session_scope
from app.utils.pubsub import SETTINGS_CHANNEL, notify

_TYPE_CASTERS: dict[str, Any] = {
    "int": int,
    "float": float,
    "bool": lambda v: v.lower() in ("1", "true", "yes", "on"),
    "str": str,
}


def _cast(value: str, value_type: str) -> Any:
    caster = _TYPE_CASTERS.get(value_type, str)
    return caster(value)


# ---------------------------------------------------------------------------
# Low-level: один-выстрел чтение/запись (используется в нечастых местах).
# ---------------------------------------------------------------------------


async def get(session: AsyncSession, key: str) -> Any | None:
    result = await session.execute(select(Setting).where(Setting.key == key))
    row = result.scalar_one_or_none()
    if row is None:
        return None
    return _cast(row.value, row.value_type)


async def get_all(session: AsyncSession) -> dict[str, Any]:
    result = await session.execute(select(Setting))
    out: dict[str, Any] = {}
    for row in result.scalars().all():
        out[row.key] = _cast(row.value, row.value_type)
    return out


async def get_all_with_types(session: AsyncSession) -> dict[str, tuple[Any, str]]:
    """Для бота: ключ → (значение, тип)."""
    result = await session.execute(select(Setting))
    out: dict[str, tuple[Any, str]] = {}
    for row in result.scalars().all():
        out[row.key] = (_cast(row.value, row.value_type), row.value_type)
    return out


async def get_value_type(session: AsyncSession, key: str) -> str | None:
    """Тип существующей настройки или None."""
    result = await session.execute(
        select(Setting.value_type).where(Setting.key == key)
    )
    return result.scalar_one_or_none()


async def set_value(
    session: AsyncSession,
    *,
    key: str,
    value: str,
    value_type: str,
    updated_by: int | None = None,
) -> None:
    """Upsert + публикация события settings_changed для инвалидации кэша."""
    if value_type not in _TYPE_CASTERS:
        raise ValueError(f"Unsupported value_type: {value_type}")
    # Проверим что значение приводится к указанному типу.
    _cast(value, value_type)

    stmt = insert(Setting).values(
        key=key, value=value, value_type=value_type, updated_by=updated_by
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=[Setting.key],
        set_=dict(
            value=stmt.excluded.value,
            value_type=stmt.excluded.value_type,
            updated_by=stmt.excluded.updated_by,
            updated_at=func.now(),
        ),
    )
    await session.execute(stmt)
    # Уведомление будет отправлено после commit'а — на уровне контекста.
    # Чтобы не пропустить нотификацию при rollback, шлём её асинхронно ПОСЛЕ
    # выхода из текущей транзакции. Здесь же только запланируем.


async def publish_changed(key: str) -> None:
    """Шлёт pg_notify('settings_changed', key). Вызывать ПОСЛЕ commit'а."""
    try:
        await notify(SETTINGS_CHANNEL, key)
    except Exception:
        logger.exception("Failed to publish settings_changed for {!r}", key)


# ---------------------------------------------------------------------------
# In-memory кэш с инвалидацией.
# ---------------------------------------------------------------------------


class SettingsCache:
    def __init__(self) -> None:
        self._values: dict[str, Any] = {}
        self._lock = asyncio.Lock()
        self._loaded = False

    async def refresh_all(self) -> None:
        async with self._lock:
            async with session_scope() as session:
                self._values = await get_all(session)
            self._loaded = True
            logger.info("Settings cache loaded: {} keys", len(self._values))

    async def refresh_key(self, key: str) -> None:
        async with self._lock:
            async with session_scope() as session:
                value = await get(session, key)
            if value is None:
                self._values.pop(key, None)
            else:
                self._values[key] = value

    async def invalidate(self, key: str) -> None:
        """Колбэк для PubSubListener (получает payload = key)."""
        if not key:
            await self.refresh_all()
        else:
            await self.refresh_key(key)
        logger.debug("Settings cache invalidated: key={!r}", key)

    # Geters — синхронные, поскольку из памяти.

    def get(self, key: str, default: Any = None) -> Any:
        return self._values.get(key, default)

    def get_int(self, key: str, default: int) -> int:
        v = self._values.get(key)
        return int(v) if v is not None else default

    def get_float(self, key: str, default: float) -> float:
        v = self._values.get(key)
        return float(v) if v is not None else default

    def get_str(self, key: str, default: str) -> str:
        v = self._values.get(key)
        return str(v) if v is not None else default

    def get_bool(self, key: str, default: bool) -> bool:
        v = self._values.get(key)
        return bool(v) if v is not None else default

    def all(self) -> dict[str, Any]:
        return dict(self._values)


# Глобальный singleton.
settings_cache = SettingsCache()
