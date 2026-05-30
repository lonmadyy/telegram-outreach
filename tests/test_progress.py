"""Юнит-тесты прогресс-сводки. ARCHITECTURE.md §10.5.

Чистые функции estimate_eta / format_progress без БД.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.campaigns.progress import (
    CampaignProgress,
    estimate_eta,
    format_progress,
)

NOW = datetime(2026, 5, 29, 12, 0, tzinfo=timezone.utc)


# --- estimate_eta ---


def test_eta_no_total():
    assert estimate_eta(0, 0, NOW - timedelta(hours=1), NOW) == "—"


def test_eta_nothing_done():
    assert estimate_eta(100, 0, NOW - timedelta(hours=1), NOW) == "—"


def test_eta_no_started_at():
    assert estimate_eta(100, 10, None, NOW) == "—"


def test_eta_zero_elapsed():
    assert estimate_eta(100, 10, NOW, NOW) == "—"


def test_eta_done_ge_total():
    assert estimate_eta(100, 100, NOW - timedelta(hours=1), NOW) == "почти готово"


def test_eta_normal_calc():
    # 10 задач за 1000с → 0.01/с; осталось 90 → 9000с = 150 мин = 2ч 30мин.
    started = NOW - timedelta(seconds=1000)
    assert estimate_eta(100, 10, started, NOW) == "~2ч 30мин"


def test_eta_minutes_only():
    # 50 за 600с → осталось 50 → 600с = 10мин.
    started = NOW - timedelta(seconds=600)
    assert estimate_eta(100, 50, started, NOW) == "~10мин"


# --- format_progress ---


def _cp(**kw) -> CampaignProgress:
    base = dict(
        id=5, type_label="рассылка", status="running",
        total=1109, sent=198, skipped=28, failed=8, eta="~9ч 30мин",
    )
    base.update(kw)
    return CampaignProgress(**base)


def test_format_progress_basic():
    text = format_progress([_cp()], {"active": 4, "pause": 1})
    assert "Кампания #5" in text
    assert "рассылка" in text
    # processed = 198+28+8 = 234; 234/1109 = 21%
    assert "234/1109 (21%)" in text
    assert "Успешно: 198 | Пропущено: 28 | Ошибок: 8" in text
    assert "ETA: ~9ч 30мин" in text
    assert "Аккаунты: 4 active, 1 pause" in text


def test_format_progress_hides_eta_dash():
    text = format_progress([_cp(eta="—")], {})
    assert "ETA" not in text


def test_format_progress_no_accounts_line_when_empty():
    text = format_progress([_cp()], {})
    assert "Аккаунты:" not in text


def test_format_progress_zero_total_no_div_error():
    text = format_progress([_cp(total=0, sent=0, skipped=0, failed=0)], {})
    assert "0/0 (0%)" in text
