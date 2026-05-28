"""Оркестрация кампаний. ARCHITECTURE.md §9.1, §10.4.

В MVP-2 — один воркер на одну рассылочную кампанию. В MVP-3 пул воркеров
и SKIP LOCKED-захват задач придёт сюда же без поломки публичного API.
"""

from __future__ import annotations

import asyncio

from loguru import logger
from sqlalchemy import select

from app.db.models import Account, AccountStatus, CampaignStatus
from app.db.repositories import campaigns as campaigns_repo
from app.db.repositories import logs as logs_repo
from app.db.repositories import processed as processed_repo
from app.db.repositories import tasks as tasks_repo
from app.db.session import session_scope
from app.telegram.worker import run_campaign_worker


# campaign_id → asyncio.Task запущенного воркера.
_running_workers: dict[int, asyncio.Task] = {}


async def create_tasks_for_campaign(
    *, campaign_id: int, usernames: list[str]
) -> tuple[int, int]:
    """ARCHITECTURE.md §9.1: фильтрация по processed_clients + bulk insert.

    Возвращает (created_count, skipped_initially).
    """
    async with session_scope() as session:
        campaign = await campaigns_repo.get_by_id(session, campaign_id)
        if campaign is None:
            raise ValueError(f"Campaign #{campaign_id} not found")

        skip_set = await processed_repo.already_processed_usernames(
            session, usernames=usernames, resend_old=campaign.resend_old
        )

        to_create = [u for u in usernames if u not in skip_set]
        skipped = len(usernames) - len(to_create)

        await tasks_repo.bulk_create(
            session, campaign_id=campaign_id, usernames=to_create
        )
        await campaigns_repo.update_counts(
            session,
            campaign_id=campaign_id,
            total=len(to_create),
            skipped_delta=skipped,
        )

    return len(to_create), skipped


async def _pick_worker_account() -> Account | None:
    """В MVP-2 берём первый аккаунт со статусом active или warmup."""
    async with session_scope() as session:
        result = await session.execute(
            select(Account)
            .where(Account.status.in_([AccountStatus.active, AccountStatus.warmup]))
            .order_by(Account.id)
            .limit(1)
        )
        return result.scalar_one_or_none()


async def start_campaign(campaign_id: int) -> tuple[bool, str]:
    """Активирует кампанию и запускает worker.

    Возвращает (started, message). Не падает на «нет аккаунтов» — возвращает False.
    """
    async with session_scope() as session:
        campaign = await campaigns_repo.get_by_id(session, campaign_id)
        if campaign is None:
            return False, "Кампания не найдена"
        if campaign.status not in (CampaignStatus.pending, CampaignStatus.paused):
            return (
                False,
                f"Кампания в статусе {campaign.status.value}, старт невозможен",
            )

    account = await _pick_worker_account()
    if account is None:
        async with session_scope() as session:
            await campaigns_repo.set_status(
                session, campaign_id=campaign_id, status=CampaignStatus.failed
            )
            await logs_repo.log_event(
                session,
                level="error",
                event_type="campaign_failed",
                campaign_id=campaign_id,
                message="Нет доступных аккаунтов (active/warmup)",
            )
        return False, "Нет доступных аккаунтов для работы"

    async with session_scope() as session:
        await campaigns_repo.set_status(
            session, campaign_id=campaign_id, status=CampaignStatus.running
        )
        await logs_repo.log_event(
            session,
            level="info",
            event_type="campaign_started",
            campaign_id=campaign_id,
            account_id=account.id,
            message=f"Кампания #{campaign_id} запущена на аккаунте {account.phone}",
        )

    # Запускаем воркер в фоне.
    existing = _running_workers.get(campaign_id)
    if existing is not None and not existing.done():
        return True, "Воркер уже запущен (повторный старт проигнорирован)"

    task = asyncio.create_task(
        run_campaign_worker(campaign_id=campaign_id, account_id=account.id),
        name=f"campaign-{campaign_id}-account-{account.id}",
    )
    _running_workers[campaign_id] = task
    task.add_done_callback(lambda _t: _running_workers.pop(campaign_id, None))

    return True, f"Кампания #{campaign_id} запущена на {account.phone}"


async def pause_campaign(campaign_id: int) -> tuple[bool, str]:
    async with session_scope() as session:
        campaign = await campaigns_repo.get_by_id(session, campaign_id)
        if campaign is None:
            return False, "Кампания не найдена"
        if campaign.status != CampaignStatus.running:
            return False, f"Кампания не в running ({campaign.status.value})"
        await campaigns_repo.set_status(
            session, campaign_id=campaign_id, status=CampaignStatus.paused
        )
        await logs_repo.log_event(
            session,
            level="info",
            event_type="campaign_paused",
            campaign_id=campaign_id,
            message=f"Кампания #{campaign_id} поставлена на паузу",
        )
    # Воркер сам обнаружит изменение статуса в начале следующего цикла и выйдет.
    return True, "Поставлено на паузу"


async def resume_campaign(campaign_id: int) -> tuple[bool, str]:
    async with session_scope() as session:
        campaign = await campaigns_repo.get_by_id(session, campaign_id)
        if campaign is None:
            return False, "Кампания не найдена"
        if campaign.status != CampaignStatus.paused:
            return False, f"Кампания не в paused ({campaign.status.value})"
    # Просто запускаем заново.
    return await start_campaign(campaign_id)


async def stop_campaign(campaign_id: int) -> tuple[bool, str]:
    async with session_scope() as session:
        campaign = await campaigns_repo.get_by_id(session, campaign_id)
        if campaign is None:
            return False, "Кампания не найдена"
        if campaign.status in (
            CampaignStatus.done,
            CampaignStatus.cancelled,
            CampaignStatus.failed,
        ):
            return False, f"Кампания уже завершена ({campaign.status.value})"
        await campaigns_repo.set_status(
            session,
            campaign_id=campaign_id,
            status=CampaignStatus.cancelled,
            mark_finished=True,
        )
        await logs_repo.log_event(
            session,
            level="info",
            event_type="campaign_cancelled",
            campaign_id=campaign_id,
            message=f"Кампания #{campaign_id} отменена",
        )
    # Воркер сам обнаружит и завершится.
    return True, "Отменено"


def is_worker_running(campaign_id: int) -> bool:
    task = _running_workers.get(campaign_id)
    return task is not None and not task.done()


async def resume_running_campaigns_at_startup() -> int:
    """При старте приложения возобновляем все кампании в статусе running.

    Также возвращает stuck-in-progress задачи в queued (§9.3 «При перезапуске»).
    Возвращает кол-во возобновлённых кампаний.
    """
    async with session_scope() as session:
        recovered = await tasks_repo.recover_stuck_in_progress(session)
        if recovered:
            logger.info("Recovered {} stuck in_progress tasks → queued", recovered)
        running = await campaigns_repo.list_running(session)

    count = 0
    for campaign in running:
        ok, msg = await start_campaign(campaign.id)
        if ok:
            count += 1
            logger.info("Resumed campaign #{}: {}", campaign.id, msg)
        else:
            logger.warning("Failed to resume campaign #{}: {}", campaign.id, msg)
    return count
