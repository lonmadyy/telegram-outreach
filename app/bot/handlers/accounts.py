"""Хендлеры аккаунтов: /accounts, /add_account FSM. ARCHITECTURE.md §10.2, §10.3."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from loguru import logger

from app.bot.keyboards import cancel_kb, main_menu
from app.bot.states import AddAccount
from app.config import settings
from app.db.repositories import accounts as accounts_repo
from app.db.repositories import logs as logs_repo
from app.db.session import session_scope
from app.telegram.auth import (
    CodeExpiredError,
    FloodError,
    InvalidCodeError,
    InvalidPasswordError,
    InvalidPhoneError,
    PhoneBannedError,
    auth_store,
    finalize,
    start_login,
    submit_code,
    submit_password,
)
from app.telegram.client_factory import normalize_phone, parse_proxy, session_path_for

router = Router(name="accounts")


def _format_account_row(acc) -> str:
    parts = [
        f"<b>{acc.phone}</b>",
        f"id={acc.id}",
        f"status={acc.status.value}",
    ]
    if acc.username:
        parts.append(f"@{acc.username}")
    if acc.warmup_until:
        parts.append(f"warmup_until={acc.warmup_until:%Y-%m-%d %H:%M}")
    if acc.proxy_url:
        parts.append("proxy=yes")
    if acc.spam_unlock_at:
        parts.append(f"unlock_at={acc.spam_unlock_at:%Y-%m-%d %H:%M}")
    return " | ".join(parts)


@router.message(Command("accounts"))
@router.callback_query(F.data == "menu:accounts")
async def list_accounts(event) -> None:
    async with session_scope() as session:
        items = await accounts_repo.list_accounts(session)

    if isinstance(event, CallbackQuery):
        target = event.message
        await event.answer()
    else:
        target = event

    if target is None:
        return

    if not items:
        await target.answer(
            "Аккаунтов пока нет. Добавьте через /add_account.",
            reply_markup=main_menu(),
        )
        return

    text = "<b>Аккаунты:</b>\n\n" + "\n".join(_format_account_row(a) for a in items)
    await target.answer(text, reply_markup=main_menu())


@router.message(Command("add_account"))
async def cmd_add_account(message: Message, state: FSMContext) -> None:
    if message.from_user is None:
        return
    await auth_store.clear(message.from_user.id)
    await state.set_state(AddAccount.waiting_phone)
    await message.answer(
        "Введите номер телефона аккаунта в международном формате "
        "(например, <code>+79991234567</code>).",
        reply_markup=cancel_kb(),
    )


@router.message(AddAccount.waiting_phone, F.text)
async def on_phone(message: Message, state: FSMContext) -> None:
    if message.from_user is None or message.text is None:
        return
    phone = normalize_phone(message.text)
    if not phone or len(phone) < 8:
        await message.answer(
            "Не похоже на телефонный номер. Введите в формате <code>+79991234567</code>.",
            reply_markup=cancel_kb(),
        )
        return
    try:
        async with session_scope() as session:
            existing = await accounts_repo.get_by_phone(session, phone)
            if existing is not None:
                await message.answer(
                    f"Аккаунт {phone} уже добавлен (id={existing.id}).",
                    reply_markup=main_menu(),
                )
                await state.clear()
                return
        sess = await start_login(phone)
    except InvalidPhoneError as e:
        await message.answer(f"{e}. Введите номер заново или /cancel.", reply_markup=cancel_kb())
        return
    except PhoneBannedError as e:
        await message.answer(f"{e}.", reply_markup=main_menu())
        await state.clear()
        return
    except FloodError as e:
        await message.answer(
            f"FloodWait {e.seconds}s — Telegram просит подождать. Попробуйте позже.",
            reply_markup=main_menu(),
        )
        await state.clear()
        return
    except Exception as e:
        logger.exception("start_login failed for {}", phone)
        await message.answer(f"Не удалось запустить авторизацию: {e}", reply_markup=main_menu())
        await state.clear()
        return

    await auth_store.set(message.from_user.id, sess)
    await state.set_state(AddAccount.waiting_code)
    await message.answer(
        "Код отправлен. Введите его сюда. "
        "Если коды приходят в Telegram, скопируйте оттуда.",
        reply_markup=cancel_kb(),
    )


@router.message(AddAccount.waiting_code, F.text)
async def on_code(message: Message, state: FSMContext) -> None:
    if message.from_user is None or message.text is None:
        return
    sess = await auth_store.get(message.from_user.id)
    if sess is None:
        await message.answer(
            "Сессия истекла, начните заново через /add_account.",
            reply_markup=main_menu(),
        )
        await state.clear()
        return

    try:
        completed = await submit_code(sess, message.text)
    except InvalidCodeError as e:
        await message.answer(f"{e}. Введите код заново или /cancel.", reply_markup=cancel_kb())
        return
    except CodeExpiredError as e:
        await auth_store.clear(message.from_user.id)
        await message.answer(f"{e}.", reply_markup=main_menu())
        await state.clear()
        return
    except FloodError as e:
        await auth_store.clear(message.from_user.id)
        await message.answer(f"FloodWait {e.seconds}s.", reply_markup=main_menu())
        await state.clear()
        return
    except Exception as e:
        logger.exception("submit_code failed")
        await auth_store.clear(message.from_user.id)
        await message.answer(f"Ошибка: {e}", reply_markup=main_menu())
        await state.clear()
        return

    if not completed:
        await state.set_state(AddAccount.waiting_password)
        await message.answer(
            "У аккаунта включена двухфакторная аутентификация. Введите облачный пароль.",
            reply_markup=cancel_kb(),
        )
        return

    await state.set_state(AddAccount.waiting_proxy)
    await _ask_proxy(message)


@router.message(AddAccount.waiting_password, F.text)
async def on_password(message: Message, state: FSMContext) -> None:
    if message.from_user is None or message.text is None:
        return
    sess = await auth_store.get(message.from_user.id)
    if sess is None:
        await message.answer(
            "Сессия истекла, начните заново через /add_account.",
            reply_markup=main_menu(),
        )
        await state.clear()
        return

    try:
        await submit_password(sess, message.text)
    except InvalidPasswordError as e:
        await message.answer(f"{e}. Введите пароль заново или /cancel.", reply_markup=cancel_kb())
        return
    except FloodError as e:
        await auth_store.clear(message.from_user.id)
        await message.answer(f"FloodWait {e.seconds}s.", reply_markup=main_menu())
        await state.clear()
        return
    except Exception as e:
        logger.exception("submit_password failed")
        await auth_store.clear(message.from_user.id)
        await message.answer(f"Ошибка: {e}", reply_markup=main_menu())
        await state.clear()
        return

    # Удалить введённый пароль из чата (best-effort)
    try:
        await message.delete()
    except Exception:
        pass

    await state.set_state(AddAccount.waiting_proxy)
    await _ask_proxy(message)


async def _ask_proxy(message: Message) -> None:
    await message.answer(
        "Указать прокси для этого аккаунта?\n"
        "Формат: <code>socks5://user:pass@host:port</code> "
        "или просто <code>socks5://host:port</code>.\n\n"
        "Отправьте URL или слово <b>skip</b>, чтобы пропустить.",
        reply_markup=cancel_kb(),
    )


@router.message(AddAccount.waiting_proxy, F.text)
async def on_proxy(message: Message, state: FSMContext) -> None:
    if message.from_user is None or message.text is None:
        return
    sess = await auth_store.get(message.from_user.id)
    if sess is None:
        await message.answer(
            "Сессия истекла, начните заново через /add_account.",
            reply_markup=main_menu(),
        )
        await state.clear()
        return

    raw = message.text.strip()
    proxy_url: str | None
    if raw.lower() in ("skip", "пропустить", "-", "no", "нет"):
        proxy_url = None
    else:
        proxy_url = raw
        try:
            parse_proxy(proxy_url)
        except ValueError as e:
            await message.answer(f"Невалидный прокси: {e}. Введите снова или skip.")
            return

    # Сохраняем в БД
    me = sess.me
    if me is None:
        try:
            me = await sess.client.get_me()
            sess.me = me
        except Exception:
            logger.exception("get_me() after auth failed")

    async with session_scope() as session:
        acc = await accounts_repo.create_account(
            session,
            phone=sess.phone,
            session_path=session_path_for(sess.phone),
            tg_user_id=me.id if me is not None else None,
            username=getattr(me, "username", None),
            first_name=getattr(me, "first_name", None),
            proxy_url=proxy_url,
            warmup_hours=settings.warmup_duration_hours,
        )
        await logs_repo.log_event(
            session,
            level="info",
            event_type="account_added",
            account_id=acc.id,
            message=f"Аккаунт {acc.phone} добавлен (warmup на {settings.warmup_duration_hours}ч)",
            payload={"with_proxy": proxy_url is not None, "username": acc.username},
        )

    await finalize(sess)
    await auth_store.clear(message.from_user.id)
    await state.clear()

    await message.answer(
        f"Аккаунт <b>{sess.phone}</b> добавлен.\n"
        f"id={acc.id} | status={acc.status.value} | "
        f"warmup до {acc.warmup_until:%Y-%m-%d %H:%M} UTC",
        reply_markup=main_menu(),
    )
