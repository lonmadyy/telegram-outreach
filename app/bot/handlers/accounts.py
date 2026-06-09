"""Хендлеры аккаунтов: /accounts, /add_account FSM, /remove_account. ARCHITECTURE.md §10.2, §10.3."""

from __future__ import annotations

import os

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from loguru import logger

from app.bot import formatting as fmt
from app.bot.keyboards import cancel_kb, confirm_kb, main_menu
from app.bot.states import AddAccount
from app.config import settings
from app.db.models import AccountStatus
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
from app.telegram.client_factory import (
    create_client,
    normalize_phone,
    parse_account_ref,
    parse_proxy,
    session_file_paths,
    session_path_for,
)
from app.telegram import warmup as warmup_mod
from app.telegram.worker_pool import worker_pool
from app.scheduler.jobs import get_scheduler

router = Router(name="accounts")


@router.message(Command("accounts"))
@router.callback_query(F.data == "menu:accounts")
async def list_accounts(event) -> None:
    async with session_scope() as session:
        all_items = await accounts_repo.list_accounts(session)

    # Отключённые (disabled, «удалённые») скрываем из основного списка (§10.2),
    # показываем лишь счётчиком для прозрачности.
    items = [a for a in all_items if a.status != AccountStatus.disabled]
    disabled_count = len(all_items) - len(items)
    disabled_note = (
        f"\n\n<i>+{disabled_count} отключённых (disabled)</i>" if disabled_count else ""
    )

    if isinstance(event, CallbackQuery):
        target = event.message
        await event.answer()
    else:
        target = event

    if target is None:
        return

    if not items:
        await target.answer(
            "Активных аккаунтов нет. Добавьте через /add_account." + disabled_note,
            reply_markup=main_menu(),
        )
        return

    header = fmt.section_header("👤", "Аккаунты", f"{len(items)} активных")
    body = "\n\n".join(fmt.account_card(a) for a in items)
    await target.answer(f"{header}\n\n{body}{disabled_note}", reply_markup=main_menu())


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

    # Warmup-подписка на каналы, пока клиент ещё авторизован (§5.1, MVP-5). Best-effort.
    joined = 0
    try:
        joined = await warmup_mod.subscribe_to_warmup_channels(sess.client)
    except Exception:
        logger.exception("warmup subscribe failed for {}", sess.phone)

    # Отключаем клиент авторизации (освобождает .session для воркера).
    await finalize(sess)
    await auth_store.clear(message.from_user.id)
    await state.clear()

    # Поднимаем воркер сразу — аккаунт начинает греться без рестарта (MVP-5, §10.3).
    started = False
    try:
        worker = await worker_pool.start_for(acc)
        started = worker is not None
        if started:
            sched = get_scheduler()
            if sched is not None:
                sched.add_spamcheck_for_account(acc.id)
    except Exception:
        logger.exception("start worker for new account {} failed", acc.id)

    start_note = (
        "воркер запущен, аккаунт греется"
        if started
        else "воркер поднимется при следующем рестарте"
    )
    await message.answer(
        f"✅ <b>Аккаунт {sess.phone} добавлен</b> (#{acc.id})\n"
        f"🔥 Прогрев до {acc.warmup_until:%d.%m %H:%M} UTC\n"
        f"Подписок на каналы: {joined} · {start_note}.",
        reply_markup=main_menu(),
    )


# ---------------------------------------------------------------------------
# /remove_account — мягкое удаление аккаунта (§10.2). Деструктивно → подтверждение.
# ---------------------------------------------------------------------------


@router.message(Command("remove_account"))
async def cmd_remove_account(message: Message) -> None:
    if message.text is None:
        return
    parts = message.text.split(maxsplit=1)
    ref = parse_account_ref(parts[1]) if len(parts) > 1 else None
    if ref is None:
        await message.answer(
            "Использование: <code>/remove_account &lt;phone|id&gt;</code>\n"
            "Например: <code>/remove_account +77788786614</code> или "
            "<code>/remove_account 3</code>",
            reply_markup=main_menu(),
        )
        return

    kind, val = ref
    async with session_scope() as session:
        if kind == "id":
            acc = await accounts_repo.get_by_id(session, int(val))
        else:
            acc = await accounts_repo.get_by_phone(session, str(val))

    if acc is None:
        await message.answer(f"Аккаунт <code>{val}</code> не найден.", reply_markup=main_menu())
        return
    if acc.status == AccountStatus.disabled:
        await message.answer(
            f"Аккаунт {acc.phone} (id={acc.id}) уже отключён (disabled).",
            reply_markup=main_menu(),
        )
        return

    await message.answer(
        f"Удалить аккаунт?\n"
        f"<b>{acc.phone}</b> | id={acc.id} | status={acc.status.value}\n\n"
        f"Воркер будет остановлен, сессия разлогинена в Telegram, файл .session удалён. "
        f"История кампаний и дедуп сохранятся (статус → <b>disabled</b>).",
        reply_markup=confirm_kb(f"rmacc:do:{acc.id}"),
    )


@router.callback_query(F.data.startswith("rmacc:do:"))
async def on_remove_confirm(query: CallbackQuery) -> None:
    raw = (query.data or "").removeprefix("rmacc:do:")
    try:
        account_id = int(raw)
    except ValueError:
        await query.answer("Битый id", show_alert=True)
        return

    async with session_scope() as session:
        acc = await accounts_repo.get_by_id(session, account_id)
    if acc is None:
        await query.answer("Аккаунт не найден", show_alert=True)
        if query.message is not None:
            await query.message.answer(
                "Аккаунт не найден или уже удалён.", reply_markup=main_menu()
            )
        return

    phone = acc.phone
    proxy_url = acc.proxy_url
    result = await _remove_account_fully(account_id, phone, proxy_url)

    if query.message is not None:
        await query.message.answer(
            f"Аккаунт <b>{phone}</b> (id={account_id}) отключён.\n{result}",
            reply_markup=main_menu(),
        )
    await query.answer("Готово")


async def _remove_account_fully(
    account_id: int, phone: str, proxy_url: str | None
) -> str:
    """Оркестрация мягкого удаления (§10.2): стоп воркера → снятие spamcheck →
    log_out + удаление .session → статус disabled. Каждый внешний шаг best-effort:
    сбой одного (напр. log_out для dead-аккаунта) не должен срывать остальные."""
    notes: list[str] = []

    # 1. Остановить воркер (освобождает .session-файл перед log_out).
    try:
        await worker_pool.stop_for(account_id)
        notes.append("воркер остановлен")
    except Exception:
        logger.exception("remove: stop_for failed for {}", account_id)

    # 2. Снять spamcheck-задачу из планировщика.
    try:
        sched = get_scheduler()
        if sched is not None:
            sched.remove_spamcheck_for_account(account_id)
            notes.append("spamcheck снят")
    except Exception:
        logger.exception("remove: remove_spamcheck failed for {}", account_id)

    # 3. Log out в Telegram + удаление .session (временный клиент, best-effort).
    logged_out = False
    try:
        client = create_client(phone=phone, proxy_url=proxy_url)
        try:
            await client.connect()
            if await client.is_user_authorized():
                await client.log_out()  # серверный logout + удаляет .session-файл
                logged_out = True
        finally:
            try:
                if client.is_connected():
                    await client.disconnect()
            except Exception:
                pass
    except Exception:
        logger.exception("remove: log_out failed for {}", phone)
    notes.append("сессия разлогинена" if logged_out else "разлогин пропущен (best-effort)")

    # 4. Подчистить оставшиеся файлы сессии (если log_out не удалил).
    removed_files = 0
    for path in session_file_paths(settings.sessions_path, phone):
        try:
            if os.path.exists(path):
                os.remove(path)
                removed_files += 1
        except Exception:
            logger.exception("remove: unlink {} failed", path)
    if removed_files:
        notes.append(".session удалён")

    # 5. Статус disabled + лог (запись остаётся — FK/история целы).
    async with session_scope() as session:
        await accounts_repo.set_disabled(session, account_id=account_id)
        await logs_repo.log_event(
            session,
            level="info",
            event_type="account_disabled",
            account_id=account_id,
            message=f"Аккаунт {phone} отключён через /remove_account",
            payload={"logged_out": logged_out, "removed_files": removed_files},
        )
    return " · ".join(notes)
