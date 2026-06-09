"""Общие хендлеры: /start, /help, /cancel, главное меню. ARCHITECTURE.md §10.6."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app.bot.keyboards import (
    BTN_ACCOUNTS,
    BTN_CAMPAIGNS,
    BTN_MENU,
    BTN_STATUS,
    main_menu,
    reply_menu_kb,
)
from app.telegram.auth import auth_store

router = Router(name="common")


HELP_TEXT = (
    "🤖 <b>telegram-outreach</b>\n"
    "Многоаккаунтная рассылка и инвайты.\n\n"
    "📊 <b>Мониторинг</b>\n"
    "/status — кампании и аккаунты\n"
    "/floodwait — кто сейчас в FloodWait\n"
    "/campaigns — список кампаний\n\n"
    "👤 <b>Аккаунты</b>\n"
    "/accounts — список аккаунтов\n"
    "/add_account — добавить (поддержка 2FA)\n"
    "/remove_account &lt;phone|id&gt; — удалить\n"
    "/spamcheck — проверка через SpamBot\n\n"
    "📢 <b>Кампании и шаблоны</b>\n"
    "/new_campaign — создать кампанию\n"
    "/templates · /new_template · /del_template — шаблоны\n"
    "/pause · /resume · /stop &lt;id&gt; — управление\n\n"
    "📤 <b>Экспорт и настройки</b>\n"
    "/export_report &lt;id&gt; — CSV по кампании\n"
    "/export_log [today|yesterday|N] — логи\n"
    "/settings · /set ключ значение — настройки\n\n"
    "/cancel — выйти из текущего сценария\n\n"
    "Внизу — кнопки быстрого доступа."
)


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    # Ставим постоянную нижнюю клавиатуру, затем показываем inline-меню действий.
    await message.answer(
        "Клавиатура меню включена. Полный список команд — в кнопке «Меню» "
        "слева от поля ввода.",
        reply_markup=reply_menu_kb(),
    )
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


# ---------------------------------------------------------------------------
# Reply-клавиатура (§10.6). Хэндлеры в common-роутере (он включается ПЕРВЫМ) и
# БЕЗ StateFilter — перехватывают кнопки в любом состоянии РАНЬШЕ FSM-хэндлеров,
# чтобы текст кнопки не попал во ввод сценария (телефон/чат/шаблон). Каждая кнопка
# корректно выходит из сценария (FSM + auth_store), как /cancel, затем делегирует
# существующему обработчику (ленивый импорт — без циклических импортов на модуле).
# ---------------------------------------------------------------------------


async def _exit_scenario(message: Message, state: FSMContext) -> None:
    await state.clear()
    if message.from_user is not None:
        await auth_store.clear(message.from_user.id)


@router.message(F.text == BTN_MENU)
async def rb_menu(message: Message, state: FSMContext) -> None:
    await _exit_scenario(message, state)
    await message.answer("Действия:", reply_markup=main_menu())


@router.message(F.text == BTN_STATUS)
async def rb_status(message: Message, state: FSMContext) -> None:
    await _exit_scenario(message, state)
    from app.bot.handlers.status import cmd_status

    await cmd_status(message)


@router.message(F.text == BTN_ACCOUNTS)
async def rb_accounts(message: Message, state: FSMContext) -> None:
    await _exit_scenario(message, state)
    from app.bot.handlers.accounts import list_accounts

    await list_accounts(message)


@router.message(F.text == BTN_CAMPAIGNS)
async def rb_campaigns(message: Message, state: FSMContext) -> None:
    await _exit_scenario(message, state)
    from app.bot.handlers.campaigns import list_campaigns

    await list_campaigns(message)
