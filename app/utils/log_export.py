"""Выгрузка файловых логов loguru через бот. ARCHITECTURE.md §15.4.

Чистые функции (без aiogram/БД): разбор аргумента, подбор файлов в каталоге логов
и сборка архива. Хэндлер (app/bot/handlers/export.py) только берёт settings.logs_path,
зовёт эти функции и отправляет результат документом.

Loguru пишет `app_{YYYY-MM-DD}.log`; после ротации старый файл сжимается в
`app_{YYYY-MM-DD}.log.zip`. Поэтому за прошлые дни ищем оба расширения.
"""

from __future__ import annotations

import io
import zipfile
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

# Максимум дней для формы `/export_log N`.
MAX_DAYS = 30
# Лимит размера документа, отправляемого ботом (Telegram Bot API — 50 МБ).
TELEGRAM_DOC_LIMIT = 50 * 1024 * 1024


class ExportLogError(ValueError):
    """Некорректный аргумент или слишком большой архив — текст уйдёт пользователю."""


@dataclass(frozen=True)
class ExportLogSpec:
    kind: str  # 'today' | 'yesterday' | 'range'
    days: int = 1  # используется только для 'range'


def parse_export_log_arg(arg: str | None, *, today: date) -> ExportLogSpec:
    """Разобрать аргумент команды. `today` передаётся явно (тестируемость, TZ).

    Пусто/`today` → today; `yesterday` → yesterday; цифра N (1..MAX_DAYS) → range.
    Иначе — ExportLogError с подсказкой.
    """
    a = (arg or "").strip().lower()
    if a in ("", "today"):
        return ExportLogSpec("today")
    if a == "yesterday":
        return ExportLogSpec("yesterday")
    if a.isdigit():
        n = int(a)
        if 1 <= n <= MAX_DAYS:
            return ExportLogSpec("range", days=n)
        raise ExportLogError(
            f"N должно быть от 1 до {MAX_DAYS}. Пример: /export_log 7"
        )
    raise ExportLogError(
        "Использование: /export_log [today|yesterday|N]. Пример: /export_log 7"
    )


def _dates_for_spec(spec: ExportLogSpec, today: date) -> list[date]:
    if spec.kind == "today":
        return [today]
    if spec.kind == "yesterday":
        return [today - timedelta(days=1)]
    # range: последние N дней, включая сегодня.
    return [today - timedelta(days=i) for i in range(spec.days)]


def select_log_files(
    logs_dir: str | Path, spec: ExportLogSpec, *, today: date
) -> list[Path]:
    """Найти файлы логов (`app_YYYY-MM-DD.log[.zip]`) за период спецификации."""
    logs_dir = Path(logs_dir)
    if not logs_dir.is_dir():
        return []
    wanted = {d.isoformat() for d in _dates_for_spec(spec, today)}
    found: list[Path] = []
    for p in sorted(logs_dir.iterdir()):
        if not p.is_file():
            continue
        name = p.name
        if not name.startswith("app_"):
            continue
        if not (name.endswith(".log") or name.endswith(".zip")):
            continue
        if any(d in name for d in wanted):
            found.append(p)
    return found


def build_log_archive(
    files: list[Path], spec: ExportLogSpec
) -> tuple[bytes, str]:
    """Собрать байты для отправки: один файл — как есть; иначе zip. Проверяет лимит."""
    if not files:
        raise ExportLogError("Логи за указанный период не найдены.")
    # Один файл и это не диапазон → отдаём как есть (включая уже сжатый .log.zip).
    if len(files) == 1 and spec.kind != "range":
        data = files[0].read_bytes()
        _check_size(data)
        return data, files[0].name
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in files:
            zf.write(p, arcname=p.name)
    data = buf.getvalue()
    _check_size(data)
    return data, _archive_name(spec)


def _archive_name(spec: ExportLogSpec) -> str:
    if spec.kind == "range":
        return f"logs_last_{spec.days}d.zip"
    return f"logs_{spec.kind}.zip"


def _check_size(data: bytes) -> None:
    if len(data) > TELEGRAM_DOC_LIMIT:
        mb = len(data) / (1024 * 1024)
        raise ExportLogError(
            f"Архив слишком большой ({mb:.1f} МБ > 50 МБ). Уменьшите период (N)."
        )
