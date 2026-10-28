"""Задания и результаты парсера групп. ARCHITECTURE.md §4.10, §20."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from typing import Iterable

from sqlalchemy import desc, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ParseJob, ParseResult

STATUS_RUNNING = "running"
STATUS_DONE = "done"
STATUS_STOPPED = "stopped"
STATUS_FAILED = "failed"
STATUS_INTERRUPTED = "interrupted"

_INSERT_CHUNK = 5000


async def create_job(
    session: AsyncSession,
    *,
    account_id: int,
    chat_id: int,
    groups: list[list],
    options: dict,
) -> int:
    job = ParseJob(
        account_id=account_id, chat_id=chat_id, groups=groups, options=options
    )
    session.add(job)
    await session.flush()
    return job.id


async def save_group(
    session: AsyncSession,
    *,
    job_id: int,
    group_idx: int,
    usernames: Iterable[str],
    report: list[dict],
    dropped: dict[str, int],
) -> None:
    """Сохраняет usernames группы (идемпотентно) и текущую сводку задания."""
    rows = [
        {"job_id": job_id, "group_idx": group_idx, "username": u}
        for u in usernames
    ]
    for i in range(0, len(rows), _INSERT_CHUNK):
        stmt = pg_insert(ParseResult).values(rows[i : i + _INSERT_CHUNK])
        await session.execute(stmt.on_conflict_do_nothing())
    await session.execute(
        update(ParseJob)
        .where(ParseJob.id == job_id)
        .values(report=report, dropped=dropped)
    )


async def finish_job(
    session: AsyncSession,
    *,
    job_id: int,
    status: str,
    report: list[dict],
    dropped: dict[str, int],
    error: str | None = None,
) -> None:
    await session.execute(
        update(ParseJob)
        .where(ParseJob.id == job_id)
        .values(
            status=status,
            report=report,
            dropped=dropped,
            error=error,
            finished_at=datetime.now(timezone.utc),
        )
    )


async def get_job(session: AsyncSession, job_id: int) -> ParseJob | None:
    return await session.get(ParseJob, job_id)


async def list_recent(session: AsyncSession, *, limit: int = 10) -> list[ParseJob]:
    result = await session.execute(
        select(ParseJob).order_by(desc(ParseJob.id)).limit(limit)
    )
    return list(result.scalars().all())


async def results_by_group(session: AsyncSession, job_id: int) -> dict[int, list[str]]:
    result = await session.execute(
        select(ParseResult.group_idx, ParseResult.username).where(
            ParseResult.job_id == job_id
        )
    )
    grouped: dict[int, list[str]] = defaultdict(list)
    for idx, username in result.all():
        grouped[idx].append(username)
    return dict(grouped)


async def mark_running_interrupted(session: AsyncSession) -> int:
    """При старте: задания, оборванные рестартом, → interrupted. Возвращает число."""
    result = await session.execute(
        update(ParseJob)
        .where(ParseJob.status == STATUS_RUNNING)
        .values(status=STATUS_INTERRUPTED, finished_at=datetime.now(timezone.utc))
    )
    return result.rowcount or 0
