"""Оркестрация кампаний. ARCHITECTURE.md §9.1, §10.4.

В MVP-3 воркеры долгоживущие (WorkerPool), не привязаны к одной кампании.
Manager отвечает только за CRUD и state-transitions самих кампаний — задачи
подхватит первый свободный воркер через SKIP LOCKED.
"""

from __future__ import annotations

from loguru import logger

from app.db.models import CampaignStatus
from app.db.repositories import campaigns as campaigns_repo
from app.db.repositories import logs as logs_repo
from app.db.repositories import processed as processed_repo
from app.db.repositories import tasks as tasks_repo
from app.db.session import session_scope


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


async def start_campaign(campaign_id: int) -> tuple[bool, str]:
    """Переводит кампанию в running. Долгоживущий WorkerPool сам подберёт задачи."""
    async with session_scope() as session:
        campaign = await campaigns_repo.get_by_id(session, campaign_id)
        if campaign is None:
            return False, "Кампания не найдена"
        if campaign.status not in (CampaignStatus.pending, CampaignStatus.paused):
            return (
                False,
                f"Кампания в статусе {campaign.status.value}, старт невозможен",
            )
        await campaigns_repo.set_status(
            session, campaign_id=campaign_id, status=CampaignStatus.running
        )
        await logs_repo.log_event(
            session,
            level="info",
            event_type="campaign_started",
            campaign_id=campaign_id,
            message=f"Кампания #{campaign_id} переведена в running",
        )

    return True, f"Кампания #{campaign_id} в работе. Воркеры подхватят задачи."


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
    # Чистим in-memory реестр непригодных для инвайта аккаунтов (MVP-4 §5.3),
    # чтобы новая кампания с этим id не наследовала старые пометки (MVP-6, гигиена).
    try:
        from app.telegram import invite as invite_mod

        invite_mod.reset_campaign(campaign_id)
    except Exception:
        logger.debug("reset_campaign ineligible failed for #{}", campaign_id)
    # Воркер сам обнаружит и завершится.
    return True, "Отменено"


async def recover_at_startup() -> None:
    """§9.3 «При перезапуске» + §18.4 шаг 6.

    Возвращает зависшие in_progress задачи (>1ч без обновления) в queued.
    Все кампании status=running остаются как есть — пул воркеров сам подхватит.
    """
    async with session_scope() as session:
        recovered = await tasks_repo.recover_stuck_in_progress(session)
        if recovered:
            logger.info("Recovered {} stuck in_progress tasks → queued", recovered)
            await logs_repo.log_event(
                session,
                level="info",
                event_type="tasks_recovered",
                message=f"При старте: {recovered} зависших задач возвращены в queued",
            )
