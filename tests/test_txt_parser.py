"""Тесты парсера TXT-списка. ARCHITECTURE.md §9.1 п.1.

Правила Telegram-username: 5–32 символа, первая буква, латиница/цифры/_.
"""

from __future__ import annotations

from app.utils.txt_parser import parse_txt_usernames


def test_parses_plain_usernames() -> None:
    valid, invalid = parse_txt_usernames("alice\nrobert\ncharlie\n")
    assert valid == ["alice", "robert", "charlie"]
    assert invalid == []


def test_strips_at_sign() -> None:
    valid, _ = parse_txt_usernames("@alice\n  @robert  \n")
    assert valid == ["alice", "robert"]


def test_lowercase_and_dedup_preserving_order() -> None:
    valid, _ = parse_txt_usernames("Alice\nROBERT\nalice\nCharlie\nRobert\n")
    assert valid == ["alice", "robert", "charlie"]


def test_skips_empty_and_comments() -> None:
    valid, _ = parse_txt_usernames(
        """
# header comment
alice

# section
robert
"""
    )
    assert valid == ["alice", "robert"]


def test_invalid_usernames_collected_separately() -> None:
    text = (
        "alice\n"
        "1user\n"          # начинается с цифры — невалидно
        "!!!\n"            # спецсимволы — невалидно
        "robert\n"
        "a_short_username_that_is_way_too_long_to_be_valid_xxxxx\n"  # >32 символов
    )
    valid, invalid = parse_txt_usernames(text)
    assert valid == ["alice", "robert"]
    assert "1user" in invalid
    assert "!!!" in invalid
    assert any(len(x) > 32 for x in invalid)


def test_telegram_username_minimum_5_chars() -> None:
    valid, invalid = parse_txt_usernames("abcd\nabcde\n")
    assert valid == ["abcde"]
    assert "abcd" in invalid


def test_underscore_and_digits_allowed_but_not_first() -> None:
    valid, _ = parse_txt_usernames("user_123\nfoo_bar\nx1234\n")
    assert valid == ["user_123", "foo_bar", "x1234"]


def test_empty_input_returns_empty() -> None:
    valid, invalid = parse_txt_usernames("")
    assert valid == []
    assert invalid == []
