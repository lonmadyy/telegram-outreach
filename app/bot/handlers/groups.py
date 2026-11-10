"""Рассылка по группам: меню, рассылки, мастер, правка на ходу. ARCHITECTURE.md §22.9."""

from __future__ import annotations

from datetime import datetime, timezone
from html import escape

from aiogram import F, Router
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import BufferedInputFile, CallbackQuery, Message
from loguru import logger

from app.bot import group_keyboards as gk
from app.bot.handlers.group_templates import create_group_template, samples_text
from app.bot.keyboards import cancel_kb
from app.bot.states import EditGroupCampaign, NewGroupCampaign
from app.db.repositories import accounts as accounts_repo
from app.db.repositories import group_campaigns as repo
from app.db.repositories import templates as templates_repo
from app.db.repositories.settings import settings_cache
from app.db.session import session_scope
from app.groups import service
from app.groups.formatting import (
    DISTRIBUTION,
    campaign_brief,
    campaign_card,
    estimate_block,
    intervals_block,
    target_line,
)
from app.groups.lines import (
    MAX_GROUPS_PER_CAMPAIGN,
    PERIOD_MAX_MINUTES,
    PERIOD_MIN_MINUTES,
    GroupEntry,
    parse_group_list,
)
from app.groups.planner import estimate_rounds, parse_minutes_range, parse_window
from app.groups.reporting import build_targets_csv, report_filename
from app.groups.templates import check_group_template
from app.utils.group_targets import GroupToken

router = Router(name="groups")

TARGETS_PAGE = 20
GAP_MIN_MINUTES, GAP_MAX_MINUTES = 1, 720

GROUPS_HELP = (
    "Пришлите <b>список групп</b> — текстом или файлом .txt, по одной в строке:\n\n"
    "<code>@chat_name</code> или <code>t.me/chat_name</code>\n"
    "<code>t.me/+AbCdEf</code> — закрытая группа по приглашению\n"
    "<code>@chat_name 24h</code> — свой период повтора для этой группы "
    "(<code>90m</code>, <code>2-4h</code>)\n"
    "<code>@chat_name #3</code> — закрепить за аккаунтом #3 (ручное распределение)\n\n"
    f"До {MAX_GROUPS_PER_CAMPAIGN} групп. Где аккаунт уже состоит — начнёт постить сразу, "
    "куда не вступил — вступит сам (до 10 в сутки, с паузами)."
)


# ---------------------------------------------------------------------------
# Общие помощники
# ---------------------------------------------------------------------------


def _tz() -> str:
    return settings_cache.get_str("quiet_hours_timezone", "Europe/Minsk")


def _defaults() -> dict:
    return dict(
        period_min=settings_cache.get_int("group_period_min_default", 30),
        period_max=settings_cache.get_int("group_period_max_default", 200),
        gap_min_sec=settings_cache.get_int("group_gap_min_default_sec", 120),
        gap_max_sec=settings_cache.get_int("group_gap_max_default_sec", 600),
        window_start=None,
        window_end=None,
    )


def _entries_to_data(entries: list[GroupEntry]) -> list[list]:
    return [
        [
            e.token.kind,
            e.token.value,
            e.period[0] if e.period else None,
            e.period[1] if e.period else None,
            e.account_id,
        ]
        for e in entries
    ]


def _entries_from_data(raw: list[list]) -> list[GroupEntry]:
    out = []
    for kind, value, lo, hi, acc in raw:
        period = (lo, hi) if lo is not None and hi is not None else None
        out.append(GroupEntry(GroupToken(kind, value), period, acc))
    return out


async def _answer_target(event) -> Message | None:
    if isinstance(event, CallbackQuery):
        await event.answer()
        return event.message
    return event


def _cid(query: CallbackQuery, prefix: str) -> int | None:
    try:
        return int((query.data or "").removeprefix(prefix).split(":", 1)[0])
    except ValueError:
        return None


async def _read_list(message: Message) -> str | None:
    """Текст сообщения или содержимое TXT (UTF-8/CP1251). None — уже ответили ошибкой."""
    if message.document is None:
        return message.text or ""
    if message.bot is None:
        return None
    doc = message.document
    if doc.file_size and doc.file_size > 1_000_000:
        await message.answer("Файл больше 1 МБ — это точно список групп?", reply_markup=cancel_kb())
        return None
    file = await message.bot.get_file(doc.file_id)
    if file.file_path is None:
        await message.answer("Не удалось получить файл, пришлите ещё раз.", reply_markup=cancel_kb())
        return None
    buf = await message.bot.download_file(file.file_path)
    raw = buf.read() if buf is not None else b""
    for enc in ("utf-8-sig", "cp1251"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    await message.answer(
        "Не удалось прочитать файл (нужен UTF-8 или CP1251).", reply_markup=cancel_kb()
    )
    return None


def _parse_report(parsed) -> str:
    with_period = sum(1 for e in parsed.entries if e.period)
    tagged = sum(1 for e in parsed.entries if e.account_id is not None)
    lines = [f"Принято групп: <b>{len(parsed.entries)}</b>"]
    if with_period:
        lines.append(f"• со своим периодом: {with_period}")
    if tagged:
        lines.append(f"• с меткой аккаунта: {tagged}")
    if parsed.duplicates:
        lines.append(f"• повторов отброшено: {parsed.duplicates}")
    if parsed.invalid:
        sample = "\n".join(escape(s) for s in parsed.invalid[:5])
        lines.append(f"• не распознано строк: {len(parsed.invalid)}\n<code>{sample}</code>")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Меню раздела и список рассылок
# ---------------------------------------------------------------------------


@router.message(Command("groups"))
@router.callback_query(F.data == "menu:groups")
async def groups_menu(event, state: FSMContext) -> None:
    await state.clear()
    async with session_scope() as session:
        running = await repo.list_campaigns(session, statuses=(repo.RUNNING,), limit=200)
        paused = await repo.list_campaigns(session, statuses=(repo.PAUSED,), limit=200)
        capable = await repo.list_group_capable_accounts(session)
    target = await _answer_target(event)
    if target is None:
        return
    await target.answer(
        "📢 <b>Рассылка по группам</b>\n\n"
        "Автопостинг: текст со спинтаксом публикуется в группах по расписанию.\n\n"
        f"Рассылок идёт: <b>{len(running)}</b> · на паузе: {len(paused)}\n"
        f"Аккаунтов с ролью «Группы»: <b>{len(capable)}</b>",
        reply_markup=gk.groups_menu_kb(),
    )


@router.message(Command("group_campaigns"))
@router.callback_query(F.data == "g:list")
async def list_group_campaigns(event) -> None:
    async with session_scope() as session:
        items = await repo.list_campaigns(session, limit=20)
        stats = [await repo.campaign_stats(session, c.id) for c in items]
    target = await _answer_target(event)
    if target is None:
        return
    if not items:
        await target.answer(
            "Групповых рассылок пока нет.", reply_markup=gk.campaigns_list_kb([])
        )
        return
    body = "\n\n".join(campaign_brief(c, s) for c, s in zip(items, stats))
    await target.answer(
        f"📢 <b>Групповые рассылки</b> — {len(items)}\n\n{body}",
        reply_markup=gk.campaigns_list_kb(items),
    )


async def show_card(target: Message, campaign_id: int) -> None:
    async with session_scope() as session:
        c = await repo.get_campaign(session, campaign_id)
        if c is None:
            await target.answer("Рассылка не найдена.", reply_markup=gk.groups_menu_kb())
            return
        stats = await repo.campaign_stats(session, campaign_id)
        tpl = await templates_repo.get_by_id(session, c.template_id)
    text = campaign_card(c, stats, template_name=tpl.name if tpl else "—", tz_name=_tz())
    await target.answer(text, reply_markup=gk.campaign_kb(c))


@router.callback_query(F.data.startswith("g:open:"))
async def open_campaign(query: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    cid = _cid(query, "g:open:")
    await query.answer()
    if cid is not None and query.message is not None:
        await show_card(query.message, cid)


@router.callback_query(F.data.startswith("g:pause:") | F.data.startswith("g:resume:"))
async def pause_resume(query: CallbackQuery) -> None:
    pause = (query.data or "").startswith("g:pause:")
    cid = _cid(query, "g:pause:" if pause else "g:resume:")
    if cid is None:
        await query.answer("Битый id", show_alert=True)
        return
    async with session_scope() as session:
        await repo.set_status(session, cid, repo.PAUSED if pause else repo.RUNNING)
    await query.answer("На паузе" if pause else "Продолжаем")
    if query.message is not None:
        await show_card(query.message, cid)


@router.callback_query(F.data.startswith("g:stop:"))
async def ask_stop(query: CallbackQuery) -> None:
    cid = _cid(query, "g:stop:")
    await query.answer()
    if cid is not None and query.message is not None:
        await query.message.answer(
            f"Остановить рассылку #{cid}? Посты прекратятся. Группы и статистика "
            "сохранятся — позже её можно запустить снова.",
            reply_markup=gk.stop_confirm_kb(cid),
        )


@router.callback_query(F.data.startswith("g:stopok:"))
async def do_stop(query: CallbackQuery) -> None:
    cid = _cid(query, "g:stopok:")
    if cid is None:
        await query.answer("Битый id", show_alert=True)
        return
    async with session_scope() as session:
        await repo.set_status(session, cid, repo.STOPPED)
    await query.answer("Остановлена")
    if query.message is not None:
        await show_card(query.message, cid)


@router.callback_query(F.data.startswith("g:retry:"))
async def retry_problems(query: CallbackQuery) -> None:
    cid = _cid(query, "g:retry:")
    if cid is None:
        await query.answer("Битый id", show_alert=True)
        return
    async with session_scope() as session:
        n = await repo.retry_problem_targets(session, cid)
    if n:
        await query.answer(f"Вернул в работу групп: {n}. Бот перепроверит их заново.", show_alert=True)
    else:
        await query.answer("Проблемных групп нет")


@router.callback_query(F.data.startswith("g:tg:"))
async def targets_page(query: CallbackQuery) -> None:
    try:
        _, _, raw_cid, raw_page = (query.data or "").split(":")
        cid, page = int(raw_cid), int(raw_page)
    except ValueError:
        await query.answer("Битые данные", show_alert=True)
        return
    async with session_scope() as session:
        total = await repo.count_targets(session, cid)
        items = await repo.list_targets(session, cid, offset=page * TARGETS_PAGE, limit=TARGETS_PAGE)
    await query.answer()
    if query.message is None:
        return
    pages = max(1, (total + TARGETS_PAGE - 1) // TARGETS_PAGE)
    now = datetime.now(timezone.utc)
    body = "\n".join(target_line(t, tz_name=_tz(), now=now) for t in items) or "Групп нет."
    text = f"📋 <b>Группы рассылки #{cid}</b> — {total} (сначала проблемные)\n\n{body}"
    kb = gk.targets_nav_kb(cid, page, pages)
    try:
        await query.message.edit_text(text[:4090], reply_markup=kb)
    except Exception:
        await query.message.answer(text[:4090], reply_markup=kb)


@router.callback_query(F.data.startswith("g:csv:"))
async def export_csv(query: CallbackQuery) -> None:
    cid = _cid(query, "g:csv:")
    if cid is None:
        await query.answer("Битый id", show_alert=True)
        return
    async with session_scope() as session:
        targets = await repo.list_targets(session, cid)
    await query.answer()
    if query.message is None:
        return
    now = datetime.now(timezone.utc)
    doc = BufferedInputFile(build_targets_csv(targets), filename=report_filename(cid, now))
    await query.message.answer_document(doc, caption=f"Группы рассылки #{cid}: {len(targets)}")


# ---------------------------------------------------------------------------
# Мастер новой рассылки
# ---------------------------------------------------------------------------


@router.message(Command("new_group_campaign"))
@router.callback_query(F.data == "g:new")
async def new_campaign(event, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(NewGroupCampaign.waiting_name)
    target = await _answer_target(event)
    if target is not None:
        await target.answer(
            "➕ <b>Новая рассылка по группам</b>\n\n"
            "Как её назвать? Например: <code>Пиар IT-услуг</code>. Название видите только вы.",
            reply_markup=cancel_kb(),
        )


@router.message(NewGroupCampaign.waiting_name, F.text, ~F.text.startswith("/"))
async def wiz_name(message: Message, state: FSMContext) -> None:
    name = (message.text or "").strip()
    if not name or len(name) > 128:
        await message.answer("Название 1–128 символов. Повторите.", reply_markup=cancel_kb())
        return
    await state.update_data(name=name, **_defaults())
    await state.set_state(NewGroupCampaign.waiting_template)
    async with session_scope() as session:
        templates = await templates_repo.list_templates(session, kind="group")
    hint = (
        "Выберите шаблон или просто пришлите текст поста — сохраню его как шаблон "
        f"«{escape(name)}».\n\nРандомизация: <code>{{Привет|Здравствуйте}}</code>. "
        "Переменных в группах нет."
    )
    await message.answer(
        hint if templates else hint.replace("Выберите шаблон или просто пришлите", "Пришлите"),
        reply_markup=gk.template_picker_kb(templates, "gw:tpl:") if templates else cancel_kb(),
    )


@router.callback_query(NewGroupCampaign.waiting_template, F.data.startswith("gw:tpl:"))
async def wiz_pick_template(query: CallbackQuery, state: FSMContext) -> None:
    try:
        tid = int((query.data or "").removeprefix("gw:tpl:"))
    except ValueError:
        await query.answer("Битый id", show_alert=True)
        return
    async with session_scope() as session:
        t = await templates_repo.get_by_id(session, tid)
    if t is None or t.kind != "group":
        await query.answer("Шаблон не найден", show_alert=True)
        return
    await state.update_data(template_id=t.id, template_name=t.name)
    await query.answer()
    await state.set_state(NewGroupCampaign.waiting_groups)
    if query.message is not None:
        await query.message.answer(GROUPS_HELP, reply_markup=cancel_kb())


@router.message(NewGroupCampaign.waiting_template, F.text, ~F.text.startswith("/"))
async def wiz_template_text(message: Message, state: FSMContext) -> None:
    body = message.text or ""
    error = check_group_template(body)
    if error:
        await message.answer(
            f"Текст не прошёл проверку: {escape(error)}\nИсправьте и пришлите снова.",
            reply_markup=cancel_kb(),
        )
        return
    data = await state.get_data()
    tid, final = await create_group_template(data["name"], body)
    await state.update_data(template_id=tid, template_name=final)
    await state.set_state(NewGroupCampaign.waiting_groups)
    await message.answer(
        f"Сохранил шаблон <b>{escape(final)}</b>. Так будут выглядеть посты:\n\n"
        f"{samples_text(body, 2)}"
    )
    await message.answer(GROUPS_HELP, reply_markup=cancel_kb())


@router.message(NewGroupCampaign.waiting_groups, F.document | (F.text & ~F.text.startswith("/")))
async def wiz_groups(message: Message, state: FSMContext) -> None:
    text = await _read_list(message)
    if text is None:
        return
    parsed = parse_group_list(text)
    if parsed.too_many:
        await message.answer(
            f"Групп больше {MAX_GROUPS_PER_CAMPAIGN}. Разбейте на несколько рассылок.",
            reply_markup=cancel_kb(),
        )
        return
    if not parsed.entries:
        await message.answer(
            "Не нашёл ни одной группы.\n\n" + _parse_report(parsed), reply_markup=cancel_kb()
        )
        return
    async with session_scope() as session:
        accounts = await repo.list_group_capable_accounts(session)
    tagged = {e.account_id for e in parsed.entries if e.account_id is not None}
    capable_ids = [a.id for a in accounts]
    selected = [a for a in capable_ids if a in tagged] or capable_ids
    await state.update_data(entries=_entries_to_data(parsed.entries), selected=selected)
    await state.set_state(NewGroupCampaign.waiting_accounts)
    await message.answer(_parse_report(parsed))
    await _show_account_picker(message, state)


async def _show_account_picker(target: Message, state: FSMContext) -> None:
    async with session_scope() as session:
        accounts = await repo.list_group_capable_accounts(session)
    if not accounts:
        await target.answer(
            "Нет аккаунтов с ролью «Группы». Включите роль в «Аккаунты и роли» и нажмите "
            "«Проверить снова».",
            reply_markup=gk.no_accounts_kb(),
        )
        return
    data = await state.get_data()
    capable = {a.id for a in accounts}
    selected = {a for a in data.get("selected", []) if a in capable}
    await state.update_data(selected=sorted(selected))
    await target.answer(
        "👤 Какие аккаунты будут постить? Показаны живые аккаунты с ролью «Группы».",
        reply_markup=gk.accounts_select_kb(accounts, selected),
    )


@router.callback_query(NewGroupCampaign.waiting_accounts, F.data.startswith("gw:acc"))
async def wiz_accounts(query: CallbackQuery, state: FSMContext) -> None:
    action = query.data or ""
    data = await state.get_data()
    selected = set(data.get("selected", []))
    async with session_scope() as session:
        accounts = await repo.list_group_capable_accounts(session)
    capable = [a.id for a in accounts]

    if action == "gw:accrefresh":
        await query.answer()
        if query.message is not None:
            await _show_account_picker(query.message, state)
        return
    if action == "gw:accall":
        selected = set(capable)
    elif action == "gw:accok":
        chosen = [a for a in capable if a in selected]
        if not chosen:
            await query.answer("Выберите хотя бы один аккаунт", show_alert=True)
            return
        await state.update_data(selected=chosen)
        await query.answer()
        if query.message is None:
            return
        if len(chosen) == 1:
            await state.update_data(distribution="even")
            await _show_intervals(query.message, state, mode="gw")
            return
        entries = _entries_from_data(data.get("entries", []))
        tagged = sum(1 for e in entries if e.account_id is not None)
        await state.set_state(NewGroupCampaign.waiting_distribution)
        await query.message.answer(
            "⚖ <b>Как распределить группы между аккаунтами?</b>\n\n"
            "• <b>Поровну</b> — каждую группу ведёт один аккаунт, бот делит список сам.\n"
            "• <b>Каждый во все</b> — каждый аккаунт постит во все группы (больше показов, "
            "но в группе видно одно и то же от разных людей).\n"
            "• <b>Вручную</b> — строки с меткой <code>#id</code> достаются этому аккаунту, "
            "остальные делятся поровну.",
            reply_markup=gk.distribution_kb(tagged),
        )
        return
    else:
        try:
            aid = int(action.removeprefix("gw:acc:"))
        except ValueError:
            await query.answer()
            return
        selected ^= {aid}
    await state.update_data(selected=sorted(selected))
    await query.answer()
    if query.message is not None:
        try:
            await query.message.edit_reply_markup(
                reply_markup=gk.accounts_select_kb(accounts, selected)
            )
        except Exception:
            pass


@router.callback_query(NewGroupCampaign.waiting_distribution, F.data.startswith("gw:dist:"))
async def wiz_distribution(query: CallbackQuery, state: FSMContext) -> None:
    mode = (query.data or "").removeprefix("gw:dist:")
    if mode not in DISTRIBUTION:
        await query.answer("Неизвестный режим", show_alert=True)
        return
    await state.update_data(distribution=mode)
    await query.answer()
    if query.message is not None:
        await _show_intervals(query.message, state, mode="gw")


# --- Интервалы: общий экран для мастера (gw) и правки (ge) ---


async def _show_intervals(target: Message, state: FSMContext, *, mode: str) -> None:
    data = await state.get_data()
    if mode == "gw":
        await state.set_state(NewGroupCampaign.waiting_intervals)
        title, done = "⏱ <b>Интервалы</b> (можно оставить как есть)", "✅ Всё верно, дальше"
    else:
        await state.set_state(EditGroupCampaign.waiting_intervals)
        title, done = f"⏱ <b>Интервалы рассылки #{data.get('edit_cid')}</b>", "💾 Сохранить"
    text = (
        f"{title}\n\n"
        + intervals_block(
            data["period_min"], data["period_max"], data["gap_min_sec"], data["gap_max_sec"],
            data.get("window_start"), data.get("window_end"),
        )
        + "\n\nПериод — как часто аккаунт снова пишет в ту же группу. Пауза — сколько "
        "аккаунт ждёт между постами в разные группы. Тихие часы (01:00–07:00) действуют всегда."
    )
    await target.answer(text, reply_markup=gk.intervals_kb(mode, done_text=done))


_IV_PROMPTS = {
    "period": (
        "Пришлите период повтора в минутах: <code>от-до</code>, например <code>30-200</code>. "
        f"Одно число — фиксированный. От {PERIOD_MIN_MINUTES} мин до {PERIOD_MAX_MINUTES // 60} ч."
    ),
    "gap": (
        "Пришлите паузу между постами аккаунта в минутах: <code>от-до</code>, например "
        f"<code>2-10</code>. От {GAP_MIN_MINUTES} до {GAP_MAX_MINUTES} мин."
    ),
    "window": (
        "Пришлите окно работы, например <code>09:00-22:00</code> (время Минска), или "
        "<code>нет</code> — работать весь день, кроме тихих часов."
    ),
}


@router.callback_query(
    StateFilter(NewGroupCampaign.waiting_intervals, EditGroupCampaign.waiting_intervals),
    F.data.regexp(r"^g[we]:iv:(period|gap|window)$"),
)
async def iv_field(query: CallbackQuery, state: FSMContext) -> None:
    mode, _, field = (query.data or "").split(":")
    await state.update_data(iv_field=field)
    await state.set_state(
        NewGroupCampaign.waiting_interval_value
        if mode == "gw"
        else EditGroupCampaign.waiting_interval_value
    )
    await query.answer()
    if query.message is not None:
        await query.message.answer(_IV_PROMPTS[field], reply_markup=cancel_kb())


@router.message(
    StateFilter(NewGroupCampaign.waiting_interval_value, EditGroupCampaign.waiting_interval_value),
    F.text,
    ~F.text.startswith("/"),
)
async def iv_value(message: Message, state: FSMContext) -> None:
    mode = "gw" if await state.get_state() == NewGroupCampaign.waiting_interval_value.state else "ge"
    data = await state.get_data()
    field = data.get("iv_field")
    raw = message.text or ""
    if field == "period":
        rng = parse_minutes_range(raw, lo_limit=PERIOD_MIN_MINUTES, hi_limit=PERIOD_MAX_MINUTES)
        if rng is None:
            await message.answer("Не понял. " + _IV_PROMPTS["period"], reply_markup=cancel_kb())
            return
        await state.update_data(period_min=rng[0], period_max=rng[1])
    elif field == "gap":
        rng = parse_minutes_range(raw, lo_limit=GAP_MIN_MINUTES, hi_limit=GAP_MAX_MINUTES)
        if rng is None:
            await message.answer("Не понял. " + _IV_PROMPTS["gap"], reply_markup=cancel_kb())
            return
        await state.update_data(gap_min_sec=rng[0] * 60, gap_max_sec=rng[1] * 60)
    elif field == "window":
        win = parse_window(raw)
        if win is False:
            await message.answer("Не понял. " + _IV_PROMPTS["window"], reply_markup=cancel_kb())
            return
        start, end = win if win else (None, None)
        await state.update_data(window_start=start, window_end=end)
    await _show_intervals(message, state, mode=mode)


@router.callback_query(NewGroupCampaign.waiting_intervals, F.data == "gw:iv:ok")
async def wiz_confirm(query: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    entries = _entries_from_data(data.get("entries", []))
    accounts = data.get("selected", [])
    plan = service.plan_targets(entries, accounts, data.get("distribution", "even"))
    if not plan.rows:
        await query.answer("Ни одной группы не назначено — проверьте метки #id", show_alert=True)
        return
    conflicts = await service.count_conflicts(plan)
    estimates = estimate_rounds(
        plan.per_account,
        gap_min_sec=data["gap_min_sec"],
        gap_max_sec=data["gap_max_sec"],
        period_min=data["period_min"],
    )
    warn = []
    if plan.problems:
        warn.append("⚠ Не назначены: " + "; ".join(escape(p) for p in plan.problems[:5]))
    if conflicts:
        warn.append(
            f"⚠ {conflicts} пар «аккаунт × группа» уже есть в других рассылках — аккаунт "
            "будет постить туда разные тексты. Лучше убрать повторы."
        )
    text = (
        "✅ <b>Проверьте рассылку</b>\n\n"
        f"• Название: <b>{escape(data['name'])}</b>\n"
        f"• Шаблон: <b>{escape(data.get('template_name', '—'))}</b>\n"
        f"• Групп: <b>{len(entries)}</b> · аккаунтов: <b>{len(accounts)}</b> · "
        f"{DISTRIBUTION[data.get('distribution', 'even')]}\n"
        + intervals_block(
            data["period_min"], data["period_max"], data["gap_min_sec"], data["gap_max_sec"],
            data.get("window_start"), data.get("window_end"),
        )
        + "\n\n<b>Круг по группам:</b>\n"
        + estimate_block(estimates, data["period_min"])
        + ("\n\n" + "\n".join(warn) if warn else "")
        + "\n\nПосле запуска аккаунты начнут постить там, где уже состоят, и постепенно "
        "вступят в остальные группы."
    )
    await state.set_state(NewGroupCampaign.waiting_confirm)
    await query.answer()
    if query.message is not None:
        await query.message.answer(text[:4090], reply_markup=gk.start_kb())


@router.callback_query(NewGroupCampaign.waiting_confirm, F.data == "gw:start")
async def wiz_start(query: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    if not data.get("entries") or not data.get("template_id") or not data.get("selected"):
        await query.answer("Сессия пустая, начните заново", show_alert=True)
        await state.clear()
        return
    draft = service.Draft(
        name=data["name"],
        template_id=int(data["template_id"]),
        entries=_entries_from_data(data["entries"]),
        account_ids=list(data["selected"]),
        distribution=data.get("distribution", "even"),
        period_min=data["period_min"],
        period_max=data["period_max"],
        gap_min_sec=data["gap_min_sec"],
        gap_max_sec=data["gap_max_sec"],
        window_start=data.get("window_start"),
        window_end=data.get("window_end"),
    )
    try:
        cid, added = await service.create_campaign(
            draft, user_id=query.from_user.id if query.from_user else None
        )
    except Exception:
        logger.exception("group campaign create failed")
        await query.answer("Не удалось создать рассылку, см. логи", show_alert=True)
        return
    await state.clear()
    await query.answer("Запущено")
    if query.message is not None:
        await query.message.answer(f"▶️ Рассылка #{cid} запущена, групп в работе: {added}.")
        await show_card(query.message, cid)


# ---------------------------------------------------------------------------
# Правка работающей рассылки
# ---------------------------------------------------------------------------


@router.callback_query(F.data.startswith("g:iv:"))
async def edit_intervals(query: CallbackQuery, state: FSMContext) -> None:
    cid = _cid(query, "g:iv:")
    async with session_scope() as session:
        c = await repo.get_campaign(session, cid) if cid is not None else None
    if c is None:
        await query.answer("Рассылка не найдена", show_alert=True)
        return
    await state.clear()
    await state.update_data(
        edit_cid=c.id,
        period_min=c.period_min,
        period_max=c.period_max,
        gap_min_sec=c.gap_min_sec,
        gap_max_sec=c.gap_max_sec,
        window_start=c.window_start,
        window_end=c.window_end,
    )
    await query.answer()
    if query.message is not None:
        await _show_intervals(query.message, state, mode="ge")


@router.callback_query(EditGroupCampaign.waiting_intervals, F.data == "ge:iv:ok")
async def save_intervals(query: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    cid = data.get("edit_cid")
    if cid is None:
        await query.answer("Сессия пустая", show_alert=True)
        await state.clear()
        return
    async with session_scope() as session:
        await repo.update_intervals(
            session,
            int(cid),
            period_min=data["period_min"],
            period_max=data["period_max"],
            gap_min_sec=data["gap_min_sec"],
            gap_max_sec=data["gap_max_sec"],
            window_start=data.get("window_start"),
            window_end=data.get("window_end"),
        )
    await state.clear()
    await query.answer("Сохранено")
    if query.message is not None:
        await query.message.answer(
            "Интервалы сохранены. Новый период применится к каждой группе после её "
            "следующего поста."
        )
        await show_card(query.message, int(cid))


@router.callback_query(F.data.startswith("g:tpl:"))
async def edit_template(query: CallbackQuery, state: FSMContext) -> None:
    cid = _cid(query, "g:tpl:")
    if cid is None:
        await query.answer("Битый id", show_alert=True)
        return
    async with session_scope() as session:
        templates = await templates_repo.list_templates(session, kind="group")
    await state.clear()
    await state.set_state(EditGroupCampaign.waiting_template_text)
    await state.update_data(edit_cid=cid)
    await query.answer()
    if query.message is not None:
        await query.message.answer(
            "📝 Выберите новый шаблон или пришлите текст поста — сохраню его как новый шаблон.",
            reply_markup=gk.template_picker_kb(templates, f"g:settpl:{cid}:"),
        )


@router.callback_query(F.data.startswith("g:settpl:"))
async def set_template(query: CallbackQuery, state: FSMContext) -> None:
    try:
        _, _, raw_cid, raw_tid = (query.data or "").split(":")
        cid, tid = int(raw_cid), int(raw_tid)
    except ValueError:
        await query.answer("Битые данные", show_alert=True)
        return
    async with session_scope() as session:
        t = await templates_repo.get_by_id(session, tid)
        if t is None or t.kind != "group":
            await query.answer("Шаблон не найден", show_alert=True)
            return
        await repo.set_template(session, cid, tid)
    await state.clear()
    await query.answer("Шаблон сменён")
    if query.message is not None:
        await show_card(query.message, cid)


@router.message(EditGroupCampaign.waiting_template_text, F.text, ~F.text.startswith("/"))
async def set_template_text(message: Message, state: FSMContext) -> None:
    body = message.text or ""
    error = check_group_template(body)
    if error:
        await message.answer(
            f"Текст не прошёл проверку: {escape(error)}\nИсправьте и пришлите снова.",
            reply_markup=cancel_kb(),
        )
        return
    data = await state.get_data()
    cid = int(data["edit_cid"])
    async with session_scope() as session:
        c = await repo.get_campaign(session, cid)
    tid, final = await create_group_template(c.name if c else "Шаблон", body)
    async with session_scope() as session:
        await repo.set_template(session, cid, tid)
    await state.clear()
    await message.answer(f"Сохранил шаблон <b>{escape(final)}</b> и поставил его в рассылку.")
    await show_card(message, cid)


@router.callback_query(F.data.startswith("g:add:"))
async def add_groups_start(query: CallbackQuery, state: FSMContext) -> None:
    cid = _cid(query, "g:add:")
    if cid is None:
        await query.answer("Битый id", show_alert=True)
        return
    await state.clear()
    await state.set_state(EditGroupCampaign.waiting_groups)
    await state.update_data(edit_cid=cid)
    await query.answer()
    if query.message is not None:
        await query.message.answer(
            f"➕ Добавление групп в рассылку #{cid}.\n\n" + GROUPS_HELP, reply_markup=cancel_kb()
        )


@router.message(EditGroupCampaign.waiting_groups, F.document | (F.text & ~F.text.startswith("/")))
async def add_groups_receive(message: Message, state: FSMContext) -> None:
    text = await _read_list(message)
    if text is None:
        return
    parsed = parse_group_list(text)
    if not parsed.entries:
        await message.answer(
            "Не нашёл ни одной группы.\n\n" + _parse_report(parsed), reply_markup=cancel_kb()
        )
        return
    data = await state.get_data()
    cid = int(data["edit_cid"])
    added, already, problems = await service.add_groups(cid, parsed.entries)
    await state.clear()
    lines = [_parse_report(parsed), "", f"Добавлено в рассылку: <b>{added}</b>"]
    if already:
        lines.append(f"Уже были в рассылке: {already}")
    if problems:
        lines.append("⚠ " + "; ".join(escape(p) for p in problems[:5]))
    await message.answer("\n".join(lines), reply_markup=gk.back_to_campaign_kb(cid))


async def _accounts_screen(cid: int):
    async with session_scope() as session:
        capable = await repo.list_group_capable_accounts(session)
        members = set(await repo.campaign_account_ids(session, cid))
        shown = {a.id: a for a in capable}
        for aid in members - set(shown):
            acc = await accounts_repo.get_by_id(session, aid)
            if acc is not None:
                shown[aid] = acc
    accounts = [shown[k] for k in sorted(shown)]
    return accounts, members


@router.callback_query(F.data.startswith("g:acc:"))
async def campaign_accounts(query: CallbackQuery) -> None:
    cid = _cid(query, "g:acc:")
    if cid is None:
        await query.answer("Битый id", show_alert=True)
        return
    accounts, members = await _accounts_screen(cid)
    await query.answer()
    if query.message is not None:
        await query.message.answer(
            f"👤 <b>Аккаунты рассылки #{cid}</b>\n\n✅ — участвует. Нажмите, чтобы добавить "
            "или убрать. Группы убранного аккаунта перейдут остальным.",
            reply_markup=gk.campaign_accounts_kb(cid, accounts, members),
        )


@router.callback_query(F.data.startswith("g:acct:"))
async def toggle_campaign_account(query: CallbackQuery) -> None:
    try:
        _, _, raw_cid, raw_aid = (query.data or "").split(":")
        cid, aid = int(raw_cid), int(raw_aid)
    except ValueError:
        await query.answer("Битые данные", show_alert=True)
        return
    async with session_scope() as session:
        members = set(await repo.campaign_account_ids(session, cid))
    if aid in members:
        if len(members) <= 1:
            await query.answer("В рассылке должен остаться хотя бы один аккаунт", show_alert=True)
            return
        result = await service.remove_account(cid, aid)
    else:
        result = await service.add_account(cid, aid)
    await query.answer(result[:190], show_alert=True)
    accounts, members = await _accounts_screen(cid)
    if query.message is not None:
        try:
            await query.message.edit_reply_markup(
                reply_markup=gk.campaign_accounts_kb(cid, accounts, members)
            )
        except Exception:
            pass
