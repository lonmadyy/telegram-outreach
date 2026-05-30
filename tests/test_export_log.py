"""Юнит-тесты выгрузки логов. ARCHITECTURE.md §15.4.

Чистые функции + tmp_path с подставными файлами loguru. Без БД/aiogram.
"""

from __future__ import annotations

import io
import zipfile
from datetime import date

import pytest

from app.utils.log_export import (
    ExportLogError,
    build_log_archive,
    parse_export_log_arg,
    select_log_files,
)

TODAY = date(2026, 5, 31)


# --- parse_export_log_arg ---


@pytest.mark.parametrize(
    "arg,kind,days",
    [
        (None, "today", 1),
        ("", "today", 1),
        ("today", "today", 1),
        ("TODAY", "today", 1),
        ("yesterday", "yesterday", 1),
        ("1", "range", 1),
        ("7", "range", 7),
        ("30", "range", 30),
    ],
)
def test_parse_valid(arg, kind, days):
    spec = parse_export_log_arg(arg, today=TODAY)
    assert spec.kind == kind
    if kind == "range":
        assert spec.days == days


@pytest.mark.parametrize("arg", ["0", "31", "99", "abc", "-1", "3.5"])
def test_parse_invalid(arg):
    with pytest.raises(ExportLogError):
        parse_export_log_arg(arg, today=TODAY)


# --- select_log_files ---


def _touch(d, name, content=b"x"):
    p = d / name
    p.write_bytes(content)
    return p


def test_select_today_picks_only_today_log(tmp_path):
    _touch(tmp_path, "app_2026-05-31.log")
    _touch(tmp_path, "app_2026-05-30.log.zip")
    spec = parse_export_log_arg("today", today=TODAY)
    files = select_log_files(tmp_path, spec, today=TODAY)
    assert [p.name for p in files] == ["app_2026-05-31.log"]


def test_select_yesterday_includes_zip(tmp_path):
    _touch(tmp_path, "app_2026-05-31.log")
    _touch(tmp_path, "app_2026-05-30.log.zip")
    spec = parse_export_log_arg("yesterday", today=TODAY)
    files = select_log_files(tmp_path, spec, today=TODAY)
    assert [p.name for p in files] == ["app_2026-05-30.log.zip"]


def test_select_range_last_3_days(tmp_path):
    _touch(tmp_path, "app_2026-05-31.log")
    _touch(tmp_path, "app_2026-05-30.log.zip")
    _touch(tmp_path, "app_2026-05-29.log.zip")
    _touch(tmp_path, "app_2026-05-28.log.zip")  # вне диапазона 3 дней
    _touch(tmp_path, "other.txt")  # не app_ → игнорируется
    spec = parse_export_log_arg("3", today=TODAY)
    files = select_log_files(tmp_path, spec, today=TODAY)
    names = sorted(p.name for p in files)
    assert names == [
        "app_2026-05-29.log.zip",
        "app_2026-05-30.log.zip",
        "app_2026-05-31.log",
    ]


def test_select_missing_dir_returns_empty(tmp_path):
    spec = parse_export_log_arg("today", today=TODAY)
    assert select_log_files(tmp_path / "nope", spec, today=TODAY) == []


# --- build_log_archive ---


def test_build_single_file_returned_as_is(tmp_path):
    p = _touch(tmp_path, "app_2026-05-31.log", b"hello")
    spec = parse_export_log_arg("today", today=TODAY)
    data, name = build_log_archive([p], spec)
    assert data == b"hello"
    assert name == "app_2026-05-31.log"


def test_build_range_makes_zip(tmp_path):
    p1 = _touch(tmp_path, "app_2026-05-31.log", b"a")
    p2 = _touch(tmp_path, "app_2026-05-30.log.zip", b"b")
    spec = parse_export_log_arg("2", today=TODAY)
    data, name = build_log_archive([p1, p2], spec)
    assert name == "logs_last_2d.zip"
    zf = zipfile.ZipFile(io.BytesIO(data))
    assert sorted(zf.namelist()) == [
        "app_2026-05-30.log.zip",
        "app_2026-05-31.log",
    ]


def test_build_empty_raises():
    spec = parse_export_log_arg("today", today=TODAY)
    with pytest.raises(ExportLogError):
        build_log_archive([], spec)


def test_build_oversize_raises(tmp_path, monkeypatch):
    import app.utils.log_export as le

    monkeypatch.setattr(le, "TELEGRAM_DOC_LIMIT", 3)
    p = _touch(tmp_path, "app_2026-05-31.log", b"toolong")
    spec = parse_export_log_arg("today", today=TODAY)
    with pytest.raises(ExportLogError):
        build_log_archive([p], spec)
