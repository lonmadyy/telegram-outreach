"""Итог задания парсинга: файлы и сводка из БД. ARCHITECTURE.md §20.3.

Отсев по реестру processed_clients делается при выдаче, а не при сборе: так файл,
полученный позже через /parses, учитывает и тех, кому успели написать с тех пор.
"""

from __future__ import annotations

from app.db.models import ParseJob
from app.db.repositories import parses as parses_repo
from app.db.repositories import processed as processed_repo
from app.db.session import session_scope
from app.parsing.collector import ParseOutputs, build_outputs
from app.utils.group_targets import GroupToken, token_label


def group_title(job: ParseJob, idx: int) -> str:
    """Название группы для файла/сводки: из отчёта, иначе исходная строка."""
    if idx - 1 < len(job.report or []):
        entry = job.report[idx - 1]
        if entry.get("title"):
            return entry["title"]
        return entry.get("label") or f"group{idx}"
    kind, value = job.groups[idx - 1]
    return token_label(GroupToken(kind, value))


async def load_outputs(job_id: int) -> tuple[ParseJob, ParseOutputs] | None:
    async with session_scope() as session:
        job = await parses_repo.get_job(session, job_id)
        if job is None:
            return None
        by_group = await parses_repo.results_by_group(session, job_id)
        everyone = sorted({u for names in by_group.values() for u in names})
        skip = await processed_repo.already_processed_usernames(
            session, usernames=everyone, resend_old=False
        )
    done_groups = sorted(set(by_group) | set(range(1, len(job.report or []) + 1)))
    groups = [(group_title(job, i), by_group.get(i, [])) for i in done_groups]
    return job, build_outputs(groups, skip=skip)
