"""CRUD по таблице accounts. ARCHITECTURE.md §4.1, §5.1, §6.3, §6.4, §6.5."""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Account, AccountStatus, CampaignType


# Warmup-таблица из §5.1 «Warmup для новых аккаунтов».
# Считается от accounts.created_at.
def warmup_age_limits(
    *, hours_since_created: float, dm_warm: int, invite_warm: int,
    dm_fresh: int, invite_fresh: int,
) -> tuple[int, int]:
    """Возвращает (dm_limit, invite_limit) для warmup-аккаунта по таблице §5.1."""
    if hours_since_created < 12:
        return 0, 0
    if hours_since_created < 24:
        return 3, 0
    if hours_since_created < 48:
        return dm_fresh, invite_fresh
    return dm_warm, invite_warm


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


# ---------------------------------------------------------------------------
# State transitions. ARCHITECTURE.md §5.1 (диаграмма), §6.3, §6.4, §6.5.
# ---------------------------------------------------------------------------


async def set_pause(
    session: AsyncSession, *, account_id: int, unlock_at: datetime
) -> None:
    """FloodWait (§6.3): пауза до unlock_at, статус 'pause'."""
    await session.execute(
        update(Account)
        .where(Account.id == account_id)
        .values(status=AccountStatus.pause, spam_unlock_at=unlock_at)
    )


async def set_spam_blocked(
    session: AsyncSession,
    *,
    account_id: int,
    unlock_at: datetime,
    limit_reduce_until: datetime,
) -> None:
    """PeerFlood (§6.4): 12ч карантин + 75% лимит на 7 дней.

    COALESCE для limit_reduced_until — если адаптивное снижение уже действует,
    повторные PeerFlood его не продлевают (см. §6.4 комментарий).
    """
    await session.execute(
        update(Account)
        .where(Account.id == account_id)
        .values(
            status=AccountStatus.spam_blocked,
            spam_unlock_at=unlock_at,
            limit_reduced_until=func.coalesce(
                Account.limit_reduced_until, limit_reduce_until
            ),
        )
    )


async def set_active_no_limits(session: AsyncSession, *, account_id: int) -> None:
    """SpamBot ok (§6.5): возврат к 100% лимиту + active + сброс таймера паузы."""
    await session.execute(
        update(Account)
        .where(Account.id == account_id)
        .values(
            status=AccountStatus.active,
            spam_unlock_at=None,
            limit_reduced_until=None,
        )
    )


async def set_dead(session: AsyncSession, *, account_id: int) -> None:
    """Session error / реавторизация требуется (§5.1 dead)."""
    await session.execute(
        update(Account)
        .where(Account.id == account_id)
        .values(status=AccountStatus.dead)
    )


async def set_active(session: AsyncSession, *, account_id: int) -> None:
    await session.execute(
        update(Account)
        .where(Account.id == account_id)
        .values(status=AccountStatus.active, spam_unlock_at=None)
    )


# ---------------------------------------------------------------------------
# Лимиты и счётчики. §5.1 «Дневные лимиты», §6.5.
# ---------------------------------------------------------------------------


def is_in_warmup(account: Account, *, now: datetime | None = None) -> bool:
    if account.warmup_until is None:
        return False
    now = now or datetime.now(timezone.utc)
    return account.warmup_until > now


def is_limit_reduced(account: Account, *, now: datetime | None = None) -> bool:
    if account.limit_reduced_until is None:
        return False
    now = now or datetime.now(timezone.utc)
    return account.limit_reduced_until > now


def effective_daily_limits(
    account: Account,
    *,
    dm_warm: int,
    invite_warm: int,
    dm_fresh: int,
    invite_fresh: int,
    reduction_ratio: float = 0.75,
    now: datetime | None = None,
) -> tuple[int, int]:
    """Возвращает (max_dm, max_invite) с учётом warmup + адаптивного снижения.

    Логика §5.1:
      1. Базовые лимиты warm/fresh выбираются из warmup-таблицы по возрасту.
      2. Если limit_reduced_until активен → умножаем на reduction_ratio (75%).
    """
    now = now or datetime.now(timezone.utc)
    hours_since_created = (
        (now - account.created_at).total_seconds() / 3600
        if account.created_at is not None
        else float("inf")
    )

    if is_in_warmup(account, now=now):
        dm_max, invite_max = warmup_age_limits(
            hours_since_created=hours_since_created,
            dm_warm=dm_warm,
            invite_warm=invite_warm,
            dm_fresh=dm_fresh,
            invite_fresh=invite_fresh,
        )
    else:
        dm_max, invite_max = dm_warm, invite_warm

    if is_limit_reduced(account, now=now):
        dm_max = math.ceil(dm_max * reduction_ratio)
        invite_max = math.ceil(invite_max * reduction_ratio)

    return dm_max, invite_max


def can_send_today(
    account: Account,
    *,
    action_type: CampaignType,
    dm_warm: int,
    invite_warm: int,
    dm_fresh: int,
    invite_fresh: int,
    reduction_ratio: float = 0.75,
    now: datetime | None = None,
) -> bool:
    """§6.1 шаг 4. Может ли аккаунт сейчас выполнить ещё одно действие данного типа?"""
    if account.status in (AccountStatus.disabled, AccountStatus.dead):
        return False
    dm_max, invite_max = effective_daily_limits(
        account,
        dm_warm=dm_warm,
        invite_warm=invite_warm,
        dm_fresh=dm_fresh,
        invite_fresh=invite_fresh,
        reduction_ratio=reduction_ratio,
        now=now,
    )
    if action_type == CampaignType.message:
        return account.daily_sent < dm_max
    return account.daily_invited < invite_max


async def increment_counter(
    session: AsyncSession,
    *,
    account_id: int,
    action_type: CampaignType,
) -> None:
    if action_type == CampaignType.message:
        await session.execute(
            update(Account)
            .where(Account.id == account_id)
            .values(
                daily_sent=Account.daily_sent + 1,
                last_used_at=datetime.now(timezone.utc),
            )
        )
    else:
        await session.execute(
            update(Account)
            .where(Account.id == account_id)
            .values(
                daily_invited=Account.daily_invited + 1,
                last_used_at=datetime.now(timezone.utc),
            )
        )


async def reset_daily_counters(session: AsyncSession) -> int:
    """Cron 00:00 UTC. §5.1 «Дневные лимиты»."""
    result = await session.execute(
        update(Account).values(
            daily_sent=0,
            daily_invited=0,
            last_reset_at=datetime.now(timezone.utc),
        )
    )
    return result.rowcount or 0


# ---------------------------------------------------------------------------
# Списки для worker pool и scheduler.
# ---------------------------------------------------------------------------


async def list_for_worker_pool(session: AsyncSession) -> list[Account]:
    """Аккаунты которые нужно поднимать как воркеры при старте.

    §18.4 шаг 4: фильтр disabled/dead. paused/spam_blocked попадают —
    воркеры сами в первом цикле спят пока spam_unlock_at не пройдёт.
    """
    result = await session.execute(
        select(Account)
        .where(Account.status.notin_([AccountStatus.disabled, AccountStatus.dead]))
        .order_by(Account.id)
    )
    return list(result.scalars().all())


async def list_for_spamcheck(session: AsyncSession) -> list[Account]:
    """Аккаунты которые опрашиваются SpamBot'ом. §7.1.

    Для disabled/dead пропускается. Для paused/spam_blocked — опрос идёт,
    чтобы поймать `no_limits` досрочно (§6.5).
    """
    result = await session.execute(
        select(Account)
        .where(Account.status.notin_([AccountStatus.disabled, AccountStatus.dead]))
        .order_by(Account.id)
    )
    return list(result.scalars().all())


async def list_active_during_quiet_start(session: AsyncSession) -> list[Account]:
    """Аккаунты в статусе active — при входе в quiet hours их переводим в pause."""
    result = await session.execute(
        select(Account).where(Account.status == AccountStatus.active)
    )
    return list(result.scalars().all())


async def has_recovered_account(session: AsyncSession) -> bool:
    """Есть ли хотя бы один рабочий аккаунт, способный слать сейчас (§5.3 resume):
    status='active' (т.е. не spam_blocked/pause/warmup/dead/disabled)."""
    result = await session.execute(
        select(Account.id).where(Account.status == AccountStatus.active).limit(1)
    )
    return result.first() is not None
