"""Тесты template engine. ARCHITECTURE.md §8."""

from __future__ import annotations

import random

import pytest

from app.campaigns.template_engine import (
    TELEGRAM_MAX_MESSAGE_LENGTH,
    TemplateError,
    extract_variables,
    preview,
    render_template,
    validate_template,
)


def test_render_no_spintax_no_vars() -> None:
    assert render_template("Привет мир!") == "Привет мир!"


def test_render_variable_substitution() -> None:
    out = render_template("Привет, {first_name}!", {"first_name": "Иван"})
    assert out == "Привет, Иван!"


def test_render_variable_fallback_when_missing() -> None:
    out = render_template("Hello, {first_name}!")
    assert out == "Hello, there!"


def test_render_variable_empty_uses_fallback() -> None:
    out = render_template("Hello, {first_name}!", {"first_name": ""})
    assert out == "Hello, there!"


def test_render_unknown_variable_becomes_empty() -> None:
    out = render_template("Hi {nope}!")
    assert out == "Hi !"


def test_render_simple_spintax(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(random, "choice", lambda items: items[1])
    out = render_template("{Привет|Здравствуй|Хей}, друг")
    assert out == "Здравствуй, друг"


def test_render_nested_spintax_inside_out(monkeypatch: pytest.MonkeyPatch) -> None:
    # Сначала раскроется внутренний {новое|старое} → "новое",
    # потом внешний {что-то новое|ничего} → "ничего" (детерминируем через choice).
    calls = iter(["новое", "ничего"])
    monkeypatch.setattr(random, "choice", lambda items: next(calls))
    out = render_template("{что-то {новое|старое}|ничего}")
    assert out == "ничего"


def test_render_spintax_with_variables(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(random, "choice", lambda items: items[0])
    out = render_template(
        "{Привет|Здравствуй}, {first_name}!", {"first_name": "Юзер"}
    )
    assert out == "Привет, Юзер!"


def test_escaped_braces_kept_literally() -> None:
    out = render_template(r"\{not_a_var\} {first_name}", {"first_name": "X"})
    assert out == "{not_a_var} X"


def test_single_token_without_pipe_is_variable_not_spintax() -> None:
    # `{single}` — это переменная (нет `|`), а не spintax.
    out = render_template("{single}", {"single": "val"})
    assert out == "val"


def test_extract_variables_skips_spintax_branches() -> None:
    body = "Hi {first_name}, {Как дела|Что нового} {username}?"
    assert extract_variables(body) == ["first_name", "username"]


def test_extract_variables_unique_preserving_order() -> None:
    body = "{a} {b} {a} {c} {b}"
    assert extract_variables(body) == ["a", "b", "c"]


def test_validate_template_returns_variables() -> None:
    vars_ = validate_template("Hi {first_name}!")
    assert vars_ == ["first_name"]


def test_validate_template_empty_raises() -> None:
    with pytest.raises(TemplateError):
        validate_template("")
    with pytest.raises(TemplateError):
        validate_template("   ")


def test_validate_template_unbalanced_braces_raises() -> None:
    with pytest.raises(TemplateError):
        validate_template("Hello {first_name")


def test_validate_template_length_limit_enforced() -> None:
    body = "x" * (TELEGRAM_MAX_MESSAGE_LENGTH + 1)
    with pytest.raises(TemplateError):
        validate_template(body)


def test_validate_template_balanced_with_escapes_ok() -> None:
    # `\{ \}` парные экранированные скобки — валидно.
    vars_ = validate_template(r"Use \{curly\} braces literally")
    assert vars_ == []


def test_preview_returns_n_renders() -> None:
    body = "{a|b|c|d|e}"
    items = preview(body, n=10)
    assert len(items) == 10
    assert all(s in {"a", "b", "c", "d", "e"} for s in items)
