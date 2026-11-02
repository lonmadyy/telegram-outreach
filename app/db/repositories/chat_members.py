"""Снимок участников целевых чатов инвайт-кампаний. ARCHITECTURE.md §4.11, §21.4."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ChatMember, ChatMemberLoad

SNAPSHOT_TTL = timedelta(hours=24)
_INSERT_CHUNK = 5000


async def get_fresh_snapshot(
    session: AsyncSession, *, chat_id: int, now: datetime | None = None
) -> set[int] | None:
    """user_id участников, если снимок моложе SNAPSHOT_TTL, иначе None."""
    now = now or datetime.now(timezone.utc)
    load = await session.get(ChatMemberLoad, chat_id)
    if load is None or now - load.loaded_at > SNAPSHOT_TTL:
        return None
    result = await session.execute(
        select(ChatMember.user_id).where(ChatMember.chat_id == chat_id)
    )
    return {row[0] for row in result.all()}


async def save_snapshot(session: AsyncSession, *, chat_id: int, user_ids: set[int]) -> None:
    await session.execute(delete(ChatMember).where(ChatMember.chat_id == chat_id))
    rows = [{"chat_id": chat_id, "user_id": uid} for uid in user_ids]
    for i in range(0, len(rows), _INSERT_CHUNK):
        await session.execute(
            pg_insert(ChatMember).values(rows[i : i + _INSERT_CHUNK]).on_conflict_do_nothing()
        )
    stmt = pg_insert(ChatMemberLoad).values(
        chat_id=chat_id, loaded_at=datetime.now(timezone.utc), members=len(user_ids)
    )
    await session.execute(
        stmt.on_conflict_do_update(
            index_elements=["chat_id"],
            set_={"loaded_at": stmt.excluded.loaded_at, "members": stmt.excluded.members},
        )
    )
