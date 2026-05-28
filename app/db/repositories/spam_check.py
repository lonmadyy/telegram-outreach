"""Записи о проверках @SpamBot. ARCHITECTURE.md §4.7, §7.3."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import SpamCheckHistory


async def get_last(
    session: AsyncSession, *, account_id: int
) -> SpamCheckHistory | None:
    result = await session.execute(
        select(SpamCheckHistory)
        .where(SpamCheckHistory.account_id == account_id)
        .order_by(desc(SpamCheckHistory.checked_at))
        .limit(1)
    )
    return result.scalar_one_or_none()


async def get_last_parsed_status(
    session: AsyncSession, *, account_id: int
) -> str | None:
    """Возвращает строку parsed_status последней записи или None если не было проверок."""
    last = await get_last(session, account_id=account_id)
    return last.parsed_status if last is not None else None


async def save_check(
    session: AsyncSession,
    *,
    account_id: int,
    raw_response: str,
    parsed_status: str,
    unlock_at: datetime | None = None,
) -> SpamCheckHistory:
    """Сохраняет запись только при смене статуса (§4.7 «В обычном режиме»).

    Если предыдущая проверка дала тот же статус — возвращаем её без новой записи.
    Это позволяет отслеживать историю изменений, не разрастаясь.
    """
    prev = await get_last(session, account_id=account_id)
    if prev is not None and prev.parsed_status == parsed_status:
        return prev

    entry = SpamCheckHistory(
        account_id=account_id,
        raw_response=raw_response,
        parsed_status=parsed_status,
        unlock_at=unlock_at,
    )
    session.add(entry)
    await session.flush()
    return entry


async def save_check_always(
    session: AsyncSession,
    *,
    account_id: int,
    raw_response: str,
    parsed_status: str,
    unlock_at: datetime | None = None,
) -> SpamCheckHistory:
    """Запись без дедупликации — для unknown-кейсов чтобы сохранять сырой текст."""
    entry = SpamCheckHistory(
        account_id=account_id,
        raw_response=raw_response,
        parsed_status=parsed_status,
        unlock_at=unlock_at,
    )
    session.add(entry)
    await session.flush()
    return entry
