"""Юнит-тесты helper'ов удаления аккаунта. Чистые функции, без БД/Telethon."""

from __future__ import annotations

import os

import pytest

from app.telegram.client_factory import parse_account_ref, session_file_paths


# --- parse_account_ref ---


@pytest.mark.parametrize(
    "arg,expected",
    [
        ("+77788786614", ("phone", "+77788786614")),
        ("77788786614", ("phone", "+77788786614")),       # длинные цифры → phone
        ("+7 (778) 878-66-14", ("phone", "+77788786614")),  # нормализация
        ("3", ("id", 3)),
        ("42", ("id", 42)),
        ("@3", ("id", 3)),            # ведущий @ срезается
        ("123456", ("id", 123456)),   # 6 цифр → id
        ("1234567", ("phone", "+1234567")),  # 7 цифр → phone
        ("  5  ", ("id", 5)),         # пробелы по краям
    ],
)
def test_parse_valid(arg, expected):
    assert parse_account_ref(arg) == expected


@pytest.mark.parametrize(
    "arg", [None, "", "   ", "abc", "+", "+5", "12a3", "@@", "-1"]
)
def test_parse_invalid(arg):
    assert parse_account_ref(arg) is None


# --- session_file_paths (проверяем имена файлов кроссплатформенно) ---


def test_session_file_paths_basic():
    paths = session_file_paths("/app/data/sessions", "+77788786614")
    assert len(paths) == 2
    assert os.path.basename(paths[0]) == "77788786614.session"
    assert os.path.basename(paths[1]) == "77788786614.session-journal"


def test_session_file_paths_strips_nondigits():
    paths = session_file_paths("/x", "+7 (778) 878-66-14")
    assert os.path.basename(paths[0]) == "77788786614.session"
    assert os.path.basename(paths[1]) == "77788786614.session-journal"
