"""CRUD по таблице campaigns. ARCHITECTURE.md §4.4, §10.4."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Campaign, CampaignStatus, CampaignType


async def list_campaigns(
    session: AsyncSession,
    *,
    statuses: list[CampaignStatus] | None = None,
    limit: int = 50,
) -> list[Campaign]:
    stmt = select(Campaign).order_by(Campaign.id.desc())
    if statuses:
        stmt = stmt.where(Campaign.status.in_(statuses))
    stmt = stmt.limit(limit)
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def get_by_id(session: AsyncSession, campaign_id: int) -> Campaign | None:
    return await session.get(Campaign, campaign_id)


async def list_running(session: AsyncSession) -> list[Campaign]:
    result = await session.execute(
        select(Campaign).where(Campaign.status == CampaignStatus.running)
    )
    return list(result.scalars().all())


async def create_campaign(
    session: AsyncSession,
    *,
    type_: CampaignType,
    created_by_user_id: int,
    template_id: int | None = None,
    target_chat: str | None = None,
    target_chat_id: int | None = None,
    resend_old: bool = False,
    notes: str | None = None,
) -> Campaign:
    campaign = Campaign(
        type=type_,
        template_id=template_id,
        target_chat=target_chat,
        target_chat_id=target_chat_id,
        resend_old=resend_old,
        notes=notes,
        created_by_user_id=created_by_user_id,
        status=CampaignStatus.pending,
    )
    session.add(campaign)
    await session.flush()
    return campaign


async def set_status(
    session: AsyncSession,
    *,
    campaign_id: int,
    status: CampaignStatus,
    mark_finished: bool = False,
) -> None:
    values: dict = {"status": status}
    if status == CampaignStatus.running:
        values["started_at"] = datetime.now(timezone.utc)
    if mark_finished or status in {
        CampaignStatus.done,
        CampaignStatus.failed,
        CampaignStatus.cancelled,
    }:
        values["finished_at"] = datetime.now(timezone.utc)
    await session.execute(
        update(Campaign).where(Campaign.id == campaign_id).values(**values)
    )


async def update_counts(
    session: AsyncSession,
    *,
    campaign_id: int,
    total: int | None = None,
    sent_delta: int = 0,
    skipped_delta: int = 0,
    failed_delta: int = 0,
) -> None:
    values: dict = {}
    if total is not None:
        values["total_count"] = total
    if sent_delta:
        values["sent_count"] = Campaign.sent_count + sent_delta
    if skipped_delta:
        values["skipped_count"] = Campaign.skipped_count + skipped_delta
    if failed_delta:
        values["failed_count"] = Campaign.failed_count + failed_delta
    if not values:
        return
    await session.execute(
        update(Campaign).where(Campaign.id == campaign_id).values(**values)
    )
