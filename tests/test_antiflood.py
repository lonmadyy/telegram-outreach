"""Юнит-тесты антифлуд-логики §5.3. Чистые функции + горячее состояние, без БД."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.telegram import antiflood


@pytest.fixture(autouse=True)
def _reset():
    antiflood.reset()
    yield
    antiflood.reset()


# --- is_global_flood ---


def test_global_flood_all_park_flooded():
    assert antiflood.is_global_flood({1, 2, 3}, {1, 2, 3}) is True


def test_global_flood_superset_flooded():
    # зафлудивших больше парка (напр. бывший аккаунт) — парк всё равно весь покрыт
    assert antiflood.is_global_flood({1, 2, 3, 9}, {1, 2, 3}) is True


def test_global_flood_partial_is_false():
    assert antiflood.is_global_flood({1, 2}, {1, 2, 3}) is False


def test_global_flood_empty_park_is_false():
    assert antiflood.is_global_flood({1, 2}, set()) is False


def test_global_flood_empty_flooded_is_false():
    assert antiflood.is_global_flood(set(), {1, 2}) is False


# --- is_adaptive_active / set_adaptive_until ---


def test_adaptive_inactive_by_default():
    now = datetime(2026, 5, 31, tzinfo=timezone.utc)
    assert antiflood.is_adaptive_active(now) is False


def test_adaptive_active_until_future_then_expires():
    now = datetime(2026, 5, 31, 12, 0, tzinfo=timezone.utc)
    antiflood.set_adaptive_until(now + timedelta(minutes=30))
    assert antiflood.is_adaptive_active(now) is True
    assert antiflood.is_adaptive_active(now + timedelta(hours=1)) is False


def test_adaptive_cleared_with_none():
    now = datetime(2026, 5, 31, tzinfo=timezone.utc)
    antiflood.set_adaptive_until(now + timedelta(hours=1))
    antiflood.set_adaptive_until(None)
    assert antiflood.is_adaptive_active(now) is False


# --- should_auto_resume ---


@pytest.mark.parametrize(
    "recovered,paused,expected",
    [
        (True, True, True),
        (True, False, False),
        (False, True, False),
        (False, False, False),
    ],
)
def test_should_auto_resume(recovered, paused, expected):
    assert (
        antiflood.should_auto_resume(
            has_recovered_account=recovered, has_flood_paused_campaigns=paused
        )
        is expected
    )


# --- pick_interval ---


def test_pick_interval_normal():
    assert antiflood.pick_interval(
        adaptive=False, normal=(300, 540), flood=(450, 720)
    ) == (300, 540)


def test_pick_interval_adaptive():
    assert antiflood.pick_interval(
        adaptive=True, normal=(300, 540), flood=(450, 720)
    ) == (450, 720)
