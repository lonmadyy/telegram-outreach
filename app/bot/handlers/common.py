"""Общие хендлеры: /start, /help, /cancel, главное меню. ARCHITECTURE.md §10.6."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app.bot.keyboards import main_menu
from app.telegram.auth import auth_store

router = Router(name="common")


HELP_TEXT = (
    "<b>telegram-outreach</b>\n"
    "Управление многоаккаунтной рассылкой и инвайтами.\n\n"
    "<b>Команды:</b>\n"
    "/accounts — список аккаунтов\n"
    "/add_account — добавить userbot-аккаунт (поддерживает 2FA)\n"
    "/cancel — выйти из текущего сценария\n"
    "/help — это сообщение\n\n"
    "Главное меню — кнопки ниже."
)


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(HELP_TEXT, reply_markup=main_menu())


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(HELP_TEXT, reply_markup=main_menu())


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext) -> None:
    current = await state.get_state()
    if message.from_user is not None:
        await auth_store.clear(message.from_user.id)
    if current is None:
        await message.answer("Нечего отменять.", reply_markup=main_menu())
        return
    await state.clear()
    await message.answer("Сценарий отменён.", reply_markup=main_menu())


@router.callback_query(F.data == "cancel")
async def cb_cancel(query: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    if query.from_user is not None:
        await auth_store.clear(query.from_user.id)
    if query.message is not None:
        await query.message.answer("Сценарий отменён.", reply_markup=main_menu())
    await query.answer()
