"""Тесты для utils/time.py — quiet hours. ARCHITECTURE.md §5.3."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from app.utils.time import (
    is_in_quiet_hours,
    next_quiet_end_at,
    seconds_until_midnight_utc,
)

MINSK = ZoneInfo("Europe/Minsk")


def _minsk(hh: int, mm: int = 0) -> datetime:
    """28 мая 2026, Europe/Minsk, заданное время."""
    return datetime(2026, 5, 28, hh, mm, tzinfo=MINSK)


# --- is_in_quiet_hours: дневное окно (наш дефолт 01:00-07:00) ---


def test_inside_window_simple() -> None:
    assert is_in_quiet_hours(now=_minsk(2, 30))


def test_at_start_boundary_inside() -> None:
    assert is_in_quiet_hours(now=_minsk(1, 0))


def test_just_before_start_outside() -> None:
    assert not is_in_quiet_hours(now=_minsk(0, 59))


def test_at_end_boundary_outside() -> None:
    assert not is_in_quiet_hours(now=_minsk(7, 0))


def test_just_before_end_inside() -> None:
    assert is_in_quiet_hours(now=_minsk(6, 59))


def test_outside_window_afternoon() -> None:
    assert not is_in_quiet_hours(now=_minsk(15, 0))


# --- is_in_quiet_hours: окно через полночь (23:00-06:00) ---


def test_overnight_inside_before_midnight() -> None:
    assert is_in_quiet_hours(
        now=_minsk(23, 30), quiet_start="23:00", quiet_end="06:00"
    )


def test_overnight_inside_after_midnight() -> None:
    assert is_in_quiet_hours(
        now=_minsk(3, 0), quiet_start="23:00", quiet_end="06:00"
    )


def test_overnight_outside() -> None:
    assert not is_in_quiet_hours(
        now=_minsk(12, 0), quiet_start="23:00", quiet_end="06:00"
    )


# --- next_quiet_end_at ---


def test_next_end_when_inside_window() -> None:
    # 02:30 Minsk → end 07:00 сегодня (с учётом jitter 0).
    res = next_quiet_end_at(now=_minsk(2, 30), jitter_seconds=0)
    res_minsk = res.astimezone(MINSK)
    assert res_minsk.hour == 7 and res_minsk.minute == 0
    assert res_minsk.date() == _minsk(2, 30).date()


def test_next_end_when_outside_after_end() -> None:
    # 12:00 Minsk → end 07:00 следующего дня.
    res = next_quiet_end_at(now=_minsk(12, 0), jitter_seconds=0)
    res_minsk = res.astimezone(MINSK)
    assert res_minsk.hour == 7
    assert res_minsk.date() == (_minsk(12, 0) + timedelta(days=1)).date()


def test_next_end_jitter_within_15_min() -> None:
    # Без передачи jitter_seconds — случайное значение 0..900.
    base = next_quiet_end_at(now=_minsk(2, 30), jitter_seconds=0)
    with_jitter = next_quiet_end_at(now=_minsk(2, 30), jitter_seconds=900)
    diff = (with_jitter - base).total_seconds()
    assert 0 <= diff <= 900


def test_next_end_returns_utc_aware() -> None:
    res = next_quiet_end_at(now=_minsk(2, 30), jitter_seconds=0)
    assert res.tzinfo is not None
    assert res.utcoffset() == timedelta(0)


# --- seconds_until_midnight_utc ---


def test_midnight_utc_24h_when_just_passed_midnight() -> None:
    now = datetime(2026, 5, 28, 0, 0, 1, tzinfo=timezone.utc)
    s = seconds_until_midnight_utc(now=now)
    # ~24h - 1s
    assert 24 * 3600 - 60 <= s <= 24 * 3600


def test_midnight_utc_one_minute_when_almost_there() -> None:
    now = datetime(2026, 5, 28, 23, 59, 0, tzinfo=timezone.utc)
    s = seconds_until_midnight_utc(now=now)
    assert 0 <= s <= 60
