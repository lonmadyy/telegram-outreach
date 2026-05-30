"""CSV-отчёты по кампаниям. ARCHITECTURE.md §10.2, §12.

Чистые функции (без БД/Telethon): принимают уже выбранные объекты campaign + tasks
и формируют байты CSV. Выборка из БД делается в хэндлере бота (app/bot/handlers/export.py).

Формат (решение MVP-6): разделитель «;» + UTF-8 BOM — русский Excel открывает файл
двойным кликом без «кракозябр» и без мастера импорта. Сначала шапка-сводка кампании,
затем пустая строка, затем построчная таблица задач.
"""

from __future__ import annotations

import csv
import io
from datetime import datetime

# Заголовки таблицы задач (совпадают с полями tasks, §4.5).
TASK_HEADER = [
    "id",
    "username",
    "status",
    "result_code",
    "attempts",
    "assigned_account_id",
    "error_message",
    "processed_at",
]


def _cell(value: object) -> str:
    """Значение ячейки: None→'', datetime→ISO-подобно, enum→.value, иначе str."""
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    # enum (CampaignType/TaskStatus/ResultCode) → строковое значение.
    return str(getattr(value, "value", value))


def build_campaign_csv(campaign, tasks, *, delimiter: str = ";") -> bytes:
    """Сформировать CSV-отчёт по кампании. Чистая функция.

    `campaign` — объект с полями id/type/status/*_count/target_chat/created_at/
    started_at/finished_at. `tasks` — итерируемое объектов с полями из TASK_HEADER.
    Возвращает UTF-8 байты с BOM (для совместимости с Excel-RU).
    """
    buf = io.StringIO()
    writer = csv.writer(buf, delimiter=delimiter, lineterminator="\n")

    # 1. Шапка-сводка (двухколоночные строки «показатель; значение»).
    writer.writerow(["Отчёт по кампании", f"#{_cell(campaign.id)}"])
    writer.writerow(["Тип", _cell(campaign.type)])
    writer.writerow(["Статус", _cell(campaign.status)])
    if getattr(campaign, "target_chat", None):
        writer.writerow(["Целевой чат", _cell(campaign.target_chat)])
    writer.writerow(["Всего задач", _cell(campaign.total_count)])
    writer.writerow(["Отправлено", _cell(campaign.sent_count)])
    writer.writerow(["Пропущено", _cell(campaign.skipped_count)])
    writer.writerow(["Ошибок", _cell(campaign.failed_count)])
    writer.writerow(["Создана", _cell(campaign.created_at)])
    writer.writerow(["Старт", _cell(getattr(campaign, "started_at", None))])
    writer.writerow(["Финиш", _cell(getattr(campaign, "finished_at", None))])

    # 2. Пустая строка-разделитель + таблица задач.
    writer.writerow([])
    writer.writerow(TASK_HEADER)
    for t in tasks:
        writer.writerow(
            [
                _cell(t.id),
                _cell(t.username),
                _cell(t.status),
                _cell(t.result_code),
                _cell(t.attempts),
                _cell(t.assigned_account_id),
                _cell(t.error_message),
                _cell(t.processed_at),
            ]
        )

    # BOM, чтобы Excel-RU корректно распознал UTF-8.
    return buf.getvalue().encode("utf-8-sig")


def report_filename(campaign_id: int, now: datetime) -> str:
    """Имя файла отчёта: report_campaign_<id>_<YYYYMMDD_HHMM>.csv."""
    return f"report_campaign_{campaign_id}_{now:%Y%m%d_%H%M}.csv"
