"""Postgres LISTEN/NOTIFY обёртка через asyncpg. ARCHITECTURE.md §4.9.

При изменении настроек через бот (`/set key value`) выполняется
`pg_notify('settings_changed', key)`. Приложение слушает этот канал и
обновляет in-memory кэш настроек, чтобы лимиты/интервалы менялись «на лету»
без рестарта.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

import asyncpg
from loguru import logger

from app.config import settings

SETTINGS_CHANNEL = "settings_changed"

# Тип колбэка: получает payload (строку из NOTIFY).
NotifyCallback = Callable[[str], Awaitable[None]]


class PubSubListener:
    """Долгоживущий LISTEN на канал. Переподключается при разрыве соединения."""

    def __init__(self, channel: str, callback: NotifyCallback) -> None:
        self.channel = channel
        self.callback = callback
        self._task: asyncio.Task | None = None
        self._shutdown = asyncio.Event()

    async def _on_notify(
        self,
        connection: asyncpg.Connection,
        pid: int,
        channel: str,
        payload: str,
    ) -> None:
        try:
            await self.callback(payload)
        except Exception:
            logger.exception(
                "pubsub callback failed: channel={}, payload={!r}", channel, payload
            )

    async def _run(self) -> None:
        backoff = 1.0
        while not self._shutdown.is_set():
            try:
                conn = await asyncpg.connect(dsn=settings.database_dsn)
                await conn.add_listener(self.channel, self._on_notify)
                logger.info("Listening on Postgres channel {!r}", self.channel)
                backoff = 1.0
                try:
                    while not self._shutdown.is_set():
                        await asyncio.sleep(60)
                finally:
                    try:
                        await conn.remove_listener(self.channel, self._on_notify)
                    except Exception:
                        pass
                    await conn.close()
            except Exception as e:
                if self._shutdown.is_set():
                    return
                logger.warning(
                    "pubsub listener crashed: {}; reconnect in {}s", e, backoff
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60.0)

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(
                self._run(), name=f"pubsub-listener-{self.channel}"
            )

    async def stop(self) -> None:
        self._shutdown.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass


async def notify(channel: str, payload: str = "") -> None:
    """Однократная отправка NOTIFY (для использования из bot-handlers etc.)."""
    conn = await asyncpg.connect(dsn=settings.database_dsn)
    try:
        # asyncpg парсит ' внутри строки, поэтому используем параметры.
        await conn.execute("SELECT pg_notify($1, $2)", channel, payload)
    finally:
        await conn.close()
