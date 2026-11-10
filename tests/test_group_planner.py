"""Распределение, расписание и окно групповой рассылки (app/groups/planner.py, §22)."""

import random
from datetime import datetime, timedelta, timezone

import pytest

from app.groups.lines import GroupEntry
from app.groups.planner import (
    assign_targets,
    effective_period,
    estimate_rounds,
    first_post_at,
    gap_seconds,
    in_window,
    next_post_at,
    parse_minutes_range,
    parse_window,
    rebalance_orphans,
    take_for_new_account,
)
from app.utils.group_targets import GroupToken

NOW = datetime(2026, 11, 11, 12, 0, tzinfo=timezone.utc)  # 15:00 по Минску


def _e(name, acc=None):
    return GroupEntry(GroupToken("username", name), None, acc)


def test_even_round_robin():
    entries = [_e(f"g{i}") for i in range(5)]
    out, problems = assign_targets(entries, [1, 2], "even")
    assert problems == []
    per = {}
    for a in out:
        per.setdefault(a.account_id, []).append(a.entry.label)
    assert len(per[1]) == 3 and len(per[2]) == 2


def test_even_ignores_tags():
    out, _ = assign_targets([_e("a", 9), _e("b", 9)], [1, 2], "even")
    assert sorted(a.account_id for a in out) == [1, 2]


def test_all_mode_product():
    out, problems = assign_targets([_e("a"), _e("b")], [1, 2, 3], "all")
    assert len(out) == 6 and problems == []


def test_manual_tags_and_rest_even():
    entries = [_e("a", 2), _e("b", 2), _e("c"), _e("d"), _e("x", 7)]
    out, problems = assign_targets(entries, [1, 2], "manual")
    by = {a.entry.label: a.account_id for a in out}
    assert by["@a"] == 2 and by["@b"] == 2
    # Свободные строки уходят к менее загруженному (#1).
    assert by["@c"] == 1 and by["@d"] == 1
    assert "@x" not in by
    assert problems and "#7" in problems[0]


def test_no_accounts():
    out, problems = assign_targets([_e("a")], [], "even")
    assert out == [] and problems


def test_bad_mode():
    with pytest.raises(ValueError):
        assign_targets([_e("a")], [1], "weird")


def test_rebalance_orphans_least_loaded():
    out = rebalance_orphans([10, 11, 12], {1: 5, 2: 3})
    assert out == {10: 2, 11: 2, 12: 1}
    assert rebalance_orphans([1], {}) == {}


def test_take_for_new_account_only_new_targets():
    # #1: 6 целей (из них 4 new), #2: 2 цели (все new). Новый аккаунт догоняет среднее.
    taken = take_for_new_account({1: [101, 102, 103, 104], 2: [201, 202]}, {1: 6, 2: 2})
    assert 2 <= len(taken) <= 3
    assert all(t in (101, 102, 103, 104) for t in taken)  # берём у перегруженного


def test_take_for_new_account_no_new():
    assert take_for_new_account({1: []}, {1: 10}) == []


def test_effective_period_override():
    assert effective_period(30, 200, (None, None)) == (30, 200)
    assert effective_period(30, 200, (60, 90)) == (60, 90)


def test_next_post_in_period_and_slowmode():
    rng = random.Random(1)
    for _ in range(50):
        t = next_post_at(NOW, (30, 200), rng=rng)
        assert NOW + timedelta(minutes=30) <= t <= NOW + timedelta(minutes=200)
    t = next_post_at(NOW, (5, 5), slowmode_sec=3600, rng=rng)
    assert t == NOW + timedelta(seconds=3600)


def test_first_post():
    rng = random.Random(2)
    t = first_post_at(NOW, joined_by_us=True, period=(30, 200), rng=rng)
    assert NOW + timedelta(minutes=5) <= t <= NOW + timedelta(minutes=20)
    t = first_post_at(NOW, joined_by_us=False, period=(30, 200), rng=rng)
    assert NOW <= t <= NOW + timedelta(minutes=30)


def test_gap_seconds_sorted_bounds():
    rng = random.Random(3)
    assert 120 <= gap_seconds(600, 120, rng=rng) <= 600


def test_window():
    tz = "Europe/Minsk"
    assert in_window(NOW, None, None, tz) is True
    assert in_window(NOW, "09:00", "22:00", tz) is True       # 15:00
    assert in_window(NOW, "16:00", "22:00", tz) is False
    assert in_window(NOW, "22:00", "16:00", tz) is True       # через полночь
    assert in_window(NOW, "10:00", "10:00", tz) is True       # пустое — не ограничивает


def test_parse_minutes_range():
    assert parse_minutes_range("30-200", lo_limit=5, hi_limit=10080) == (30, 200)
    assert parse_minutes_range("30 – 200", lo_limit=5, hi_limit=10080) == (30, 200)
    assert parse_minutes_range("45", lo_limit=5, hi_limit=10080) == (45, 45)
    assert parse_minutes_range("200-30", lo_limit=5, hi_limit=10080) is None
    assert parse_minutes_range("2", lo_limit=5, hi_limit=10080) is None
    assert parse_minutes_range("abc", lo_limit=5, hi_limit=10080) is None


def test_parse_window():
    assert parse_window("09:00-22:00") == ("09:00", "22:00")
    assert parse_window("9:00 — 22:30") == ("09:00", "22:30")
    assert parse_window("нет") is None
    assert parse_window("25:00-10:00") is False
    assert parse_window("10:00-10:00") is False
    assert parse_window("утром") is False


def test_estimate_rounds():
    est = estimate_rounds({1: 10, 2: 40}, gap_min_sec=120, gap_max_sec=600, period_min=30)
    by = {e.account_id: e for e in est}
    assert by[1].round_minutes == pytest.approx(60.0)
    assert by[1].slower_than_period is True
    assert by[2].groups == 40
    small = estimate_rounds({1: 3}, gap_min_sec=120, gap_max_sec=600, period_min=30)
    assert small[0].slower_than_period is False
