"""CRUD по таблице tasks. ARCHITECTURE.md §4.5, §9.1, §9.2, §9.3.

В MVP-2 захват задачи делается простым SELECT с LIMIT 1 (один воркер,
без конкуренции). SKIP LOCKED-захват для пула воркеров добавится в MVP-3.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ResultCode, Task, TaskStatus


async def bulk_create(
    session: AsyncSession, *, campaign_id: int, usernames: list[str]
) -> int:
    """Bulk insert новых задач для кампании. Дубли по (campaign_id, username) — пропускаются."""
    if not usernames:
        return 0
    stmt = insert(Task).values(
        [{"campaign_id": campaign_id, "username": u} for u in usernames]
    )
    stmt = stmt.on_conflict_do_nothing(index_elements=["campaign_id", "username"])
    result = await session.execute(stmt)
    return result.rowcount or 0


async def get_next_queued(
    session: AsyncSession, *, campaign_id: int
) -> Task | None:
    """Простой fetch одной задачи без блокировки.

    Для MVP-2 (один воркер). В MVP-3 заменится на SKIP LOCKED-запрос из §6.2.
    """
    now = datetime.now(timezone.utc)
    stmt = (
        select(Task)
        .where(Task.campaign_id == campaign_id)
        .where(Task.status == TaskStatus.queued)
        .where((Task.locked_until.is_(None)) | (Task.locked_until <= now))
        .order_by(Task.id)
        .limit(1)
    )
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def mark_in_progress(
    session: AsyncSession, *, task_id: int, account_id: int
) -> None:
    await session.execute(
        update(Task)
        .where(Task.id == task_id)
        .values(
            status=TaskStatus.in_progress,
            assigned_account_id=account_id,
            attempts=Task.attempts + 1,
            last_attempt_at=datetime.now(timezone.utc),
        )
    )


async def mark_done(
    session: AsyncSession, *, task_id: int, result_code: ResultCode
) -> None:
    await session.execute(
        update(Task)
        .where(Task.id == task_id)
        .values(
            status=TaskStatus.done,
            result_code=result_code,
            processed_at=datetime.now(timezone.utc),
        )
    )


async def mark_skipped(
    session: AsyncSession,
    *,
    task_id: int,
    result_code: ResultCode,
    error_message: str | None = None,
) -> None:
    await session.execute(
        update(Task)
        .where(Task.id == task_id)
        .values(
            status=TaskStatus.skipped,
            result_code=result_code,
            error_message=error_message,
            processed_at=datetime.now(timezone.utc),
        )
    )


async def mark_failed(
    session: AsyncSession,
    *,
    task_id: int,
    result_code: ResultCode,
    error_message: str | None = None,
) -> None:
    await session.execute(
        update(Task)
        .where(Task.id == task_id)
        .values(
            status=TaskStatus.failed,
            result_code=result_code,
            error_message=error_message,
            processed_at=datetime.now(timezone.utc),
        )
    )


async def requeue_with_delay(
    session: AsyncSession, *, task_id: int, delay_seconds: int
) -> None:
    """ARCHITECTURE.md §9.3 — возврат задачи в queued с отложенным locked_until."""
    unlock_at = datetime.now(timezone.utc) + timedelta(seconds=delay_seconds)
    await session.execute(
        update(Task)
        .where(Task.id == task_id)
        .values(
            status=TaskStatus.queued,
            assigned_account_id=None,
            locked_until=unlock_at,
        )
    )


async def recover_stuck_in_progress(session: AsyncSession) -> int:
    """При перезапуске возвращаем зависшие в in_progress > 1 часа задачи в queued.

    ARCHITECTURE.md §9.3 «При перезапуске приложения».
    """
    sql = text(
        """
        UPDATE tasks
        SET status = 'queued', assigned_account_id = NULL
        WHERE status = 'in_progress'
          AND last_attempt_at < NOW() - INTERVAL '1 hour'
        """
    )
    result = await session.execute(sql)
    return result.rowcount or 0


async def count_remaining_queued(
    session: AsyncSession, *, campaign_id: int
) -> int:
    result = await session.execute(
        select(Task.id)
        .where(Task.campaign_id == campaign_id)
        .where(Task.status.in_([TaskStatus.queued, TaskStatus.in_progress]))
    )
    return len(result.all())
