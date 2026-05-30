"""Handlers для управления настройками в рантайме. ARCHITECTURE.md §4.9, §10.2."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message
from loguru import logger

from app.bot.keyboards import main_menu
from app.db.repositories import settings as settings_repo
from app.db.session import session_scope

router = Router(name="settings")


@router.message(Command("settings"))
@router.callback_query(F.data == "menu:settings")
async def list_settings(event) -> None:
    async with session_scope() as session:
        rows = await settings_repo.get_all_with_types(session)

    if isinstance(event, CallbackQuery):
        target = event.message
        await event.answer()
    else:
        target = event
    if target is None:
        return

    if not rows:
        await target.answer("Настроек пока нет.", reply_markup=main_menu())
        return

    lines = ["<b>Текущие настройки:</b>", ""]
    for key in sorted(rows.keys()):
        value, value_type = rows[key]
        lines.append(f"<code>{key}</code> = <b>{value}</b> ({value_type})")
    lines.append("")
    lines.append(
        "Изменить: <code>/set ключ значение</code>\n"
        "Например: <code>/set interval_min_sec 360</code>"
    )
    await target.answer("\n".join(lines), reply_markup=main_menu())


@router.message(Command("set"))
async def cmd_set(message: Message) -> None:
    if message.text is None:
        return
    parts = message.text.split(maxsplit=2)
    if len(parts) < 3:
        await message.answer(
            "Использование: <code>/set ключ значение</code>",
            reply_markup=main_menu(),
        )
        return

    key = parts[1].strip()
    value = parts[2].strip()

    async with session_scope() as session:
        existing_type = await settings_repo.get_value_type(session, key)
        if existing_type is None:
            await message.answer(
                f"Ключ <code>{key}</code> не существует. "
                "Доступные ключи смотри /settings.",
                reply_markup=main_menu(),
            )
            return

        try:
            await settings_repo.set_value(
                session,
                key=key,
                value=value,
                value_type=existing_type,
                updated_by=message.from_user.id if message.from_user else None,
            )
        except ValueError as e:
            await message.answer(
                f"Ошибка значения: {e}", reply_markup=main_menu()
            )
            return

    # pg_notify ПОСЛЕ commit (вышли из async with — он закоммитился).
    try:
        await settings_repo.publish_changed(key)
    except Exception:
        logger.exception("publish_changed failed for {!r}", key)

    await message.answer(
        f"Настройка <code>{key}</code> = <b>{value}</b> ({existing_type}) обновлена.\n"
        f"Все воркеры и scheduler увидят новое значение в течение пары секунд.",
        reply_markup=main_menu(),
    )
