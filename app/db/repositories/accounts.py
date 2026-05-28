"""CRUD по таблице accounts. ARCHITECTURE.md §4.1."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Account, AccountStatus


async def list_accounts(session: AsyncSession) -> list[Account]:
    result = await session.execute(select(Account).order_by(Account.id))
    return list(result.scalars().all())


async def get_by_id(session: AsyncSession, account_id: int) -> Account | None:
    return await session.get(Account, account_id)


async def get_by_phone(session: AsyncSession, phone: str) -> Account | None:
    result = await session.execute(select(Account).where(Account.phone == phone))
    return result.scalar_one_or_none()


async def create_account(
    session: AsyncSession,
    *,
    phone: str,
    session_path: str,
    tg_user_id: int | None = None,
    username: str | None = None,
    first_name: str | None = None,
    proxy_url: str | None = None,
    warmup_hours: int = 48,
) -> Account:
    """Создаёт новый аккаунт в статусе warmup. §5.1, §4.1."""
    warmup_until = datetime.now(timezone.utc) + timedelta(hours=warmup_hours)
    account = Account(
        phone=phone,
        session_path=session_path,
        tg_user_id=tg_user_id,
        username=username,
        first_name=first_name,
        proxy_url=proxy_url,
        status=AccountStatus.warmup,
        warmup_until=warmup_until,
    )
    session.add(account)
    await session.flush()
    return account


async def delete_account(session: AsyncSession, account_id: int) -> bool:
    account = await session.get(Account, account_id)
    if account is None:
        return False
    await session.delete(account)
    return True
