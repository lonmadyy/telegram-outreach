"""Тесты can_send_today + effective_daily_limits. ARCHITECTURE.md §5.1."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.db.models import Account, AccountStatus, CampaignType
from app.db.repositories.accounts import (
    can_send_today,
    effective_daily_limits,
    is_in_warmup,
    is_limit_reduced,
    warmup_age_limits,
)

DM_WARM = 40
INVITE_WARM = 100
DM_FRESH = 10
INVITE_FRESH = 5


def make_account(
    *,
    hours_old: float = 100,
    in_warmup: bool = False,
    reduced: bool = False,
    daily_sent: int = 0,
    daily_invited: int = 0,
    status: AccountStatus = AccountStatus.active,
) -> Account:
    now = datetime.now(timezone.utc)
    acc = Account()
    acc.id = 1
    acc.phone = "+79991234567"
    acc.session_path = "/tmp/sess"
    acc.status = status
    acc.created_at = now - timedelta(hours=hours_old)
    acc.daily_sent = daily_sent
    acc.daily_invited = daily_invited
    acc.warmup_until = (now + timedelta(hours=48 - hours_old)) if in_warmup else None
    acc.limit_reduced_until = (now + timedelta(days=7)) if reduced else None
    return acc


# --- warmup_age_limits ---


def test_warmup_first_12h_zero() -> None:
    dm, inv = warmup_age_limits(
        hours_since_created=2,
        dm_warm=DM_WARM, invite_warm=INVITE_WARM,
        dm_fresh=DM_FRESH, invite_fresh=INVITE_FRESH,
    )
    assert (dm, inv) == (0, 0)


def test_warmup_12_24h_three_dm_no_invite() -> None:
    dm, inv = warmup_age_limits(
        hours_since_created=15,
        dm_warm=DM_WARM, invite_warm=INVITE_WARM,
        dm_fresh=DM_FRESH, invite_fresh=INVITE_FRESH,
    )
    assert (dm, inv) == (3, 0)


def test_warmup_24_48h_fresh_limits() -> None:
    dm, inv = warmup_age_limits(
        hours_since_created=36,
        dm_warm=DM_WARM, invite_warm=INVITE_WARM,
        dm_fresh=DM_FRESH, invite_fresh=INVITE_FRESH,
    )
    assert (dm, inv) == (DM_FRESH, INVITE_FRESH)


def test_warmup_after_48h_full_limits() -> None:
    dm, inv = warmup_age_limits(
        hours_since_created=100,
        dm_warm=DM_WARM, invite_warm=INVITE_WARM,
        dm_fresh=DM_FRESH, invite_fresh=INVITE_FRESH,
    )
    assert (dm, inv) == (DM_WARM, INVITE_WARM)


# --- effective_daily_limits с warmup ---


def test_effective_limits_in_warmup_24_48h() -> None:
    acc = make_account(hours_old=30, in_warmup=True)
    dm, inv = effective_daily_limits(
        acc,
        dm_warm=DM_WARM, invite_warm=INVITE_WARM,
        dm_fresh=DM_FRESH, invite_fresh=INVITE_FRESH,
    )
    assert (dm, inv) == (10, 5)


def test_effective_limits_active_account_full() -> None:
    acc = make_account(hours_old=200)
    dm, inv = effective_daily_limits(
        acc,
        dm_warm=DM_WARM, invite_warm=INVITE_WARM,
        dm_fresh=DM_FRESH, invite_fresh=INVITE_FRESH,
    )
    assert (dm, inv) == (40, 100)


def test_effective_limits_with_reduction_75_percent() -> None:
    acc = make_account(hours_old=200, reduced=True)
    dm, inv = effective_daily_limits(
        acc,
        dm_warm=DM_WARM, invite_warm=INVITE_WARM,
        dm_fresh=DM_FRESH, invite_fresh=INVITE_FRESH,
    )
    # ceil(40 * 0.75) = 30, ceil(100 * 0.75) = 75
    assert (dm, inv) == (30, 75)


def test_effective_limits_warmup_with_reduction_compounds() -> None:
    # Warmup 24-48h → 10/5. С 75% → ceil(10*0.75)=8, ceil(5*0.75)=4.
    acc = make_account(hours_old=30, in_warmup=True, reduced=True)
    dm, inv = effective_daily_limits(
        acc,
        dm_warm=DM_WARM, invite_warm=INVITE_WARM,
        dm_fresh=DM_FRESH, invite_fresh=INVITE_FRESH,
    )
    assert (dm, inv) == (8, 4)


# --- can_send_today ---


def test_can_send_disabled_account_returns_false() -> None:
    acc = make_account(status=AccountStatus.disabled)
    assert not can_send_today(
        acc, action_type=CampaignType.message,
        dm_warm=DM_WARM, invite_warm=INVITE_WARM,
        dm_fresh=DM_FRESH, invite_fresh=INVITE_FRESH,
    )


def test_can_send_dead_account_returns_false() -> None:
    acc = make_account(status=AccountStatus.dead)
    assert not can_send_today(
        acc, action_type=CampaignType.message,
        dm_warm=DM_WARM, invite_warm=INVITE_WARM,
        dm_fresh=DM_FRESH, invite_fresh=INVITE_FRESH,
    )


def test_can_send_active_under_limit() -> None:
    acc = make_account(daily_sent=20)
    assert can_send_today(
        acc, action_type=CampaignType.message,
        dm_warm=DM_WARM, invite_warm=INVITE_WARM,
        dm_fresh=DM_FRESH, invite_fresh=INVITE_FRESH,
    )


def test_can_send_active_at_limit_blocks() -> None:
    acc = make_account(daily_sent=40)
    assert not can_send_today(
        acc, action_type=CampaignType.message,
        dm_warm=DM_WARM, invite_warm=INVITE_WARM,
        dm_fresh=DM_FRESH, invite_fresh=INVITE_FRESH,
    )


def test_can_send_invite_uses_invite_counter() -> None:
    acc = make_account(daily_sent=40, daily_invited=50)  # DM лимит исчерпан
    assert can_send_today(
        acc, action_type=CampaignType.invite,
        dm_warm=DM_WARM, invite_warm=INVITE_WARM,
        dm_fresh=DM_FRESH, invite_fresh=INVITE_FRESH,
    )


def test_can_send_after_peerflood_uses_75_percent() -> None:
    # 30 = ceil(40*0.75). Если уже отправлено 30 — больше нельзя.
    acc = make_account(reduced=True, daily_sent=30)
    assert not can_send_today(
        acc, action_type=CampaignType.message,
        dm_warm=DM_WARM, invite_warm=INVITE_WARM,
        dm_fresh=DM_FRESH, invite_fresh=INVITE_FRESH,
    )
    acc.daily_sent = 29
    assert can_send_today(
        acc, action_type=CampaignType.message,
        dm_warm=DM_WARM, invite_warm=INVITE_WARM,
        dm_fresh=DM_FRESH, invite_fresh=INVITE_FRESH,
    )
