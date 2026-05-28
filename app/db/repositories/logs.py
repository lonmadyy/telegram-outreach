"""Запись лог-событий в БД. ARCHITECTURE.md §4.8, §15.1."""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Log, LogLevel


async def log_event(
    session: AsyncSession,
    *,
    level: LogLevel | str,
    event_type: str,
    message: str,
    account_id: int | None = None,
    campaign_id: int | None = None,
    task_id: int | None = None,
    payload: dict[str, Any] | None = None,
) -> Log:
    if isinstance(level, str):
        level = LogLevel(level)
    entry = Log(
        level=level,
        event_type=event_type,
        message=message,
        account_id=account_id,
        campaign_id=campaign_id,
        task_id=task_id,
        payload=payload or {},
    )
    session.add(entry)
    await session.flush()
    return entry
