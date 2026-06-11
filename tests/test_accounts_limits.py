"""Тесты can_send_today + effective_daily_limits. ARCHITECTURE.md §5.1."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.db.models import Account, AccountStatus, CampaignType
from app.db.repositories.accounts import (
    can_send_today,
    effective_daily_limits,
    is_flood_waiting,
    is_in_warmup,
    is_limit_reduced,
    is_pause_expired,
    is_restricted,
    is_spam_line_restricted,
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
    spam_unlock_at: datetime | None = None,
    pause_reason: str | None = None,
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
    acc.spam_unlock_at = spam_unlock_at
    acc.pause_reason = pause_reason
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


# --- is_pause_expired (возврат active после истёкшей паузы) ---


def test_pause_expired_past_unlock_true() -> None:
    now = datetime.now(timezone.utc)
    acc = make_account(
        status=AccountStatus.pause, spam_unlock_at=now - timedelta(hours=1)
    )
    assert is_pause_expired(acc) is True


def test_pause_expired_none_unlock_true() -> None:
    acc = make_account(status=AccountStatus.pause, spam_unlock_at=None)
    assert is_pause_expired(acc) is True


def test_pause_future_unlock_false() -> None:
    now = datetime.now(timezone.utc)
    acc = make_account(
        status=AccountStatus.pause, spam_unlock_at=now + timedelta(hours=1)
    )
    assert is_pause_expired(acc) is False


def test_pause_expired_active_status_false() -> None:
    assert is_pause_expired(make_account(status=AccountStatus.active)) is False


def test_pause_expired_spam_blocked_false() -> None:
    # spam_blocked не снимаем по таймеру даже с истёкшим unlock (только SpamBot §6.5).
    now = datetime.now(timezone.utc)
    acc = make_account(
        status=AccountStatus.spam_blocked, spam_unlock_at=now - timedelta(hours=1)
    )
    assert is_pause_expired(acc) is False


# --- is_restricted (есть ли действующее ограничение) ---


def test_restricted_pause_true() -> None:
    assert is_restricted(make_account(status=AccountStatus.pause)) is True


def test_restricted_spam_blocked_true() -> None:
    assert is_restricted(make_account(status=AccountStatus.spam_blocked)) is True


def test_restricted_active_clean_false() -> None:
    assert is_restricted(make_account(status=AccountStatus.active)) is False


def test_restricted_active_with_unlock_true() -> None:
    now = datetime.now(timezone.utc)
    acc = make_account(
        status=AccountStatus.active, spam_unlock_at=now + timedelta(hours=1)
    )
    assert is_restricted(acc) is True


def test_restricted_active_with_reduced_true() -> None:
    assert is_restricted(make_account(status=AccountStatus.active, reduced=True)) is True


# --- is_spam_line_restricted (что снимает SpamBot no_limits, фикс пинг-понга) ---


def test_spam_line_flood_wait_pause_not_lifted() -> None:
    # FloodWait-пауза: SpamBot её НЕ снимает (свой таймер, §6.3) — иначе пинг-понг.
    now = datetime.now(timezone.utc)
    acc = make_account(
        status=AccountStatus.pause,
        pause_reason="flood_wait",
        spam_unlock_at=now + timedelta(minutes=30),
    )
    assert is_spam_line_restricted(acc) is False


def test_spam_line_quiet_pause_not_lifted() -> None:
    # Ночная quiet-пауза: SpamBot её НЕ снимает (наше расписание, §5.3).
    now = datetime.now(timezone.utc)
    acc = make_account(
        status=AccountStatus.pause,
        pause_reason="quiet_hours",
        spam_unlock_at=now + timedelta(hours=6),
    )
    assert is_spam_line_restricted(acc) is False


def test_spam_line_legacy_null_reason_lifted() -> None:
    # pause без причины (legacy/неизвестно) — снимается, как раньше (safe fallback).
    acc = make_account(status=AccountStatus.pause, pause_reason=None)
    assert is_spam_line_restricted(acc) is True


def test_spam_line_spam_blocked_lifted() -> None:
    # PeerFlood-карантин/temporary — это спам-линия, SpamBot снимает досрочно (§7.4).
    now = datetime.now(timezone.utc)
    acc = make_account(
        status=AccountStatus.spam_blocked, spam_unlock_at=now + timedelta(hours=12)
    )
    assert is_spam_line_restricted(acc) is True


def test_spam_line_active_with_reduced_lifted() -> None:
    assert is_spam_line_restricted(
        make_account(status=AccountStatus.active, reduced=True)
    ) is True


def test_spam_line_active_with_unlock_lifted() -> None:
    now = datetime.now(timezone.utc)
    acc = make_account(
        status=AccountStatus.active, spam_unlock_at=now + timedelta(hours=1)
    )
    assert is_spam_line_restricted(acc) is True


def test_spam_line_active_clean_false() -> None:
    assert is_spam_line_restricted(make_account(status=AccountStatus.active)) is False


# --- is_flood_waiting (FloodWait-пауза отдельно от quiet/spam) ---


def test_flood_waiting_future_unlock_true() -> None:
    now = datetime.now(timezone.utc)
    acc = make_account(
        status=AccountStatus.pause,
        pause_reason="flood_wait",
        spam_unlock_at=now + timedelta(minutes=10),
    )
    assert is_flood_waiting(acc) is True


def test_flood_waiting_quiet_pause_false() -> None:
    # Та же status=pause, но причина quiet_hours — НЕ FloodWait.
    now = datetime.now(timezone.utc)
    acc = make_account(
        status=AccountStatus.pause,
        pause_reason="quiet_hours",
        spam_unlock_at=now + timedelta(hours=2),
    )
    assert is_flood_waiting(acc) is False


def test_flood_waiting_spam_blocked_false() -> None:
    now = datetime.now(timezone.utc)
    acc = make_account(
        status=AccountStatus.spam_blocked, spam_unlock_at=now + timedelta(hours=2)
    )
    assert is_flood_waiting(acc) is False


def test_flood_waiting_active_false() -> None:
    assert is_flood_waiting(make_account(status=AccountStatus.active)) is False


def test_flood_waiting_expired_unlock_false() -> None:
    # Окно FloodWait истекло — больше не «в FloodWait» (воркер вернёт в active).
    now = datetime.now(timezone.utc)
    acc = make_account(
        status=AccountStatus.pause,
        pause_reason="flood_wait",
        spam_unlock_at=now - timedelta(minutes=1),
    )
    assert is_flood_waiting(acc) is False
