"""CRUD по таблице processed_clients. ARCHITECTURE.md §4.6, §9.1.

Глобальный реестр обработанных клиентов между всеми кампаниями. Запись
происходит ТОЛЬКО при `result_code = ok` (§4.6 конец раздела).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ProcessedClient, ResultCode

PROCESSED_CUTOFF_DAYS = 180

# Размер чанка для запросов с большим списком usernames. PostgreSQL/asyncpg не
# принимают больше ~65535 параметров на один запрос, поэтому большие списки
# (например 120k из TXT) бьём на части (§9.1).
_USERNAMES_CHUNK = 5000


async def already_processed_usernames(
    session: AsyncSession, *, usernames: list[str], resend_old: bool
) -> set[str]:
    """Возвращает usernames, которые НУЖНО пропустить при создании задач кампании.

    Логика из §4.6 «Логика проверки» / §9.1 п.2:
      - resend_old=False: пропускаем ВСЕХ, кто обрабатывался когда-либо.
      - resend_old=True:  пропускаем только обработанных за последние 180 дней.

    Большой список бьётся на чанки, чтобы не превысить лимит параметров запроса.
    """
    if not usernames:
        return set()

    cutoff = None
    if resend_old:
        cutoff = datetime.now(timezone.utc) - timedelta(days=PROCESSED_CUTOFF_DAYS)

    skip: set[str] = set()
    for i in range(0, len(usernames), _USERNAMES_CHUNK):
        chunk = usernames[i : i + _USERNAMES_CHUNK]
        stmt = select(ProcessedClient.username).where(
            ProcessedClient.username.in_(chunk)
        )
        if cutoff is not None:
            stmt = stmt.where(ProcessedClient.last_processed_at >= cutoff)
        result = await session.execute(stmt)
        skip.update(row[0] for row in result.all())
    return skip


async def register_processed(
    session: AsyncSession,
    *,
    username: str,
    last_action: str,
    last_result_code: ResultCode,
    account_id: int | None,
    campaign_id: int | None,
) -> None:
    """Upsert обработанного клиента.

    Должно вызываться ТОЛЬКО при `result_code = ok` — иначе клиент не
    блокируется для будущих кампаний.
    """
    stmt = insert(ProcessedClient).values(
        username=username,
        last_action=last_action,
        last_result_code=last_result_code,
        account_id=account_id,
        campaign_id=campaign_id,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=[ProcessedClient.username],
        set_=dict(
            last_action=stmt.excluded.last_action,
            last_result_code=stmt.excluded.last_result_code,
            account_id=stmt.excluded.account_id,
            campaign_id=stmt.excluded.campaign_id,
            last_processed_at=func.now(),
        ),
    )
    await session.execute(stmt)
