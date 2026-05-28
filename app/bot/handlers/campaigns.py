"""Хендлеры кампаний. ARCHITECTURE.md §10.2, §10.4.

В MVP-2 поддерживается только тип `message` (рассылка). Ветка invite
покажет заглушку «доступно в MVP-4».
"""

from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import BufferedInputFile, CallbackQuery, Message
from loguru import logger

from app.bot.keyboards import (
    campaign_type_kb,
    cancel_kb,
    confirm_kb,
    main_menu,
    resend_decision_kb,
    templates_picker_kb,
)
from app.bot.states import NewCampaign
from app.campaigns import manager as campaign_manager
from app.db.models import CampaignType
from app.db.repositories import campaigns as campaigns_repo
from app.db.repositories import templates as templates_repo
from app.db.session import session_scope
from app.utils.txt_parser import parse_txt_usernames

router = Router(name="campaigns")


@router.message(Command("campaigns"))
@router.callback_query(F.data == "menu:status")
async def list_campaigns(event) -> None:
    async with session_scope() as session:
        items = await campaigns_repo.list_campaigns(session, limit=15)

    if isinstance(event, CallbackQuery):
        target = event.message
        await event.answer()
    else:
        target = event
    if target is None:
        return

    if not items:
        await target.answer(
            "Кампаний пока нет.\nЗапустите через /new_campaign.",
            reply_markup=main_menu(),
        )
        return

    lines = ["<b>Кампании:</b>"]
    for c in items:
        lines.append(
            f"#{c.id} [{c.type.value}] {c.status.value} | "
            f"sent={c.sent_count}/{c.total_count} "
            f"skip={c.skipped_count} fail={c.failed_count}"
        )
    await target.answer("\n".join(lines), reply_markup=main_menu())


@router.message(Command("new_campaign"))
@router.callback_query(F.data == "menu:new_campaign")
async def cmd_new_campaign(event, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(NewCampaign.waiting_type)

    if isinstance(event, CallbackQuery):
        target = event.message
        await event.answer()
    else:
        target = event
    if target is None:
        return

    await target.answer(
        "Выберите тип кампании:",
        reply_markup=campaign_type_kb(),
    )


@router.callback_query(NewCampaign.waiting_type, F.data.startswith("ctype:"))
async def on_campaign_type(query: CallbackQuery, state: FSMContext) -> None:
    choice = (query.data or "").removeprefix("ctype:")
    if choice == "invite_disabled":
        await query.answer("Инвайт-кампания будет в MVP-4.", show_alert=True)
        return
    if choice != "message":
        await query.answer("Неизвестный тип", show_alert=True)
        return

    await state.update_data(campaign_type=CampaignType.message.value)
    await state.set_state(NewCampaign.waiting_txt)
    if query.message is not None:
        await query.message.answer(
            "Пришлите <b>TXT-файл</b> со списком username (один в строке, "
            "можно с @, регистр любой).",
            reply_markup=cancel_kb(),
        )
    await query.answer()


@router.message(NewCampaign.waiting_txt, F.document)
async def on_campaign_txt(message: Message, state: FSMContext) -> None:
    if message.document is None or message.bot is None:
        return

    doc = message.document
    if doc.mime_type and "text" not in doc.mime_type and not (
        doc.file_name and doc.file_name.lower().endswith(".txt")
    ):
        await message.answer(
            "Ожидаю текстовый файл .txt. Пришлите снова или /cancel.",
            reply_markup=cancel_kb(),
        )
        return

    file = await message.bot.get_file(doc.file_id)
    if file.file_path is None:
        await message.answer(
            "Не удалось получить файл, попробуйте ещё раз.", reply_markup=cancel_kb()
        )
        return
    buffer = await message.bot.download_file(file.file_path)
    if buffer is None:
        await message.answer(
            "Не удалось скачать файл.", reply_markup=cancel_kb()
        )
        return
    raw_bytes = buffer.read()

    try:
        content = raw_bytes.decode("utf-8")
    except UnicodeDecodeError:
        try:
            content = raw_bytes.decode("cp1251")
        except UnicodeDecodeError:
            await message.answer(
                "Не удалось декодировать файл (ожидаю UTF-8 или CP1251).",
                reply_markup=cancel_kb(),
            )
            return

    valid, invalid = parse_txt_usernames(content)
    if not valid:
        invalid_preview = "\n".join(invalid[:10]) if invalid else "—"
        await message.answer(
            f"В файле нет ни одного валидного username.\n"
            f"Невалидные строки (первые 10):\n<code>{invalid_preview}</code>",
            reply_markup=main_menu(),
        )
        await state.clear()
        return

    await state.update_data(usernames=valid, invalid_count=len(invalid))

    txt = (
        f"<b>Парсинг файла:</b>\n"
        f"• Валидных username: <b>{len(valid)}</b>\n"
        f"• Невалидных строк: {len(invalid)}\n\n"
        "Что делать с теми, кому уже когда-либо писали (записи в "
        "<code>processed_clients</code>)?\n\n"
        "• <b>Продолжить</b> — пропустить ВСЕХ, кто был обработан хоть когда-то.\n"
        "• <b>Переотправить тем, кому &gt;180 дней</b> — повторять для давних."
    )
    await state.set_state(NewCampaign.waiting_resend_decision)
    await message.answer(txt, reply_markup=resend_decision_kb())


@router.callback_query(NewCampaign.waiting_resend_decision, F.data.startswith("resend:"))
async def on_resend_decision(query: CallbackQuery, state: FSMContext) -> None:
    resend = (query.data or "").removeprefix("resend:") == "yes"
    await state.update_data(resend_old=resend)

    async with session_scope() as session:
        templates = await templates_repo.list_templates(session)

    if not templates:
        await query.answer("Нет шаблонов", show_alert=True)
        if query.message is not None:
            await query.message.answer(
                "Сначала создайте шаблон через /new_template.",
                reply_markup=main_menu(),
            )
        await state.clear()
        return

    await state.set_state(NewCampaign.waiting_template)
    if query.message is not None:
        await query.message.answer(
            "Выберите шаблон для рассылки:",
            reply_markup=templates_picker_kb(templates),
        )
    await query.answer()


@router.callback_query(NewCampaign.waiting_template, F.data.startswith("pick_tpl:"))
async def on_pick_template(query: CallbackQuery, state: FSMContext) -> None:
    raw = (query.data or "").removeprefix("pick_tpl:")
    try:
        template_id = int(raw)
    except ValueError:
        await query.answer("Битое значение", show_alert=True)
        return

    async with session_scope() as session:
        template = await templates_repo.get_by_id(session, template_id)
    if template is None:
        await query.answer("Шаблон не найден", show_alert=True)
        return

    data = await state.get_data()
    usernames = data.get("usernames", [])
    resend_old = data.get("resend_old", False)

    await state.update_data(template_id=template_id, template_name=template.name)
    await state.set_state(NewCampaign.waiting_confirm)
    if query.message is not None:
        body_head = template.body.replace("\n", " ")[:160]
        await query.message.answer(
            f"<b>Готово к старту:</b>\n"
            f"• Тип: рассылка\n"
            f"• Username в работе: {len(usernames)}\n"
            f"• Переотправлять &gt;180 дней: {'да' if resend_old else 'нет'}\n"
            f"• Шаблон: <b>{template.name}</b>\n"
            f"  <code>{body_head}</code>\n\n"
            "Жми «Подтвердить» — кампания создаётся и стартует.",
            reply_markup=confirm_kb("camp:start"),
        )
    await query.answer()


@router.callback_query(NewCampaign.waiting_confirm, F.data == "camp:start")
async def on_campaign_start(query: CallbackQuery, state: FSMContext) -> None:
    if query.from_user is None:
        return
    data = await state.get_data()
    usernames = data.get("usernames") or []
    template_id = data.get("template_id")
    resend_old = bool(data.get("resend_old"))

    if not usernames or not template_id:
        await query.answer("Сессия пустая, начните заново", show_alert=True)
        await state.clear()
        return

    # 1. Создаём campaign.
    async with session_scope() as session:
        campaign = await campaigns_repo.create_campaign(
            session,
            type_=CampaignType.message,
            template_id=template_id,
            resend_old=resend_old,
            created_by_user_id=query.from_user.id,
        )
        campaign_id = campaign.id

    # 2. Заполняем tasks с дедупом.
    created, skipped = await campaign_manager.create_tasks_for_campaign(
        campaign_id=campaign_id, usernames=usernames
    )

    # 3. Стартуем worker.
    ok, msg = await campaign_manager.start_campaign(campaign_id)

    await state.clear()
    if query.message is not None:
        text = (
            f"<b>Кампания #{campaign_id} создана:</b>\n"
            f"• Задач в работу: {created}\n"
            f"• Уже обработано ранее (пропущено): {skipped}\n"
        )
        if ok:
            text += f"\nСтарт: {msg}"
        else:
            text += f"\nНе удалось запустить: {msg}"
        await query.message.answer(text, reply_markup=main_menu())
    await query.answer("Готово")


@router.message(Command("pause"))
async def cmd_pause(message: Message) -> None:
    if message.text is None:
        return
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip().isdigit():
        await message.answer("Использование: <code>/pause &lt;campaign_id&gt;</code>")
        return
    campaign_id = int(parts[1].strip())
    ok, msg = await campaign_manager.pause_campaign(campaign_id)
    await message.answer(msg, reply_markup=main_menu())


@router.message(Command("resume"))
async def cmd_resume(message: Message) -> None:
    if message.text is None:
        return
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip().isdigit():
        await message.answer("Использование: <code>/resume &lt;campaign_id&gt;</code>")
        return
    campaign_id = int(parts[1].strip())
    ok, msg = await campaign_manager.resume_campaign(campaign_id)
    await message.answer(msg, reply_markup=main_menu())


@router.message(Command("stop"))
async def cmd_stop(message: Message) -> None:
    if message.text is None:
        return
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip().isdigit():
        await message.answer("Использование: <code>/stop &lt;campaign_id&gt;</code>")
        return
    campaign_id = int(parts[1].strip())
    ok, msg = await campaign_manager.stop_campaign(campaign_id)
    await message.answer(msg, reply_markup=main_menu())
