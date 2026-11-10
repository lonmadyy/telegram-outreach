"""CSV-отчёт по групповой рассылке. ARCHITECTURE.md §22.9.

Тот же формат, что у отчёта кампании (app/campaigns/reporting.py): `;`, BOM для Excel.
"""

from __future__ import annotations

import csv
import io
from datetime import datetime

from app.groups.formatting import STATE, ref_label

HEADER = [
    "id",
    "группа",
    "название",
    "аккаунт",
    "статус",
    "причина",
    "постов",
    "последний пост (UTC)",
    "следующий (UTC)",
    "свой период, мин",
    "slowmode, с",
]


def _dt(v: datetime | None) -> str:
    return v.strftime("%Y-%m-%d %H:%M") if v else ""


def build_targets_csv(targets, *, delimiter: str = ";") -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=delimiter)
    w.writerow(HEADER)
    for t in targets:
        period = (
            f"{t.period_min}-{t.period_max}" if t.period_min is not None else ""
        )
        w.writerow(
            [
                t.id,
                ref_label(t.ref_kind, t.ref_value),
                t.title or "",
                t.account_id or "",
                STATE.get(t.state, ("", t.state))[1],
                t.state_reason or "",
                t.posts_count,
                _dt(t.last_post_at),
                _dt(t.next_at),
                period,
                t.slowmode_sec or "",
            ]
        )
    return ("﻿" + buf.getvalue()).encode("utf-8")


def report_filename(campaign_id: int, now: datetime) -> str:
    return f"group_campaign_{campaign_id}_{now:%Y%m%d_%H%M}.csv"
