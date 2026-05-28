"""Чтение/запись настроек из таблицы settings. ARCHITECTURE.md §4.9.

Полноценный кэш с инвалидацией через LISTEN/NOTIFY придёт в MVP-3
(app/utils/pubsub.py). В MVP-1 — простой fetch при каждом обращении.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Setting

_TYPE_CASTERS: dict[str, Any] = {
    "int": int,
    "float": float,
    "bool": lambda v: v.lower() in ("1", "true", "yes", "on"),
    "str": str,
}


async def get(session: AsyncSession, key: str) -> Any | None:
    result = await session.execute(select(Setting).where(Setting.key == key))
    row = result.scalar_one_or_none()
    if row is None:
        return None
    caster = _TYPE_CASTERS.get(row.value_type, str)
    return caster(row.value)


async def get_all(session: AsyncSession) -> dict[str, Any]:
    result = await session.execute(select(Setting))
    out: dict[str, Any] = {}
    for row in result.scalars().all():
        caster = _TYPE_CASTERS.get(row.value_type, str)
        out[row.key] = caster(row.value)
    return out


async def set_value(
    session: AsyncSession,
    *,
    key: str,
    value: str,
    value_type: str,
    updated_by: int | None = None,
) -> None:
    """Upsert настройки."""
    if value_type not in _TYPE_CASTERS:
        raise ValueError(f"Unsupported value_type: {value_type}")

    stmt = insert(Setting).values(
        key=key,
        value=value,
        value_type=value_type,
        updated_by=updated_by,
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
