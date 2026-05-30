"""DB-снимок приглашённых нами участников. ARCHITECTURE.md §19 #11 (инкрементально).

Пишем ТОЛЬКО тех, кого пригласили мы — чтобы после рестарта не пытаться пригласить
повторно (двойной инвайт палит аккаунт). Полный префетч членства чата остаётся
in-memory (см. app/telegram/invite.py ParticipantsCache).
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import InvitedParticipant


async def add_invited(
    session: AsyncSession,
    *,
    chat_id: int,
    user_id: int,
    campaign_id: int | None = None,
) -> None:
    """Записать факт нашего инвайта. Idempotent по PK (chat_id, user_id)."""
    stmt = insert(InvitedParticipant).values(
        chat_id=chat_id, user_id=user_id, campaign_id=campaign_id
    )
    stmt = stmt.on_conflict_do_nothing(index_elements=["chat_id", "user_id"])
    await session.execute(stmt)


async def list_invited_user_ids(
    session: AsyncSession, *, chat_id: int
) -> set[int]:
    """user_id всех, кого мы приглашали в этот чат — для предзагрузки кэша членства."""
    result = await session.execute(
        select(InvitedParticipant.user_id).where(
            InvitedParticipant.chat_id == chat_id
        )
    )
    return {row[0] for row in result.all()}
