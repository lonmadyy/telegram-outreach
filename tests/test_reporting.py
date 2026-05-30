"""Юнит-тесты CSV-отчётов по кампаниям. ARCHITECTURE.md §10.2.

Чистые функции (без БД/Telethon): подаём SimpleNamespace вместо ORM-объектов.
"""

from __future__ import annotations

import csv
import io
from datetime import datetime, timezone
from types import SimpleNamespace

from app.campaigns.reporting import TASK_HEADER, build_campaign_csv, report_filename


def _campaign(**kw):
    base = dict(
        id=7,
        type="invite",
        status="done",
        total_count=3,
        sent_count=1,
        skipped_count=1,
        failed_count=1,
        target_chat="@mychat",
        created_at=datetime(2026, 5, 1, 10, 0, tzinfo=timezone.utc),
        started_at=datetime(2026, 5, 1, 10, 5, tzinfo=timezone.utc),
        finished_at=datetime(2026, 5, 1, 11, 0, tzinfo=timezone.utc),
    )
    base.update(kw)
    return SimpleNamespace(**base)


def _task(**kw):
    base = dict(
        id=1,
        username="user1",
        status="done",
        result_code="ok",
        attempts=1,
        assigned_account_id=2,
        error_message=None,
        processed_at=datetime(2026, 5, 1, 10, 6, tzinfo=timezone.utc),
    )
    base.update(kw)
    return SimpleNamespace(**base)


def test_csv_has_bom_and_semicolon():
    data = build_campaign_csv(_campaign(), [_task()])
    assert data.startswith(b"\xef\xbb\xbf")  # UTF-8 BOM
    text = data.decode("utf-8-sig")
    assert ";" in text
    assert "Отчёт по кампании;#7" in text


def test_csv_contains_header_and_task_rows():
    data = build_campaign_csv(
        _campaign(), [_task(id=1, username="a"), _task(id=2, username="b")]
    )
    text = data.decode("utf-8-sig")
    assert ";".join(TASK_HEADER) in text
    lines = [ln for ln in text.splitlines() if ln]
    assert any(ln.startswith("1;a;") for ln in lines)
    assert any(ln.startswith("2;b;") for ln in lines)


def test_csv_escapes_delimiter_and_quotes():
    # username/error с ';', '"', переводом строки — должны экранироваться и читаться обратно.
    t = _task(username="a;b", error_message='he said "hi"\nbye')
    data = build_campaign_csv(_campaign(), [t])
    text = data.decode("utf-8-sig")
    assert '"a;b"' in text          # поле с разделителем взято в кавычки
    assert '""hi""' in text          # внутренние кавычки удвоены (стандарт CSV)

    reader = list(csv.reader(io.StringIO(text), delimiter=";"))
    header_idx = reader.index(TASK_HEADER)
    rows = [r for r in reader[header_idx + 1 :] if r]
    assert len(rows) == 1
    assert rows[0][1] == "a;b"
    assert rows[0][6] == 'he said "hi"\nbye'


def test_report_filename_format():
    now = datetime(2026, 5, 31, 14, 3, tzinfo=timezone.utc)
    assert report_filename(7, now) == "report_campaign_7_20260531_1403.csv"


def test_csv_handles_none_target_and_empty_tasks():
    data = build_campaign_csv(_campaign(target_chat=None), [])
    text = data.decode("utf-8-sig")
    assert "Целевой чат" not in text          # строки нет, если чат не задан (message)
    assert ";".join(TASK_HEADER) in text       # заголовок таблицы есть даже без задач
