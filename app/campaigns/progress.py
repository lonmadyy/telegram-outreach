"""Агрегация и форматирование прогресса кампаний. ARCHITECTURE.md §10.5.

Используется scheduler-задачей `progress_notify_job` (раз в 30 мин) и может
переиспользоваться в /status. Сводка отправляется админу через notify_admin.

`format_progress` и `estimate_eta` — чистые функции (тестируются без БД).
`build_summary` собирает данные из БД и возвращает None, если активных
(running) кампаний нет — в простое бот молчит (§10.5, MVP-5).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Account, CampaignType
from app.db.repositories import campaigns as campaigns_repo

_TYPE_LABELS = {
    CampaignType.message: "рассылка",
    CampaignType.invite: "инвайт",
}


@dataclass(frozen=True)
class CampaignProgress:
    id: int
    type_label: str
    status: str
    total: int
    sent: int
    skipped: int
    failed: int
    eta: str


def _humanize_duration(seconds: float) -> str:
    minutes = int(seconds // 60)
    if minutes < 1:
        return "<1мин"
    hours, mins = divmod(minutes, 60)
    if hours <= 0:
        return f"{mins}мин"
    return f"{hours}ч {mins}мин"


def estimate_eta(
    total: int, done: int, started_at: datetime | None, now: datetime
) -> str:
    """Грубая оценка времени до завершения по средней скорости с `started_at`.

    Приблизительно: на старте/паузах может врать. «—» если данных недостаточно.
    """
    if total <= 0 or done <= 0:
        return "—"
    remaining = total - done
    if remaining <= 0:
        return "почти готово"
    if started_at is None:
        return "—"
    elapsed = (now - started_at).total_seconds()
    if elapsed <= 0:
        return "—"
    rate = done / elapsed  # задач/сек
    if rate <= 0:
        return "—"
    return "~" + _humanize_duration(remaining / rate)


def format_progress(
    campaigns: list[CampaignProgress], account_status_counts: dict[str, int]
) -> str:
    """Чистое форматирование сводки §10.5."""
    lines: list[str] = []
    for c in campaigns:
        processed = c.sent + c.skipped + c.failed
        pct = int(processed / c.total * 100) if c.total else 0
        lines.append(f"<b>Кампания #{c.id}</b> ({c.type_label}) — {c.status}")
        lines.append(f"Прогресс: {processed}/{c.total} ({pct}%)")
        lines.append(
            f"Успешно: {c.sent} | Пропущено: {c.skipped} | Ошибок: {c.failed}"
        )
        if c.eta and c.eta != "—":
            lines.append(f"ETA: {c.eta}")
        lines.append("")

    if account_status_counts:
        parts = [
            f"{count} {status}"
            for status, count in sorted(account_status_counts.items())
        ]
        lines.append("Аккаунты: " + ", ".join(parts))

    return "\n".join(lines).strip()


async def build_summary(session: AsyncSession) -> str | None:
    """Сводка по running-кампаниям + статусам аккаунтов. None если активных нет."""
    campaigns = await campaigns_repo.list_running(session)
    if not campaigns:
        return None

    now = datetime.now(timezone.utc)
    cps: list[CampaignProgress] = []
    for c in campaigns:
        processed = c.sent_count + c.skipped_count + c.failed_count
        cps.append(
            CampaignProgress(
                id=c.id,
                type_label=_TYPE_LABELS.get(c.type, c.type.value),
                status=c.status.value,
                total=c.total_count,
                sent=c.sent_count,
                skipped=c.skipped_count,
                failed=c.failed_count,
                eta=estimate_eta(c.total_count, processed, c.started_at, now),
            )
        )

    result = await session.execute(select(Account))
    counts: dict[str, int] = {}
    for acc in result.scalars().all():
        counts[acc.status.value] = counts.get(acc.status.value, 0) + 1

    return format_progress(cps, counts)
