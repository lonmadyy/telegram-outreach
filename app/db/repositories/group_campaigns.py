"""CRUD групповых рассылок. ARCHITECTURE.md §22.8."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, case, delete, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    Account,
    AccountStatus,
    GroupCampaign,
    GroupCampaignAccount,
    GroupPost,
    GroupTarget,
    Template,
)

RUNNING = "running"
PAUSED = "paused"
STOPPED = "stopped"
PROBLEM_STATES = ("manual", "excluded")
# Аккаунт выбыл из рассылки насовсем (§22.7). Выключенная роль — НЕ выбыл.
GONE_ACCOUNT_STATUSES = (AccountStatus.dead, AccountStatus.disabled)


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Рассылки
# ---------------------------------------------------------------------------


async def create_campaign(
    session: AsyncSession,
    *,
    name: str,
    template_id: int,
    distribution: str,
    period_min: int,
    period_max: int,
    gap_min_sec: int,
    gap_max_sec: int,
    window_start: str | None,
    window_end: str | None,
    created_by_user_id: int | None,
) -> GroupCampaign:
    campaign = GroupCampaign(
        name=name,
        template_id=template_id,
        status=RUNNING,
        distribution=distribution,
        period_min=period_min,
        period_max=period_max,
        gap_min_sec=gap_min_sec,
        gap_max_sec=gap_max_sec,
        window_start=window_start,
        window_end=window_end,
        created_by_user_id=created_by_user_id,
        started_at=_now(),
    )
    session.add(campaign)
    await session.flush()
    return campaign


async def get_campaign(session: AsyncSession, campaign_id: int) -> GroupCampaign | None:
    return await session.get(GroupCampaign, campaign_id)


async def list_campaigns(
    session: AsyncSession, *, statuses: tuple[str, ...] | None = None, limit: int = 30
) -> list[GroupCampaign]:
    stmt = select(GroupCampaign).order_by(GroupCampaign.id.desc()).limit(limit)
    if statuses:
        stmt = stmt.where(GroupCampaign.status.in_(statuses))
    return list((await session.execute(stmt)).scalars().all())


async def set_status(session: AsyncSession, campaign_id: int, status: str) -> None:
    values: dict = {"status": status}
    if status == STOPPED:
        values["stopped_at"] = _now()
    await session.execute(
        update(GroupCampaign).where(GroupCampaign.id == campaign_id).values(**values)
    )


async def update_intervals(
    session: AsyncSession,
    campaign_id: int,
    *,
    period_min: int,
    period_max: int,
    gap_min_sec: int,
    gap_max_sec: int,
    window_start: str | None,
    window_end: str | None,
) -> None:
    await session.execute(
        update(GroupCampaign)
        .where(GroupCampaign.id == campaign_id)
        .values(
            period_min=period_min,
            period_max=period_max,
            gap_min_sec=gap_min_sec,
            gap_max_sec=gap_max_sec,
            window_start=window_start,
            window_end=window_end,
        )
    )


async def set_template(session: AsyncSession, campaign_id: int, template_id: int) -> None:
    await session.execute(
        update(GroupCampaign)
        .where(GroupCampaign.id == campaign_id)
        .values(template_id=template_id)
    )


async def template_usage(session: AsyncSession, template_id: int) -> int:
    """Сколько рассылок ссылаются на шаблон (FK RESTRICT не даст удалить)."""
    result = await session.execute(
        select(func.count()).where(GroupCampaign.template_id == template_id)
    )
    return int(result.scalar_one())


# ---------------------------------------------------------------------------
# Аккаунты рассылки
# ---------------------------------------------------------------------------


async def campaign_account_ids(session: AsyncSession, campaign_id: int) -> list[int]:
    result = await session.execute(
        select(GroupCampaignAccount.account_id)
        .where(GroupCampaignAccount.campaign_id == campaign_id)
        .order_by(GroupCampaignAccount.account_id)
    )
    return [r[0] for r in result.all()]


async def add_campaign_accounts(
    session: AsyncSession, campaign_id: int, account_ids: list[int]
) -> None:
    existing = set(await campaign_account_ids(session, campaign_id))
    for aid in account_ids:
        if aid not in existing:
            session.add(GroupCampaignAccount(campaign_id=campaign_id, account_id=aid))
    await session.flush()


async def remove_campaign_account(
    session: AsyncSession, campaign_id: int, account_id: int
) -> None:
    await session.execute(
        delete(GroupCampaignAccount).where(
            GroupCampaignAccount.campaign_id == campaign_id,
            GroupCampaignAccount.account_id == account_id,
        )
    )


async def list_group_capable_accounts(session: AsyncSession) -> list[Account]:
    """Живые аккаунты с ролью «Группы» (§22.1) — кандидаты для рассылки."""
    result = await session.execute(
        select(Account)
        .where(Account.role_groups.is_(True))
        .where(Account.status.notin_(list(GONE_ACCOUNT_STATUSES)))
        .order_by(Account.id)
    )
    return list(result.scalars().all())


# ---------------------------------------------------------------------------
# Цели: создание и выборки для UI
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class NewTarget:
    account_id: int | None
    ref_kind: str
    ref_value: str
    ref_key: str
    period_min: int | None
    period_max: int | None


async def insert_targets(
    session: AsyncSession, campaign_id: int, rows: list[NewTarget]
) -> int:
    """Вставка целей; пропускает уже существующие пары (account_id, ref_key)."""
    existing = {
        (r[0], r[1])
        for r in (
            await session.execute(
                select(GroupTarget.account_id, GroupTarget.ref_key).where(
                    GroupTarget.campaign_id == campaign_id
                )
            )
        ).all()
    }
    added = 0
    for r in rows:
        if (r.account_id, r.ref_key) in existing:
            continue
        existing.add((r.account_id, r.ref_key))
        session.add(
            GroupTarget(
                campaign_id=campaign_id,
                account_id=r.account_id,
                ref_kind=r.ref_kind,
                ref_value=r.ref_value,
                ref_key=r.ref_key,
                period_min=r.period_min,
                period_max=r.period_max,
                state="new",
            )
        )
        added += 1
    await session.flush()
    return added


async def campaign_ref_keys(session: AsyncSession, campaign_id: int) -> set[str]:
    result = await session.execute(
        select(GroupTarget.ref_key).where(GroupTarget.campaign_id == campaign_id).distinct()
    )
    return {r[0] for r in result.all()}


async def conflicts(
    session: AsyncSession,
    pairs: list[tuple[int, str]],
    *,
    exclude_campaign_id: int | None = None,
) -> int:
    """Сколько пар (аккаунт, группа) уже есть в других незавершённых рассылках (§22.4)."""
    if not pairs:
        return 0
    accounts = {a for a, _ in pairs}
    stmt = (
        select(GroupTarget.account_id, GroupTarget.ref_key)
        .join(GroupCampaign, GroupCampaign.id == GroupTarget.campaign_id)
        .where(GroupCampaign.status != STOPPED)
        .where(GroupTarget.account_id.in_(accounts))
        .where(GroupTarget.state != "excluded")
    )
    if exclude_campaign_id is not None:
        stmt = stmt.where(GroupTarget.campaign_id != exclude_campaign_id)
    taken = {(r[0], r[1]) for r in (await session.execute(stmt)).all()}
    return sum(1 for p in pairs if p in taken)


async def list_targets(
    session: AsyncSession, campaign_id: int, *, offset: int = 0, limit: int | None = None
) -> list[GroupTarget]:
    # Проблемные — первыми: их чаще всего ищут глазами.
    order_state = case(
        {"manual": 0, "excluded": 1, "pending": 2, "new": 3, "active": 4},
        value=GroupTarget.state,
        else_=5,
    )
    stmt = (
        select(GroupTarget)
        .where(GroupTarget.campaign_id == campaign_id)
        .order_by(order_state, GroupTarget.id)
        .offset(offset)
    )
    if limit is not None:
        stmt = stmt.limit(limit)
    return list((await session.execute(stmt)).scalars().all())


async def count_targets(session: AsyncSession, campaign_id: int) -> int:
    result = await session.execute(
        select(func.count()).where(GroupTarget.campaign_id == campaign_id)
    )
    return int(result.scalar_one())


async def retry_problem_targets(session: AsyncSession, campaign_id: int) -> int:
    """«♻ Вернуть проблемные» (§22.6): manual/excluded → new."""
    result = await session.execute(
        update(GroupTarget)
        .where(GroupTarget.campaign_id == campaign_id)
        .where(GroupTarget.state.in_(PROBLEM_STATES))
        .values(
            state="new", state_reason=None, fail_streak=0, next_at=None, updated_at=_now()
        )
    )
    return result.rowcount or 0


@dataclass
class CampaignStats:
    by_state: dict[str, int]
    accounts: int
    posts_24h: int
    posts_since: int
    last_post_at: datetime | None
    next_post_at: datetime | None
    unassigned: int

    @property
    def total(self) -> int:
        return sum(self.by_state.values())

    @property
    def problems(self) -> int:
        return self.by_state.get("manual", 0) + self.by_state.get("excluded", 0)


async def campaign_stats(
    session: AsyncSession, campaign_id: int, *, since: datetime | None = None
) -> CampaignStats:
    now = _now()
    rows = (
        await session.execute(
            select(GroupTarget.state, func.count())
            .where(GroupTarget.campaign_id == campaign_id)
            .group_by(GroupTarget.state)
        )
    ).all()
    by_state = {r[0]: int(r[1]) for r in rows}
    accounts = len(await campaign_account_ids(session, campaign_id))

    async def _posts_since(ts: datetime) -> int:
        res = await session.execute(
            select(func.count())
            .where(GroupPost.campaign_id == campaign_id)
            .where(GroupPost.posted_at >= ts)
        )
        return int(res.scalar_one())

    posts_24h = await _posts_since(now - timedelta(hours=24))
    posts_since = await _posts_since(since) if since is not None else 0
    last_post_at = (
        await session.execute(
            select(func.max(GroupPost.posted_at)).where(GroupPost.campaign_id == campaign_id)
        )
    ).scalar_one()
    next_post_at = (
        await session.execute(
            select(func.min(GroupTarget.next_at))
            .where(GroupTarget.campaign_id == campaign_id)
            .where(GroupTarget.state == "active")
        )
    ).scalar_one()
    unassigned = int(
        (
            await session.execute(
                select(func.count())
                .where(GroupTarget.campaign_id == campaign_id)
                .where(GroupTarget.account_id.is_(None))
                .where(GroupTarget.state != "excluded")
            )
        ).scalar_one()
    )
    return CampaignStats(
        by_state=by_state,
        accounts=accounts,
        posts_24h=posts_24h,
        posts_since=posts_since,
        last_post_at=last_post_at,
        next_post_at=next_post_at,
        unassigned=unassigned,
    )


async def account_target_counts(
    session: AsyncSession, campaign_id: int
) -> dict[int, int]:
    """Число неисключённых целей у каждого аккаунта рассылки."""
    rows = (
        await session.execute(
            select(GroupTarget.account_id, func.count())
            .where(GroupTarget.campaign_id == campaign_id)
            .where(GroupTarget.account_id.is_not(None))
            .where(GroupTarget.state != "excluded")
            .group_by(GroupTarget.account_id)
        )
    ).all()
    return {int(r[0]): int(r[1]) for r in rows}


async def new_target_ids_by_account(
    session: AsyncSession, campaign_id: int
) -> dict[int, list[int]]:
    rows = (
        await session.execute(
            select(GroupTarget.account_id, GroupTarget.id)
            .where(GroupTarget.campaign_id == campaign_id)
            .where(GroupTarget.state == "new")
            .where(GroupTarget.account_id.is_not(None))
            .order_by(GroupTarget.id)
        )
    ).all()
    out: dict[int, list[int]] = {}
    for acc, tid in rows:
        out.setdefault(int(acc), []).append(int(tid))
    return out


async def reassign_targets(
    session: AsyncSession, assignments: dict[int, int | None]
) -> None:
    """Передать цели другому аккаунту как new: членство и peer у него свои (§22.7)."""
    for target_id, account_id in assignments.items():
        await session.execute(
            update(GroupTarget)
            .where(GroupTarget.id == target_id)
            .values(
                account_id=account_id,
                state="new",
                state_reason=None,
                chat_id=None,
                access_hash=None,
                peer_type=None,
                joined_by_us=False,
                joined_at=None,
                next_at=None,
                fail_streak=0,
                updated_at=_now(),
            )
        )


async def delete_account_targets(
    session: AsyncSession, campaign_id: int, account_id: int
) -> int:
    result = await session.execute(
        delete(GroupTarget).where(
            GroupTarget.campaign_id == campaign_id, GroupTarget.account_id == account_id
        )
    )
    return result.rowcount or 0


async def orphan_targets(session: AsyncSession, campaign_id: int) -> list[GroupTarget]:
    """Цели без живого аккаунта рассылки (§22.7): аккаунт не задан, dead/disabled или
    исключён из рассылки. Исключённые цели не трогаем."""
    members = select(GroupCampaignAccount.account_id).where(
        GroupCampaignAccount.campaign_id == campaign_id
    )
    gone = select(Account.id).where(Account.status.in_(list(GONE_ACCOUNT_STATUSES)))
    result = await session.execute(
        select(GroupTarget)
        .where(GroupTarget.campaign_id == campaign_id)
        .where(GroupTarget.state != "excluded")
        .where(
            or_(
                GroupTarget.account_id.is_(None),
                GroupTarget.account_id.in_(gone),
                GroupTarget.account_id.notin_(members),
            )
        )
        .order_by(GroupTarget.id)
    )
    return list(result.scalars().all())


async def alive_campaign_accounts(session: AsyncSession, campaign_id: int) -> list[int]:
    """Живые аккаунты рассылки с ролью «Группы» — кому можно отдать сирот."""
    result = await session.execute(
        select(Account.id)
        .join(GroupCampaignAccount, GroupCampaignAccount.account_id == Account.id)
        .where(GroupCampaignAccount.campaign_id == campaign_id)
        .where(Account.status.notin_(list(GONE_ACCOUNT_STATUSES)))
        .where(Account.role_groups.is_(True))
        .order_by(Account.id)
    )
    return [r[0] for r in result.all()]


# ---------------------------------------------------------------------------
# Групповая линия воркера (§22.5)
# ---------------------------------------------------------------------------


@dataclass
class DueTarget:
    target: GroupTarget
    campaign: GroupCampaign
    template_body: str


async def list_new_targets(
    session: AsyncSession, account_id: int, *, limit: int = 300
) -> list[GroupTarget]:
    result = await session.execute(
        select(GroupTarget)
        .join(GroupCampaign, GroupCampaign.id == GroupTarget.campaign_id)
        .where(GroupTarget.account_id == account_id)
        .where(GroupTarget.state == "new")
        .where(GroupCampaign.status == RUNNING)
        .order_by(GroupTarget.id)
        .limit(limit)
    )
    return list(result.scalars().all())


async def list_pending_due(
    session: AsyncSession, account_id: int, *, now: datetime, limit: int = 5
) -> list[GroupTarget]:
    result = await session.execute(
        select(GroupTarget)
        .join(GroupCampaign, GroupCampaign.id == GroupTarget.campaign_id)
        .where(GroupTarget.account_id == account_id)
        .where(GroupTarget.state == "pending")
        .where(or_(GroupTarget.next_at.is_(None), GroupTarget.next_at <= now))
        .where(GroupCampaign.status == RUNNING)
        .order_by(GroupTarget.next_at.nulls_first(), GroupTarget.id)
        .limit(limit)
    )
    return list(result.scalars().all())


async def list_due_active(
    session: AsyncSession, account_id: int, *, now: datetime, limit: int = 30
) -> list[DueTarget]:
    result = await session.execute(
        select(GroupTarget, GroupCampaign, Template.body)
        .join(GroupCampaign, GroupCampaign.id == GroupTarget.campaign_id)
        .join(Template, Template.id == GroupCampaign.template_id)
        .where(GroupTarget.account_id == account_id)
        .where(GroupTarget.state == "active")
        .where(GroupTarget.next_at <= now)
        .where(GroupCampaign.status == RUNNING)
        .order_by(GroupTarget.next_at, GroupTarget.id)
        .limit(limit)
    )
    return [DueTarget(t, c, body) for t, c, body in result.all()]


async def find_known_peer(
    session: AsyncSession, account_id: int, ref_key: str
) -> GroupTarget | None:
    """Та же группа у того же аккаунта в другой рассылке — peer уже известен (0 запросов)."""
    result = await session.execute(
        select(GroupTarget)
        .where(GroupTarget.account_id == account_id)
        .where(GroupTarget.ref_key == ref_key)
        .where(GroupTarget.chat_id.is_not(None))
        .where(GroupTarget.state == "active")
        .limit(1)
    )
    return result.scalar_one_or_none()


async def activate_target(
    session: AsyncSession,
    target_id: int,
    *,
    chat_id: int,
    access_hash: int | None,
    peer_type: str,
    title: str | None,
    joined_by_us: bool,
    next_at: datetime,
    slowmode_sec: int | None = None,
) -> None:
    values: dict = dict(
        state="active",
        state_reason=None,
        chat_id=chat_id,
        access_hash=access_hash,
        peer_type=peer_type,
        title=(title or "")[:255] or None,
        next_at=next_at,
        fail_streak=0,
        updated_at=_now(),
    )
    if joined_by_us:
        values.update(joined_by_us=True, joined_at=_now())
    if slowmode_sec is not None:
        values["slowmode_sec"] = slowmode_sec
    await session.execute(
        update(GroupTarget).where(GroupTarget.id == target_id).values(**values)
    )


async def set_target_state(
    session: AsyncSession,
    target_id: int,
    state: str,
    *,
    reason: str | None = None,
    next_at: datetime | None = None,
    title: str | None = None,
) -> None:
    values: dict = dict(state=state, state_reason=reason, next_at=next_at, updated_at=_now())
    if title:
        values["title"] = title[:255]
    await session.execute(
        update(GroupTarget).where(GroupTarget.id == target_id).values(**values)
    )


async def mark_join_requested(session: AsyncSession, target_id: int, *, at: datetime) -> None:
    """Момент подачи заявки на вступление (таймаут ожидания, §22.5)."""
    await session.execute(
        update(GroupTarget).where(GroupTarget.id == target_id).values(joined_at=at)
    )


async def mark_posted(
    session: AsyncSession,
    target: GroupTarget,
    *,
    now: datetime,
    next_at: datetime,
    message_id: int | None,
) -> None:
    await session.execute(
        update(GroupTarget)
        .where(GroupTarget.id == target.id)
        .values(
            last_post_at=now,
            next_at=next_at,
            posts_count=GroupTarget.posts_count + 1,
            fail_streak=0,
            state_reason=None,
            updated_at=now,
        )
    )
    session.add(
        GroupPost(
            campaign_id=target.campaign_id,
            target_id=target.id,
            account_id=target.account_id,
            posted_at=now,
            message_id=message_id,
        )
    )
    await session.flush()


async def reschedule(
    session: AsyncSession,
    target_id: int,
    *,
    next_at: datetime,
    slowmode_sec: int | None = None,
) -> None:
    values: dict = dict(next_at=next_at, updated_at=_now())
    if slowmode_sec is not None:
        values["slowmode_sec"] = slowmode_sec
    await session.execute(
        update(GroupTarget).where(GroupTarget.id == target_id).values(**values)
    )


async def bump_fail(
    session: AsyncSession, target_id: int, *, reason: str, next_at: datetime
) -> int:
    """Ошибка подряд +1; возвращает новую серию (§22.6)."""
    result = await session.execute(
        update(GroupTarget)
        .where(GroupTarget.id == target_id)
        .values(
            fail_streak=GroupTarget.fail_streak + 1,
            state_reason=reason[:500],
            next_at=next_at,
            updated_at=_now(),
        )
        .returning(GroupTarget.fail_streak)
    )
    return int(result.scalar_one())


async def defer_siblings(
    session: AsyncSession, target: GroupTarget, *, not_before: datetime
) -> None:
    """Режим all: другие аккаунты рассылки не постят в ту же группу сразу после нас."""
    await session.execute(
        update(GroupTarget)
        .where(GroupTarget.campaign_id == target.campaign_id)
        .where(GroupTarget.ref_key == target.ref_key)
        .where(GroupTarget.id != target.id)
        .where(GroupTarget.state == "active")
        .where(and_(GroupTarget.next_at.is_not(None), GroupTarget.next_at < not_before))
        .values(next_at=not_before)
    )


async def inc_group_post(session: AsyncSession, account_id: int) -> None:
    await session.execute(
        update(Account)
        .where(Account.id == account_id)
        .values(daily_group_posts=Account.daily_group_posts + 1, last_used_at=_now())
    )


async def inc_group_join(session: AsyncSession, account_id: int) -> None:
    now = _now()
    await session.execute(
        update(Account)
        .where(Account.id == account_id)
        .values(
            daily_group_joins=Account.daily_group_joins + 1,
            last_group_join_at=now,
            last_used_at=now,
        )
    )


# ---------------------------------------------------------------------------
# Роли аккаунтов (§22.1)
# ---------------------------------------------------------------------------


async def set_role(
    session: AsyncSession, account_id: int, *, role: str, value: bool
) -> None:
    if role not in ("dm", "groups"):
        raise ValueError(role)
    column = "role_dm" if role == "dm" else "role_groups"
    await session.execute(
        update(Account).where(Account.id == account_id).values({column: value})
    )
