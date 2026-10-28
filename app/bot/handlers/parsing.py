"""Парсинг групп: /parse, /parses. ARCHITECTURE.md §10.2, §20.

Сценарий: список групп → выбор аккаунта → опции (переключатели) → запуск в фоне.
Статус — одно сообщение, обновляется раз в ~15 с; итог — TXT + ZIP по группам +
кнопка «Создать кампанию из этого списка» (переход в мастер /new_campaign).
"""

from __future__ import annotations

import io
import zipfile
from html import escape as html_escape

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from loguru import logger

from app.bot import formatting as fmt
from app.bot.keyboards import campaign_type_kb, cancel_kb, main_menu
from app.bot.states import NewCampaign, ParseGroups
from app.db.repositories import accounts as accounts_repo
from app.db.repositories import parses as parses_repo
from app.db.session import session_scope
from app.parsing import runner
from app.parsing.results import load_outputs
from app.utils.group_targets import MAX_GROUPS, GroupToken, parse_group_lines

router = Router(name="parsing")

DAYS_CHOICES = (7, 30, 90)
MSG_LIMIT_MAX = 1_000_000
REPORT_LINES_IN_MESSAGE = 20

_STATUS_TITLE = {
    parses_repo.STATUS_DONE: "✅ Парсинг #{id} завершён",
    parses_repo.STATUS_STOPPED: "⏹ Парсинг #{id} остановлен, собранное сохранено",
    parses_repo.STATUS_FAILED: "❌ Парсинг #{id} прерван ошибкой",
    parses_repo.STATUS_INTERRUPTED: "⚠️ Парсинг #{id} прерван перезапуском бота",
    parses_repo.STATUS_RUNNING: "⏳ Парсинг #{id} ещё идёт",
}

_DROP_LABELS = (
    ("no_username", "без username"),
    ("bot", "ботов"),
    ("deleted", "удалённых"),
    ("admin", "админов"),
    ("own_account", "наших аккаунтов"),
)


# ---------------------------------------------------------------------------
# Шаг 1: список групп
# ---------------------------------------------------------------------------


@router.message(Command("parse"))
@router.callback_query(F.data == "menu:parse")
async def cmd_parse(event, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(ParseGroups.waiting_groups)
    if isinstance(event, CallbackQuery):
        target = event.message
        await event.answer()
    else:
        target = event
    if target is None:
        return
    await target.answer(
        "🔎 <b>Парсинг групп</b>\n\n"
        "Пришлите список групп <b>по одной в строке</b> (текстом или TXT-файлом, "
        f"до {MAX_GROUPS}):\n"
        "• <code>@group_name</code> или <code>t.me/group_name</code>;\n"
        "• приглашение <code>t.me/+AbCd…</code> (закрытая группа);\n"
        "• id <code>-100…</code> или название группы, где аккаунт уже состоит.\n\n"
        "Соберу @username участников и тех, кто писал в группе, без повторов.",
        reply_markup=cancel_kb(),
    )


async def _read_text(message: Message) -> str | None:
    if message.document is None:
        return message.text or ""
    if message.bot is None:
        return None
    file = await message.bot.get_file(message.document.file_id)
    if file.file_path is None:
        return None
    buffer = await message.bot.download_file(file.file_path)
    if buffer is None:
        return None
    raw = buffer.read()
    for enc in ("utf-8-sig", "cp1251"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return None


@router.message(ParseGroups.waiting_groups, F.document | (F.text & ~F.text.startswith("/")))
async def on_groups(message: Message, state: FSMContext) -> None:
    content = await _read_text(message)
    if content is None:
        await message.answer(
            "Не удалось прочитать файл (ожидаю TXT в UTF-8 или CP1251).",
            reply_markup=cancel_kb(),
        )
        return
    tokens, invalid = parse_group_lines(content)
    if not tokens:
        await message.answer(
            "Не нашёл ни одной группы. Пришлите по одной в строке или /cancel.",
            reply_markup=cancel_kb(),
        )
        return
    if len(tokens) > MAX_GROUPS:
        await message.answer(
            f"Групп {len(tokens)}, за один раз можно до {MAX_GROUPS}. "
            "Разделите список на части.",
            reply_markup=cancel_kb(),
        )
        return

    async with session_scope() as session:
        accounts = await accounts_repo.list_accounts(session)
    busy = runner.busy_account_ids()
    candidates = [a for a in runner.parse_candidates(accounts) if a.id not in busy]
    if not candidates:
        await state.clear()
        await message.answer(
            "Нет свободного подключённого аккаунта для парсинга "
            "(все отключены или уже парсят). Проверьте /accounts.",
            reply_markup=main_menu(),
        )
        return

    await state.update_data(groups=[[t.kind, t.value] for t in tokens])
    await state.set_state(ParseGroups.waiting_account)
    invalid_note = ""
    if invalid:
        preview = "\n".join(invalid[:5])
        invalid_note = (
            f"\nНе распознано строк: {len(invalid)} (пропущу):\n<code>{preview}</code>\n"
        )
    rows = []
    for a in candidates:
        emoji, status = fmt.account_status(a)
        name = f"@{a.username}" if a.username else a.phone
        rows.append(
            [InlineKeyboardButton(text=f"{emoji} {name} · {status}", callback_data=f"pacc:{a.id}")]
        )
    rows.append([InlineKeyboardButton(text="Отмена", callback_data="cancel")])
    await message.answer(
        f"Групп в списке: <b>{len(tokens)}</b>.{invalid_note}\n"
        "Каким аккаунтом парсить? На время парсинга он <b>не будет рассылать</b>, "
        "потом вернётся в работу сам.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )


# ---------------------------------------------------------------------------
# Шаг 2–3: аккаунт и опции
# ---------------------------------------------------------------------------


def _options_text(opts: runner.ParseOptions, n_groups: int) -> str:
    if opts.msg_limit:
        depth = f"последние {fmt.num(opts.msg_limit)} сообщений"
    elif opts.days:
        depth = f"за {opts.days} дней"
    else:
        depth = "вся история"
    lines = [
        f"<b>Настройки парсинга</b> · групп в списке: {n_groups}",
        "",
        f"• Участники групп: {'да' if opts.members else 'нет'}",
        f"• Писавшие в группах: {'да' if opts.senders else 'нет'}"
        + (f" ({depth})" if opts.senders else ""),
        f"• Вступать в закрытые группы по ссылке: {'да' if opts.join_private else 'нет'}",
        "",
        "Отсеиваю всегда: ботов, удалённых, без username, админов группы, наши "
        "аккаунты и тех, кто уже есть в реестре обработанных.",
    ]
    return "\n".join(lines)


def _options_kb(opts: runner.ParseOptions) -> InlineKeyboardMarkup:
    def mark(flag: bool) -> str:
        return "✅" if flag else "☐"

    depth_row = []
    for d in DAYS_CHOICES:
        on = opts.days == d and not opts.msg_limit
        depth_row.append(
            InlineKeyboardButton(text=f"{'• ' if on else ''}{d} дн", callback_data=f"popt:days:{d}")
        )
    all_on = opts.days is None and not opts.msg_limit
    depth_row.append(
        InlineKeyboardButton(text=f"{'• ' if all_on else ''}вся", callback_data="popt:days:0")
    )
    rows = [
        [
            InlineKeyboardButton(text=f"{mark(opts.members)} Участники", callback_data="popt:members"),
            InlineKeyboardButton(text=f"{mark(opts.senders)} Писавшие", callback_data="popt:senders"),
        ],
    ]
    if opts.senders:
        rows.append(depth_row)
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{'• ' if opts.msg_limit else ''}N последних сообщений",
                    callback_data="popt:limit",
                )
            ]
        )
    rows += [
        [
            InlineKeyboardButton(
                text=f"{mark(opts.join_private)} Вступать в закрытые по ссылке",
                callback_data="popt:join",
            )
        ],
        [
            InlineKeyboardButton(text="▶️ Запустить", callback_data="popt:go"),
            InlineKeyboardButton(text="Отмена", callback_data="cancel"),
        ],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _get_opts(state: FSMContext) -> tuple[runner.ParseOptions, dict]:
    data = await state.get_data()
    return runner.ParseOptions.from_dict(data.get("options") or {}), data


@router.callback_query(ParseGroups.waiting_account, F.data.startswith("pacc:"))
async def on_pick_account(query: CallbackQuery, state: FSMContext) -> None:
    try:
        account_id = int((query.data or "").removeprefix("pacc:"))
    except ValueError:
        await query.answer("Неверный аккаунт", show_alert=True)
        return
    opts = runner.ParseOptions()
    await state.update_data(account_id=account_id, options=opts.to_dict())
    await state.set_state(ParseGroups.waiting_options)
    data = await state.get_data()
    if query.message is not None:
        await query.message.answer(
            _options_text(opts, len(data["groups"])), reply_markup=_options_kb(opts)
        )
    await query.answer()


@router.callback_query(ParseGroups.waiting_options, F.data.startswith("popt:"))
async def on_option(query: CallbackQuery, state: FSMContext) -> None:
    action = (query.data or "").removeprefix("popt:")
    opts, data = await _get_opts(state)

    if action == "go":
        await _launch(query, state, opts, data)
        return
    if action == "limit":
        await state.set_state(ParseGroups.waiting_msg_limit)
        if query.message is not None:
            await query.message.answer(
                "Сколько последних сообщений просмотреть в каждой группе? "
                f"Число от 100 до {fmt.num(MSG_LIMIT_MAX)}.",
                reply_markup=cancel_kb(),
            )
        await query.answer()
        return
    if action == "members":
        opts.members = not opts.members
    elif action == "senders":
        opts.senders = not opts.senders
    elif action == "join":
        opts.join_private = not opts.join_private
    elif action.startswith("days:"):
        days = int(action.removeprefix("days:") or 0)
        opts.days = days or None
        opts.msg_limit = None
    await state.update_data(options=opts.to_dict())
    if query.message is not None:
        try:
            await query.message.edit_text(
                _options_text(opts, len(data["groups"])), reply_markup=_options_kb(opts)
            )
        except Exception:
            pass  # «message is not modified» при повторном нажатии
    await query.answer()


@router.message(ParseGroups.waiting_msg_limit, F.text & ~F.text.startswith("/"))
async def on_msg_limit(message: Message, state: FSMContext) -> None:
    raw = (message.text or "").replace(" ", "").strip()
    if not raw.isdigit() or not (100 <= int(raw) <= MSG_LIMIT_MAX):
        await message.answer(
            f"Нужно целое число от 100 до {fmt.num(MSG_LIMIT_MAX)}.", reply_markup=cancel_kb()
        )
        return
    opts, data = await _get_opts(state)
    opts.msg_limit = int(raw)
    opts.days = None
    await state.update_data(options=opts.to_dict())
    await state.set_state(ParseGroups.waiting_options)
    await message.answer(
        _options_text(opts, len(data["groups"])), reply_markup=_options_kb(opts)
    )


# ---------------------------------------------------------------------------
# Запуск, статус, итог
# ---------------------------------------------------------------------------


def _stop_kb(job_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="⛔ Остановить", callback_data=f"pstop:{job_id}")]]
    )


def _progress_text(p: runner.Progress) -> str:
    seen = f" · просмотрено {fmt.num(p.seen)}" if p.seen else ""
    return (
        f"⏳ <b>Парсинг #{p.job_id}</b>\n"
        f"Группа {p.group_no} из {p.groups_total}: {html_escape(p.label)}\n"
        f"Этап: {p.stage}{seen}\n"
        f"Собрано @username: <b>{fmt.num(p.collected)}</b>"
    )


async def _launch(
    query: CallbackQuery, state: FSMContext, opts: runner.ParseOptions, data: dict
) -> None:
    if not opts.members and not opts.senders:
        await query.answer("Включите участников или писавших", show_alert=True)
        return
    message = query.message
    bot = query.bot
    if message is None or bot is None:
        await query.answer()
        return
    tokens = [GroupToken(k, v) for k, v in data["groups"]]
    chat_id = message.chat.id
    status_msg = await message.answer("⏳ Запускаю парсинг…")

    async def on_progress(p: runner.Progress) -> None:
        await bot.edit_message_text(
            _progress_text(p),
            chat_id=chat_id,
            message_id=status_msg.message_id,
            reply_markup=_stop_kb(p.job_id),
        )

    async def on_done(job_id: int, status: str) -> None:
        try:
            await bot.edit_message_reply_markup(
                chat_id=chat_id, message_id=status_msg.message_id, reply_markup=None
            )
        except Exception:
            pass
        await send_parse_result(bot, chat_id, job_id)

    try:
        job_id = await runner.start_parse(
            account_id=data["account_id"],
            chat_id=chat_id,
            tokens=tokens,
            options=opts,
            on_progress=on_progress,
            on_done=on_done,
        )
    except runner.ParseStartError as e:
        await status_msg.edit_text(str(e))
        await state.clear()
        await query.answer()
        return
    await state.clear()
    await status_msg.edit_text(
        f"⏳ <b>Парсинг #{job_id}</b> запущен: {len(tokens)} групп.\n"
        "Статус будет обновляться здесь, итог пришлю отдельным сообщением.",
        reply_markup=_stop_kb(job_id),
    )
    await query.answer("Запущено")


@router.callback_query(F.data.startswith("pstop:"))
async def on_stop(query: CallbackQuery) -> None:
    try:
        job_id = int((query.data or "").removeprefix("pstop:"))
    except ValueError:
        await query.answer()
        return
    if runner.stop_parse(job_id):
        await query.answer("Останавливаю, собранное сохраню…")
    else:
        await query.answer("Это задание уже не выполняется", show_alert=True)


def _summary_text(job, outputs) -> str:
    title = _STATUS_TITLE.get(job.status, "Парсинг #{id}").format(id=job.id)
    report = job.report or []
    ok = sum(1 for r in report if r.get("ok"))
    lines = [f"<b>{title}</b>"]
    if job.error:
        lines.append(f"Причина: {html_escape(job.error)}")
    lines += [
        f"Групп пройдено: {len(report)} из {len(job.groups)}, открылись: {ok}",
        f"Итог: <b>{fmt.num(len(outputs.all_usernames))}</b> @username без повторов",
    ]
    dropped = job.dropped or {}
    parts = [f"{label} {fmt.num(dropped[key])}" for key, label in _DROP_LABELS if dropped.get(key)]
    if outputs.skipped:
        parts.append(f"уже обработанных {fmt.num(outputs.skipped)}")
    if parts:
        lines.append("Отсеяно: " + ", ".join(parts))
    lines.append("")
    for i, r in enumerate(report[:REPORT_LINES_IN_MESSAGE], start=1):
        name = html_escape(r.get("title") or r.get("label") or "?")
        if r.get("ok"):
            line = (
                f"{i}. {name}: участники {fmt.num(r.get('members', 0))}, "
                f"писавшие {fmt.num(r.get('senders', 0))}, новых в итог +{fmt.num(r.get('added', 0))}"
            )
        else:
            line = f"{i}. {name}: ✗"
        if r.get("note"):
            line += f" <i>({html_escape(r['note'])})</i>"
        lines.append(line)
    if len(report) > REPORT_LINES_IN_MESSAGE:
        lines.append(f"…ещё {len(report) - REPORT_LINES_IN_MESSAGE}, полный отчёт в архиве.")
    # Режем по строкам, а не по символам — иначе можно разорвать HTML-тег.
    text, total = [], 0
    for line in lines:
        if total + len(line) > 3800:
            text.append("…полный отчёт в архиве.")
            break
        text.append(line)
        total += len(line) + 1
    return "\n".join(text)


def _report_txt(job) -> bytes:
    out = [f"Парсинг #{job.id}, статус: {job.status}"]
    for i, r in enumerate(job.report or [], start=1):
        out.append(
            f"{i:02d}. {r.get('title') or r.get('label')} | "
            f"{'ok' if r.get('ok') else 'ошибка'} | участники {r.get('members', 0)} | "
            f"писавшие {r.get('senders', 0)} | новых {r.get('added', 0)} | {r.get('note', '')}"
        )
    return ("\n".join(out) + "\n").encode("utf-8")


def _result_kb(job_id: int, has_users: bool) -> InlineKeyboardMarkup | None:
    if not has_users:
        return None
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📢 Создать кампанию из этого списка", callback_data=f"pcamp:{job_id}")]
        ]
    )


async def send_parse_result(bot: Bot, chat_id: int, job_id: int) -> None:
    loaded = await load_outputs(job_id)
    if loaded is None:
        await bot.send_message(chat_id, f"Парсинг #{job_id} не найден.")
        return
    job, outputs = loaded
    await bot.send_message(chat_id, _summary_text(job, outputs))
    if not outputs.all_usernames:
        await bot.send_message(
            chat_id,
            "Подходящих @username не нашлось: все отсеяны или группы не открылись. "
            "Файлы не отправляю.",
            reply_markup=main_menu(),
        )
        return
    # Отчёт кладём в тот же ZIP, что и файлы по группам.
    buf = io.BytesIO(outputs.zip_bytes)
    with zipfile.ZipFile(buf, "a", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("00_отчёт.txt", _report_txt(job))
    await bot.send_document(
        chat_id,
        BufferedInputFile(outputs.all_txt, filename=f"parse_{job_id}_all.txt"),
        caption=f"Все @username без повторов: {fmt.num(len(outputs.all_usernames))}",
    )
    await bot.send_document(
        chat_id,
        BufferedInputFile(buf.getvalue(), filename=f"parse_{job_id}_by_groups.zip"),
        caption="По каждой группе отдельно + отчёт",
        reply_markup=_result_kb(job_id, True),
    )


# ---------------------------------------------------------------------------
# История и переход в кампанию
# ---------------------------------------------------------------------------


@router.message(Command("parses"))
async def cmd_parses(message: Message) -> None:
    async with session_scope() as session:
        jobs = await parses_repo.list_recent(session, limit=10)
    if not jobs:
        await message.answer("Заданий парсинга ещё не было. Начать: /parse", reply_markup=main_menu())
        return
    rows = []
    for j in jobs:
        icon = _STATUS_TITLE.get(j.status, "•").split(" ", 1)[0]
        when = fmt.fmt_dt(j.created_at)
        rows.append(
            [InlineKeyboardButton(text=f"{icon} #{j.id} · {when} · {len(j.groups)} групп", callback_data=f"pres:{j.id}")]
        )
    await message.answer(
        "Последние задания парсинга (нажмите, чтобы получить файлы):",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )


@router.callback_query(F.data.startswith("pres:"))
async def on_result_request(query: CallbackQuery) -> None:
    try:
        job_id = int((query.data or "").removeprefix("pres:"))
    except ValueError:
        await query.answer()
        return
    await query.answer()
    if query.bot is not None and query.message is not None:
        if runner.is_running(job_id):
            await query.message.answer(
                f"Парсинг #{job_id} ещё идёт; ниже то, что собрано на сейчас."
            )
        await send_parse_result(query.bot, query.message.chat.id, job_id)


@router.callback_query(F.data.startswith("pcamp:"))
async def on_make_campaign(query: CallbackQuery, state: FSMContext) -> None:
    try:
        job_id = int((query.data or "").removeprefix("pcamp:"))
    except ValueError:
        await query.answer()
        return
    await state.clear()
    await state.set_state(NewCampaign.waiting_type)
    await state.update_data(prefill_parse_job=job_id)
    if query.message is not None:
        await query.message.answer(
            f"Кампания по списку парсинга #{job_id}. Выберите тип:",
            reply_markup=campaign_type_kb(),
        )
    await query.answer()
    logger.info("parse[{}] → new campaign wizard", job_id)
