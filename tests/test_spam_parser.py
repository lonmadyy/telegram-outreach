"""Юнит-тесты парсера ответов @SpamBot. ARCHITECTURE.md §7.3.

Включает реальные ответы из логов, на которых парсер ранее ошибался.
Чистая функция parse_spambot_response, без БД/Telethon-вызовов.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.telegram.spam_checker import (
    ParsedSpamStatus,
    parse_spambot_response,
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
