"""Авто-reconnect воркера (§16): `_ensure_alive` переподключается при обрыве.

Регрессия с прода (кампания #8): Telethon, исчерпав `connection_retries`,
оставался в состоянии disconnected, и воркер бесконечно падал в
`ConnectionError: Cannot send requests while disconnected` (тысячи ERROR в логах,
нулевая полезная работа). `_ensure_alive` восстанавливает соединение перед тиком.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from app.telegram.worker import WorkerAccount


def _worker(*, connected: bool, connect_raises: bool = False) -> WorkerAccount:
    client = MagicMock()
    client.is_connected = MagicMock(return_value=connected)
    client.connect = AsyncMock(
        side_effect=ConnectionError("net down") if connect_raises else None
    )
    return WorkerAccount(account_id=1, client=client)


async def test_ensure_alive_noop_when_connected() -> None:
    """Соединение живо → connect() не вызывается, возвращает True."""
    w = _worker(connected=True)
    assert await w._ensure_alive() is True
    w.client.connect.assert_not_called()


async def test_ensure_alive_reconnects_when_disconnected() -> None:
    """Соединение отвалилось → connect() вызывается, возвращает True."""
    w = _worker(connected=False)
    assert await w._ensure_alive() is True
    w.client.connect.assert_awaited_once()


async def test_ensure_alive_false_on_reconnect_failure() -> None:
    """Reconnect не удался (сеть лежит) → возвращает False без исключения."""
    w = _worker(connected=False, connect_raises=True)
    assert await w._ensure_alive() is False
    w.client.connect.assert_awaited_once()
