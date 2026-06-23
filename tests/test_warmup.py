"""Юнит-тесты warmup-плана по возрасту аккаунта. ARCHITECTURE.md §5.1.

Чистая логика (warmup_actions_for_age) без БД/Telethon.
"""

from __future__ import annotations

import pytest

from app.telegram.warmup import warmup_actions_for_age


@pytest.mark.parametrize(
    "hours,in_warmup,subscribe,presence,saved",
    [
        (0.5, True, False, True, False),   # 0-2ч: только онлайн
        (1.0, True, False, True, False),
        (2.0, True, True, True, False),    # >=2ч: можно подписку
        (6.0, True, True, True, False),
        (12.0, True, True, True, True),    # >=12ч: можно Saved Messages
        (23.0, True, True, True, True),
        (24.0, True, True, True, True),    # 24-48ч: всё разрешено
        (47.9, True, True, True, True),
    ],
)
def test_warmup_plan_within_warmup(hours, in_warmup, subscribe, presence, saved):
    plan = warmup_actions_for_age(hours)
    assert plan.in_warmup is in_warmup
    assert plan.allow_subscribe is subscribe
    assert plan.allow_presence is presence
    assert plan.allow_saved is saved


@pytest.mark.parametrize("hours", [48.0, 48.1, 100.0, 10_000.0])
def test_warmup_finished_after_48h(hours):
    plan = warmup_actions_for_age(hours)
    assert plan.in_warmup is False
    assert plan.allow_subscribe is False
    assert plan.allow_presence is False
    assert plan.allow_saved is False


@pytest.mark.parametrize(
    "hours,total,in_warmup",
    [
        (20.0, 24, True),    # короткий warmup (24ч): на 20ч ещё в прогреве
        (24.0, 24, False),   # ровно total_hours → прогрев завершён
        (25.0, 24, False),
        (47.0, 72, True),    # длинный warmup (72ч): на 47ч ещё в прогреве (не 48)
        (72.0, 72, False),
    ],
)
def test_warmup_total_hours_param(hours, total, in_warmup):
    """total_hours переопределяет порог завершения warmup (управляется /set
    warmup_duration_hours через settings_cache, §4.9)."""
    plan = warmup_actions_for_age(hours, total_hours=total)
    assert plan.in_warmup is in_warmup
