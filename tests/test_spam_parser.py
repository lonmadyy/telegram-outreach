"""Юнит-тесты парсера ответов @SpamBot. ARCHITECTURE.md §7.3.

Включает реальные ответы из логов, на которых парсер ранее ошибался.
Чистая функция parse_spambot_response, без БД/Telethon-вызовов.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.db.models import Account, AccountStatus
from app.telegram.spam_checker import (
    ParsedSpamStatus,
    parse_spambot_response,
    peerflood_cooldown_active,
    resolve_temporary_unlock,
)


# --- no_limits ---


def test_russian_free_of_restrictions_is_no_limits():
    # Реальный ответ SpamBot, ранее падавший в unknown.
    r = parse_spambot_response("Ваш аккаунт свободен от каких-либо ограничений.")
    assert r.status == "no_limits"


def test_english_good_news_is_no_limits():
    r = parse_spambot_response(
        "Good news, no limits are currently applied to your account. "
        "You're free as a bird!"
    )
    assert r.status == "no_limits"


def test_russian_net_ogranicheniy_is_no_limits():
    r = parse_spambot_response("Хорошие новости! Нет ограничений на ваш аккаунт.")
    assert r.status == "no_limits"


# --- блокировки НЕ должны читаться как no_limits ---


def test_temporary_with_date_unchanged():
    r = parse_spambot_response(
        "Your account is now limited until 12 January 2025, 17:00 UTC."
    )
    assert r.status == "temporary"
    assert r.unlock_at is not None


def test_permanent_unchanged():
    r = parse_spambot_response(
        "Unfortunately your account was limited permanently. Unable to lift this."
    )
    assert r.status == "permanent"


def test_russian_blocked_not_misread_as_no_limits():
    # Содержит «ограничен», но НЕ «свобод» → не должно стать no_limits.
    r = parse_spambot_response("Ваш аккаунт ограничен из-за жалоб пользователей.")
    assert r.status != "no_limits"


# --- реальные ответы SpamBot из прода, ранее падавшие в unknown ---


def test_blocked_for_violations_is_permanent():
    # Жёсткий бан по жалобам без даты автоснятия → permanent (set_dead).
    r = parse_spambot_response(
        "Your account was blocked for violations of the "
        "[Telegram Terms of Service](https://telegram.org/tos) "
        "based on user reports confirmed by our moderators."
    )
    assert r.status == "permanent"


def test_dateless_limited_is_temporary_with_fallback():
    # Лимит без даты автоснятия → temporary, дату не распознаём (fallback в реакции).
    r = parse_spambot_response(
        "Hello Arseniy! I’m very sorry that you had to contact me. Unfortunately, "
        "some actions can trigger a harsh response from our anti-spam systems. "
        "While the account is limited, you will not be able to send messages to "
        "people who do not have your number in their phone contacts or add them "
        "to groups and channels."
    )
    assert r.status == "temporary"
    assert r.unlock_at is None


def test_limited_with_date_still_wins_over_dateless_rule():
    # Лимит С датой → правило даты приоритетнее нового бездатного «limited».
    r = parse_spambot_response(
        "I’m afraid some Telegram users found your messages annoying and forwarded "
        "them to our team of moderators. The moderators have confirmed the report "
        "and your account is now limited until 29 Jun 2026, 08:26 UTC."
    )
    assert r.status == "temporary"
    assert r.unlock_at is not None


# --- peerflood_cooldown_active (§6.5 кулдаун PeerFlood-карантина) ---


def _acc(status: AccountStatus) -> Account:
    a = Account()
    a.id = 1
    a.status = status
    return a


def test_cooldown_holds_spam_blocked_with_recent_peerflood():
    # Главный кейс пинг-понга: spam_blocked + недавний peer_flood → карантин держим.
    acc = _acc(AccountStatus.spam_blocked)
    assert peerflood_cooldown_active(
        acc, has_recent_peer_flood=True, cooldown_hours=3
    ) is True


def test_cooldown_lifts_after_window():
    # peer_flood старше кулдауна → снятие по no_limits разрешено (§7.4 как обычно).
    acc = _acc(AccountStatus.spam_blocked)
    assert peerflood_cooldown_active(
        acc, has_recent_peer_flood=False, cooldown_hours=3
    ) is False


def test_cooldown_disabled_by_zero_setting():
    # peerflood_cooldown_hours=0 → фича выключена, поведение прежнее.
    acc = _acc(AccountStatus.spam_blocked)
    assert peerflood_cooldown_active(
        acc, has_recent_peer_flood=True, cooldown_hours=0
    ) is False


def test_cooldown_only_for_spam_blocked():
    # Кулдаун касается только spam_blocked; прочие статусы — не его зона.
    for status in (AccountStatus.active, AccountStatus.pause, AccountStatus.warmup):
        assert peerflood_cooldown_active(
            _acc(status), has_recent_peer_flood=True, cooldown_hours=3
        ) is False


# --- прочие статусы без регрессий ---


def test_soft_warning_unchanged():
    r = parse_spambot_response(
        "Some users may report your messages as spam. Please be careful."
    )
    assert r.status == "soft_warning"


def test_unknown_unchanged():
    r = parse_spambot_response("Привет! Чем могу помочь?")
    assert r.status == "unknown"


# --- resolve_temporary_unlock: fallback при нераспознанной дате (MVP-6 §16) ---


def test_resolve_temporary_unlock_uses_parsed_date():
    known = datetime(2025, 1, 12, 17, 0, tzinfo=timezone.utc)
    now = datetime(2025, 1, 1, tzinfo=timezone.utc)
    parsed = ParsedSpamStatus("temporary", unlock_at=known)
    assert resolve_temporary_unlock(parsed, now, 24) == known


def test_resolve_temporary_unlock_fallback_when_no_date():
    now = datetime(2025, 1, 1, tzinfo=timezone.utc)
    parsed = ParsedSpamStatus("temporary", unlock_at=None)
    assert resolve_temporary_unlock(parsed, now, 24) == now + timedelta(hours=24)
