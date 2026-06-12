"""CRUD по таблице processed_clients. ARCHITECTURE.md §4.6, §9.1.

Глобальный реестр обработанных клиентов между всеми кампаниями. Пишется при
`result_code = ok` (успешный контакт) и при target-независимых СТРУКТУРНЫХ
отказах (STRUCTURAL_SKIP_CODES) — последние помним навсегда, чтобы будущие
кампании не сжигали антиспам-бюджет на заведомо непригодных (§4.6).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import func, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ProcessedClient, ResultCode

PROCESSED_CUTOFF_DAYS = 180

# Размер чанка для запросов с большим списком usernames. PostgreSQL/asyncpg не
# принимают больше ~65535 параметров на один запрос, поэтому большие списки
# (например 120k из TXT) бьём на части (§9.1).
_USERNAMES_CHUNK = 5000

# Структурные отказы, НЕ зависящие от целевого чата: помним глобально и больше
# не пробуем в будущих кампаниях (§4.6). Каждая попытка инвайта/DM — реальный
# API-запрос, нагружающий антиспам Telegram; для заведомо непригодных это чистая
# потеря бюджета. ВАЖНО: сюда НЕ входят target-специфичные коды
# (banned_in_channel, already_member) — они верны лишь для конкретного чата.
STRUCTURAL_SKIP_CODES: frozenset[ResultCode] = frozenset(
    {
        ResultCode.privacy_restricted,
        ResultCode.not_found,
        ResultCode.deactivated,
        ResultCode.too_many_channels,
    }
)


def is_structural_skip(result_code: ResultCode) -> bool:
    """Target-независимый структурный отказ — запоминается навсегда (§4.6).

    Только для таких кодов `_record_skip` пишет клиента в реестр; повторяемые
    (flood/peer/прочие ошибки) и target-специфичные (бан/уже-участник) — нет."""
    return result_code in STRUCTURAL_SKIP_CODES


async def already_processed_usernames(
    session: AsyncSession, *, usernames: list[str], resend_old: bool
) -> set[str]:
    """Возвращает usernames, которые НУЖНО пропустить при создании задач кампании.

    Логика из §4.6 «Логика проверки» / §9.1 п.2:
      - resend_old=False: пропускаем ВСЕХ, кто обрабатывался когда-либо.
      - resend_old=True:  пропускаем успехи за последние 180 дней, НО структурные
        отказы (STRUCTURAL_SKIP_CODES) пропускаем всегда — их не пробуем повторно.

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
            # resend_old: переотправляем старые УСПЕХИ, но структурные отказы
            # держим в skip всегда (они target-независимы и не «протухают»).
            stmt = stmt.where(
                or_(
                    ProcessedClient.last_result_code.in_(STRUCTURAL_SKIP_CODES),
                    ProcessedClient.last_processed_at >= cutoff,
                )
            )
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

    Вызывается при `result_code = ok` (успех) и при структурных отказах из
    STRUCTURAL_SKIP_CODES (см. is_structural_skip) — оба варианта блокируют
    клиента для будущих кампаний (§4.6).
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
