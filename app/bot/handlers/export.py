"""Хендлеры экспорта. ARCHITECTURE.md §10.2, §15.4.

  • /export_report <campaign_id> — CSV-отчёт по кампании (app/campaigns/reporting.py).
  • /export_log [today|yesterday|N] — выгрузка файловых логов loguru (app/utils/log_export.py).

Тонкий слой: выборка из БД / подбор файлов + отправка документом. Вся логика
формирования — в чистых модулях (тестируется юнитами).
"""

from __future__ import annotations

from datetime import datetime, timezone

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import BufferedInputFile, Message
from loguru import logger

from app.bot import formatting as fmt
from app.campaigns.reporting import build_campaign_csv, report_filename
from app.config import settings
from app.db.repositories import campaigns as campaigns_repo
from app.db.repositories import tasks as tasks_repo
from app.db.session import session_scope
from app.utils.log_export import (
    ExportLogError,
    build_log_archive,
    parse_export_log_arg,
    select_log_files,
)

router = Router(name="export")


@router.message(Command("export_report"))
async def cmd_export_report(message: Message) -> None:
    """CSV-отчёт по кампании (§10.2)."""
    if message.text is None:
        return
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip().isdigit():
        await message.answer(
            "Использование: <code>/export_report &lt;campaign_id&gt;</code>"
        )
        return
    campaign_id = int(parts[1].strip())

    async with session_scope() as session:
        campaign = await campaigns_repo.get_by_id(session, campaign_id)
        if campaign is None:
            await message.answer(f"Кампания #{campaign_id} не найдена.")
            return
        tasks = await tasks_repo.list_for_campaign(session, campaign_id=campaign_id)

    data = build_campaign_csv(campaign, tasks)
    filename = report_filename(campaign_id, datetime.now(timezone.utc))
    caption = (
        f"📤 <b>Отчёт по кампании #{campaign_id}</b> · {fmt.campaign_type_ru(campaign)}\n"
        f"{fmt.sent_label(campaign)}: {fmt.num(campaign.sent_count)} из "
        f"{fmt.num(campaign.total_count)}\n"
        f"Пропущено: {fmt.num(campaign.skipped_count)} · "
        f"Ошибки: {fmt.num(campaign.failed_count)}\n"
        f"Строк в файле: {fmt.num(len(tasks))}"
    )
    await message.answer_document(
        BufferedInputFile(data, filename=filename), caption=caption
    )
    logger.info("export_report campaign #{} → {} tasks", campaign_id, len(tasks))


@router.message(Command("export_log"))
async def cmd_export_log(message: Message) -> None:
    """Выгрузка файловых логов за период: today | yesterday | N (§15.4)."""
    if message.text is None:
        return
    parts = message.text.split(maxsplit=1)
    arg = parts[1].strip() if len(parts) > 1 else None
    # Локальная дата процесса = TZ контейнера — совпадает с именами файлов loguru.
    today = datetime.now().date()
    try:
        spec = parse_export_log_arg(arg, today=today)
        files = select_log_files(settings.logs_path, spec, today=today)
        data, filename = build_log_archive(files, spec)
    except ExportLogError as exc:
        await message.answer(str(exc))
        return
    await message.answer_document(
        BufferedInputFile(data, filename=filename),
        caption=f"Логи: {filename}",
    )
    logger.info("export_log {} → {} ({} bytes)", spec.kind, filename, len(data))
