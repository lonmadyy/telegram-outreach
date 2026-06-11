"""Запись лог-событий в БД. ARCHITECTURE.md §4.8, §15.1."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import func, select
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


async def flooded_account_ids_since(
    session: AsyncSession, *, since: datetime
) -> set[int]:
    """DISTINCT account_id с событием peer_flood начиная с `since` (§5.3).

    Источник окна флуда для антифлуд-джобы. Использует индексы idx_logs_event/ts.
    """
    result = await session.execute(
        select(Log.account_id)
        .where(Log.event_type == "peer_flood")
        .where(Log.ts >= since)
        .where(Log.account_id.is_not(None))
        .distinct()
    )
    return {row[0] for row in result.all()}


async def exists_peer_flood_since(
    session: AsyncSession, *, since: datetime
) -> bool:
    """Был ли хотя бы один peer_flood начиная с `since` (§5.3, адаптивный интервал)."""
    result = await session.execute(
        select(Log.id)
        .where(Log.event_type == "peer_flood")
        .where(Log.ts >= since)
        .limit(1)
    )
    return result.first() is not None


async def exists_peer_flood_for_account_since(
    session: AsyncSession, *, account_id: int, since: datetime
) -> bool:
    """Был ли peer_flood у КОНКРЕТНОГО аккаунта начиная с `since`.

    Для кулдауна PeerFlood-карантина (§6.5): SpamBot не отражает PeerFlood-лимит
    и отвечает no_limits — без кулдауна карантин снимался в ту же секунду."""
    result = await session.execute(
        select(Log.id)
        .where(Log.event_type == "peer_flood")
        .where(Log.account_id == account_id)
        .where(Log.ts >= since)
        .limit(1)
    )
    return result.first() is not None


async def flood_wait_counts_since(
    session: AsyncSession, *, since: datetime
) -> dict[int, int]:
    """Сколько событий `flood_wait` у каждого аккаунта начиная с `since`
    (для /floodwait и сводки /status): {account_id: count}. Read-only по таблице
    logs — без отдельного поля-счётчика в accounts (§10.2)."""
    result = await session.execute(
        select(Log.account_id, func.count())
        .where(Log.event_type == "flood_wait")
        .where(Log.ts >= since)
        .where(Log.account_id.is_not(None))
        .group_by(Log.account_id)
    )
    return {row[0]: row[1] for row in result.all()}
