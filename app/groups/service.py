"""Операции над групповыми рассылками. ARCHITECTURE.md §22.4, §22.7, §22.9.

Здесь — то, что затрагивает несколько таблиц сразу: создание рассылки из мастера,
добавление групп и аккаунтов в работающую рассылку, передача групп выбывшего аккаунта,
блок сводки. Хендлеры бота и планировщик зовут эти функции, а не репозиторий напрямую.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from loguru import logger

from app.db.repositories import group_campaigns as repo
from app.db.session import session_scope
from app.groups import notify
from app.groups.formatting import progress_block
from app.groups.lines import GroupEntry
from app.groups.planner import (
    assign_targets,
    rebalance_orphans,
    take_for_new_account,
)


@dataclass
class Draft:
    """Данные мастера новой рассылки."""

    name: str
    template_id: int
    entries: list[GroupEntry]
    account_ids: list[int]
    distribution: str
    period_min: int
    period_max: int
    gap_min_sec: int
    gap_max_sec: int
    window_start: str | None = None
    window_end: str | None = None


@dataclass
class Plan:
    rows: list[repo.NewTarget] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    per_account: dict[int, int] = field(default_factory=dict)


def _row(entry: GroupEntry, account_id: int | None) -> repo.NewTarget:
    lo, hi = entry.period if entry.period else (None, None)
    return repo.NewTarget(
        account_id=account_id,
        ref_kind=entry.token.kind,
        ref_value=str(entry.token.value),
        ref_key=entry.key,
        period_min=lo,
        period_max=hi,
    )


def plan_targets(
    entries: list[GroupEntry],
    account_ids: list[int],
    distribution: str,
    *,
    initial_load: dict[int, int] | None = None,
) -> Plan:
    """Строки + аккаунты + режим → строки group_targets (чисто, без БД)."""
    assignments, problems = assign_targets(
        entries, account_ids, distribution, initial_load=initial_load
    )
    plan = Plan(problems=problems)
    for a in assignments:
        plan.rows.append(_row(a.entry, a.account_id))
        plan.per_account[a.account_id] = plan.per_account.get(a.account_id, 0) + 1
    return plan


async def count_conflicts(plan: Plan, *, exclude_campaign_id: int | None = None) -> int:
    pairs = [(r.account_id, r.ref_key) for r in plan.rows if r.account_id is not None]
    async with session_scope() as session:
        return await repo.conflicts(session, pairs, exclude_campaign_id=exclude_campaign_id)


async def create_campaign(draft: Draft, *, user_id: int | None) -> tuple[int, int]:
    """Создать и запустить рассылку. → (id, сколько целей создано)."""
    plan = plan_targets(draft.entries, draft.account_ids, draft.distribution)
    async with session_scope() as session:
        c = await repo.create_campaign(
            session,
            name=draft.name,
            template_id=draft.template_id,
            distribution=draft.distribution,
            period_min=draft.period_min,
            period_max=draft.period_max,
            gap_min_sec=draft.gap_min_sec,
            gap_max_sec=draft.gap_max_sec,
            window_start=draft.window_start,
            window_end=draft.window_end,
            created_by_user_id=user_id,
        )
        await repo.add_campaign_accounts(session, c.id, draft.account_ids)
        added = await repo.insert_targets(session, c.id, plan.rows)
        campaign_id = c.id
    logger.info("group campaign #{} created: targets={}", campaign_id, added)
    return campaign_id, added


async def add_groups(campaign_id: int, entries: list[GroupEntry]) -> tuple[int, int, list[str]]:
    """Добавить группы в рассылку по её режиму. → (добавлено, уже было, проблемы)."""
    async with session_scope() as session:
        c = await repo.get_campaign(session, campaign_id)
        if c is None:
            return 0, 0, ["рассылка не найдена"]
        account_ids = await repo.alive_campaign_accounts(session, campaign_id)
        existing = await repo.campaign_ref_keys(session, campaign_id)
        load = await repo.account_target_counts(session, campaign_id)
    fresh = [e for e in entries if e.key not in existing]
    already = len(entries) - len(fresh)
    if not fresh:
        return 0, already, []
    if not account_ids:
        return 0, already, ["в рассылке нет живых аккаунтов с ролью «Группы»"]
    plan = plan_targets(fresh, account_ids, c.distribution, initial_load=load)
    async with session_scope() as session:
        added = await repo.insert_targets(session, campaign_id, plan.rows)
    return added, already, plan.problems


async def add_account(campaign_id: int, account_id: int) -> str:
    """Добавить аккаунт в рассылку (§22.9). → человеческий итог."""
    async with session_scope() as session:
        c = await repo.get_campaign(session, campaign_id)
        if c is None:
            return "рассылка не найдена"
        await repo.add_campaign_accounts(session, campaign_id, [account_id])
        if c.distribution == "all":
            targets = await repo.list_targets(session, campaign_id)
            seen: dict[str, object] = {}
            for t in targets:
                seen.setdefault(t.ref_key, t)
            rows = [
                repo.NewTarget(
                    account_id=account_id,
                    ref_kind=t.ref_kind,
                    ref_value=t.ref_value,
                    ref_key=t.ref_key,
                    period_min=t.period_min,
                    period_max=t.period_max,
                )
                for t in seen.values()
            ]
            added = await repo.insert_targets(session, campaign_id, rows)
            return f"аккаунт #{account_id} добавлен, ему назначено групп: {added}"
        totals = await repo.account_target_counts(session, campaign_id)
        totals.pop(account_id, None)
        new_by_acc = await repo.new_target_ids_by_account(session, campaign_id)
        new_by_acc.pop(account_id, None)
        moved = take_for_new_account(new_by_acc, totals)
        await repo.reassign_targets(session, {tid: account_id for tid in moved})
    if moved:
        return f"аккаунт #{account_id} добавлен, ему переданы ещё не начатые группы: {len(moved)}"
    return (
        f"аккаунт #{account_id} добавлен. Не начатых групп для передачи нет — новые "
        "группы будут доставаться ему в первую очередь."
    )


async def remove_account(campaign_id: int, account_id: int) -> str:
    """Убрать аккаунт из рассылки; его группы — остальным (§22.7)."""
    async with session_scope() as session:
        c = await repo.get_campaign(session, campaign_id)
        if c is None:
            return "рассылка не найдена"
        await repo.remove_campaign_account(session, campaign_id, account_id)
        if c.distribution == "all":
            n = await repo.delete_account_targets(session, campaign_id, account_id)
            return (
                f"аккаунт #{account_id} убран, его постов в группы больше не будет "
                f"(групп у него было: {n}; их ведут остальные аккаунты)"
            )
    moved, waiting = await _rehome_orphans(campaign_id)
    msg = f"аккаунт #{account_id} убран"
    if moved:
        msg += f", его группы переданы другим: {moved}"
    if waiting:
        msg += f", без аккаунта осталось групп: {waiting}"
    return msg


async def _rehome_orphans(campaign_id: int) -> tuple[int, int]:
    """Сироты рассылки → наименее загруженным живым аккаунтам. → (передано, ждут)."""
    async with session_scope() as session:
        orphans = await repo.orphan_targets(session, campaign_id)
        if not orphans:
            return 0, 0
        alive = await repo.alive_campaign_accounts(session, campaign_id)
        counts = await repo.account_target_counts(session, campaign_id)
        load = {a: counts.get(a, 0) for a in alive}
        mapping = rebalance_orphans([t.id for t in orphans], load)
        if mapping:
            await repo.reassign_targets(session, mapping)
            return len(mapping), 0
        # Живых нет: снимаем мёртвый аккаунт, чтобы цели не числились за ним.
        stale = {t.id: None for t in orphans if t.account_id is not None}
        if stale:
            await repo.reassign_targets(session, stale)
        return 0, len(orphans)


# Чтобы не повторять одно и то же уведомление каждые 5 минут.
_reported: set[tuple[int, int, int]] = set()


async def maintenance() -> None:
    """Джоба §22.7: группы выбывших аккаунтов → живым аккаунтам рассылки."""
    async with session_scope() as session:
        campaigns = await repo.list_campaigns(
            session, statuses=(repo.RUNNING, repo.PAUSED), limit=200
        )
    for c in campaigns:
        if c.distribution == "all":
            continue  # группы и так покрыты остальными аккаунтами
        try:
            moved, waiting = await _rehome_orphans(c.id)
        except Exception:
            logger.exception("group maintenance failed for campaign #{}", c.id)
            continue
        key = (c.id, moved, waiting)
        if (moved or waiting) and key not in _reported:
            _reported.add(key)
            if moved:
                notify.queue(
                    f"«{c.name}»: у аккаунта, выбывшего из рассылки, было {moved} групп — "
                    "переданы другим аккаунтам рассылки"
                )
            if waiting:
                notify.queue(
                    f"«{c.name}»: {waiting} групп без живого аккаунта — добавьте аккаунт "
                    "с ролью «Группы» в рассылку"
                )


async def progress_text(session, *, interval_sec: int) -> str | None:
    """Блок «Группы» для сводки §10.5. None — работающих групповых рассылок нет."""
    campaigns = await repo.list_campaigns(session, statuses=(repo.RUNNING,), limit=50)
    if not campaigns:
        return None
    since = datetime.now(timezone.utc) - timedelta(seconds=interval_sec)
    items = []
    for c in campaigns:
        stats = await repo.campaign_stats(session, c.id, since=since)
        items.append((c, stats))
    minutes = max(1, interval_sec // 60)
    label = f"{minutes} мин" if minutes < 60 else f"{minutes // 60} ч"
    return progress_block(items, interval_label=label)
