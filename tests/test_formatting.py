"""Тесты единого форматирования сообщений бота (app/bot/formatting.py).

Чистые функции — без БД/IO. Покрывают статус-лейблы аккаунта (включая различение
FloodWait/quiet/spam), карточку кампании, разделитель тысяч и заголовки.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.bot import formatting as fmt
from app.db.models import (
    Account,
    AccountStatus,
    Campaign,
    CampaignStatus,
    CampaignType,
)

NOW = datetime(2026, 6, 10, 12, 0, tzinfo=timezone.utc)


def _acc(
    status: AccountStatus,
    *,
    pause_reason: str | None = None,
    spam_unlock_at: datetime | None = None,
    daily_invited: int = 0,
    daily_sent: int = 0,
    limit_reduced_until: datetime | None = None,
) -> Account:
    a = Account()
    a.id = 1
    a.phone = "+79991234567"
    a.status = status
    a.pause_reason = pause_reason
    a.spam_unlock_at = spam_unlock_at
    a.daily_invited = daily_invited
    a.daily_sent = daily_sent
    a.limit_reduced_until = limit_reduced_until
    return a


def _camp(
    status: CampaignStatus,
    type_: CampaignType,
    *,
    sent: int = 0,
    total: int = 0,
    skipped: int = 0,
    failed: int = 0,
) -> Campaign:
    c = Campaign()
    c.id = 5
    c.status = status
    c.type = type_
    c.sent_count = sent
    c.total_count = total
    c.skipped_count = skipped
    c.failed_count = failed
    return c


# --- num ---


def test_num_thousands_separator() -> None:
    assert fmt.num(120219) == "120 219"
    assert fmt.num(0) == "0"
    assert fmt.num(92) == "92"
    assert fmt.num(1000000) == "1 000 000"


# --- account_status ---


def test_account_status_active() -> None:
    emoji, text = fmt.account_status(_acc(AccountStatus.active), now=NOW)
    assert emoji == "🟢"
    assert text == "активен"


def test_account_status_flood_wait() -> None:
    a = _acc(
        AccountStatus.pause,
        pause_reason="flood_wait",
        spam_unlock_at=NOW + timedelta(minutes=30),
    )
    emoji, text = fmt.account_status(a, now=NOW)
    assert emoji == "⏳"
    assert "FloodWait" in text and "12:30" in text


def test_account_status_quiet_vs_flood() -> None:
    # Та же status=pause, но причина quiet_hours → не FloodWait.
    a = _acc(
        AccountStatus.pause,
        pause_reason="quiet_hours",
        spam_unlock_at=NOW + timedelta(hours=2),
    )
    emoji, text = fmt.account_status(a, now=NOW)
    assert emoji == "🌙"
    assert "тихие часы" in text


def test_account_status_pause_generic() -> None:
    a = _acc(AccountStatus.pause, spam_unlock_at=NOW + timedelta(hours=1))
    emoji, text = fmt.account_status(a, now=NOW)
    assert emoji == "⏸"
    assert "пауза" in text


def test_account_status_spam_blocked() -> None:
    a = _acc(AccountStatus.spam_blocked, spam_unlock_at=NOW + timedelta(hours=5))
    emoji, text = fmt.account_status(a, now=NOW)
    assert emoji == "🚫"
    assert "спам-блок" in text


def test_account_status_other_states() -> None:
    assert fmt.account_status(_acc(AccountStatus.warmup), now=NOW)[0] == "🔥"
    assert fmt.account_status(_acc(AccountStatus.dead), now=NOW)[0] == "⚰️"
    assert fmt.account_status(_acc(AccountStatus.disabled), now=NOW)[0] == "⛔"


# --- account_card ---


def test_account_card_two_lines() -> None:
    a = _acc(AccountStatus.active, daily_invited=22, daily_sent=3)
    card = fmt.account_card(a, now=NOW)
    assert "#1" in card and "+79991234567" in card
    assert "22 инв" in card and "3 DM" in card
    assert "\n" in card


# --- campaign ---


def test_sent_label_by_type() -> None:
    assert fmt.sent_label(_camp(CampaignStatus.running, CampaignType.invite)) == "Приглашено"
    assert fmt.sent_label(_camp(CampaignStatus.running, CampaignType.message)) == "Отправлено"


def test_campaign_card_invite() -> None:
    c = _camp(
        CampaignStatus.running, CampaignType.invite,
        sent=92, total=120219, skipped=224, failed=147,
    )
    card = fmt.campaign_card(c)
    assert "▶️" in card and "идёт" in card and "инвайт" in card
    assert "Приглашено: 92 из 120 219 (0%)" in card
    assert "Пропущено: 224" in card and "Ошибки: 147" in card


def test_campaign_card_done_message() -> None:
    c = _camp(CampaignStatus.done, CampaignType.message, sent=50, total=100)
    card = fmt.campaign_card(c)
    assert "✅" in card and "завершена" in card and "рассылка" in card
    assert "Отправлено: 50 из 100 (50%)" in card


# --- section_header / fmt_dt ---


def test_section_header() -> None:
    assert fmt.section_header("👤", "Аккаунты", "4 активных") == "👤 <b>Аккаунты</b> — 4 активных"
    assert fmt.section_header("📢", "Кампании") == "📢 <b>Кампании</b>"


def test_fmt_dt_today_other_none() -> None:
    assert fmt.fmt_dt(NOW.replace(hour=23, minute=45), now=NOW) == "23:45"
    other = NOW + timedelta(days=1)
    assert fmt.fmt_dt(other, now=NOW) == f"{other:%d.%m %H:%M}"
    assert fmt.fmt_dt(None) == "—"
