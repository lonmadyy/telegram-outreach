"""Юнит-тесты per-account API-ключа. ARCHITECTURE.md §11.1, §10.3.

Чистые функции parse_api_id / validate_api_hash / effective_api_credentials
без БД, Telethon и сети.
"""

from __future__ import annotations

import pytest

from app.telegram import client_factory
from app.telegram.client_factory import (
    effective_api_credentials,
    parse_api_id,
    validate_api_hash,
)


# --- parse_api_id ---


def test_parse_api_id_valid():
    assert parse_api_id("2040") == 2040
    assert parse_api_id("  34554902  ") == 34554902


def test_parse_api_id_non_digit():
    for bad in ("abc", "20a40", "2040.0", "-5", "+7"):
        with pytest.raises(ValueError):
            parse_api_id(bad)


def test_parse_api_id_zero_rejected():
    with pytest.raises(ValueError):
        parse_api_id("0")


def test_parse_api_id_empty():
    for bad in ("", "   ", None):  # type: ignore[arg-type]
        with pytest.raises(ValueError):
            parse_api_id(bad)


# --- validate_api_hash ---


def test_validate_api_hash_valid_lower():
    h = "0123456789abcdef0123456789abcdef"
    assert validate_api_hash(h) == h


def test_validate_api_hash_uppercase_normalized():
    h = "0123456789ABCDEF0123456789ABCDEF"
    assert validate_api_hash(h) == h.lower()


def test_validate_api_hash_trims_whitespace():
    h = "0123456789abcdef0123456789abcdef"
    assert validate_api_hash(f"  {h}\n") == h


def test_validate_api_hash_wrong_length():
    for bad in (
        "0123456789abcdef",  # 16
        "0123456789abcdef0123456789abcdef00",  # 34
    ):
        with pytest.raises(ValueError):
            validate_api_hash(bad)


def test_validate_api_hash_non_hex():
    with pytest.raises(ValueError):
        validate_api_hash("g123456789abcdef0123456789abcdef")


def test_validate_api_hash_empty():
    for bad in ("", "   ", None):  # type: ignore[arg-type]
        with pytest.raises(ValueError):
            validate_api_hash(bad)


# --- effective_api_credentials (fallback на глобальный ключ из .env) ---


def test_effective_credentials_per_account(monkeypatch):
    monkeypatch.setattr(client_factory.settings, "tg_api_id", 111, raising=False)
    monkeypatch.setattr(client_factory.settings, "tg_api_hash", "global", raising=False)
    h = "0123456789abcdef0123456789abcdef"
    assert effective_api_credentials(2040, h) == (2040, h)


def test_effective_credentials_fallback_when_none(monkeypatch):
    monkeypatch.setattr(client_factory.settings, "tg_api_id", 111, raising=False)
    monkeypatch.setattr(client_factory.settings, "tg_api_hash", "global", raising=False)
    assert effective_api_credentials(None, None) == (111, "global")


def test_effective_credentials_fallback_when_partial(monkeypatch):
    # Неполный per-account ключ (одно из полей пустое) → откат на глобальный целиком.
    monkeypatch.setattr(client_factory.settings, "tg_api_id", 111, raising=False)
    monkeypatch.setattr(client_factory.settings, "tg_api_hash", "global", raising=False)
    assert effective_api_credentials(2040, None) == (111, "global")
    assert effective_api_credentials(None, "abc") == (111, "global")
    assert effective_api_credentials(2040, "") == (111, "global")
