"""Шаблоны для групп. ARCHITECTURE.md §22.2, §22.9."""

from __future__ import annotations

from html import escape

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app.bot import formatting as fmt
from app.bot.group_keyboards import group_templates_kb, groups_menu_kb, template_delete_kb
from app.bot.keyboards import cancel_kb, confirm_kb
from app.bot.states import NewGroupTemplate
from app.campaigns.template_engine import preview
from app.db.repositories import group_campaigns as group_repo
from app.db.repositories import templates as templates_repo
from app.db.session import session_scope
from app.groups.templates import check_group_template, unique_name

router = Router(name="group_templates")

BODY_HELP = (
    "Пришлите <b>текст сообщения</b> для групп.\n\n"
    "Рандомизация, как в личке: <code>{Привет|Всем привет|Здравствуйте}</code> — для "
    "каждого поста выбирается случайный вариант. Чем больше вариантов, тем меньше "
    "одинаковых постов и риска антиспама.\n"
    "Переменных вроде {first_name} в группах нет. Лимит — 4096 символов."
)


async def create_group_template(name: str, body: str) -> tuple[int, str]:
    """Сохранить шаблон для групп с уникальным именем. → (id, итоговое имя)."""
    async with session_scope() as session:
        final = unique_name(name, await templates_repo.all_names(session))
        t = await templates_repo.create_template(
            session, name=final, body=body, variables=[], kind="group"
        )
        return t.id, final


def samples_text(body: str, n: int = 3) -> str:
    return "\n\n".join(f"<b>{i + 1}.</b> {escape(s)}" for i, s in enumerate(preview(body, n=n)))


@router.message(Command("group_templates"))
@router.callback_query(F.data == "gt:list")
async def list_group_templates(event) -> None:
    async with session_scope() as session:
        items = await templates_repo.list_templates(session, kind="group")
    if isinstance(event, CallbackQuery):
        await event.answer()
        target = event.message
    else:
        target = event
    if target is None:
        return
    if not items:
        await target.answer(
            "📝 <b>Шаблоны для групп</b>\n\nПока нет ни одного. Создайте через кнопку ниже "
            "или прямо в мастере новой рассылки — там можно просто прислать текст.",
            reply_markup=group_templates_kb([]),
        )
        return
    lines = [fmt.section_header("📝", "Шаблоны для групп", str(len(items)))]
    for t in items:
        head = escape(t.body.replace("\n", " ")[:80])
        lines.append(f"📄 <b>#{t.id} · {escape(t.name)}</b> · {len(t.body)} симв.\n    <code>{head}…</code>")
    await target.answer("\n\n".join(lines), reply_markup=group_templates_kb(items))


@router.callback_query(F.data.startswith("gt:view:"))
async def view_group_template(query: CallbackQuery) -> None:
    try:
        tid = int((query.data or "").removeprefix("gt:view:"))
    except ValueError:
        await query.answer("Битый id", show_alert=True)
        return
    async with session_scope() as session:
        t = await templates_repo.get_by_id(session, tid)
    if t is None or t.kind != "group":
        await query.answer("Шаблон не найден", show_alert=True)
        return
    await query.answer()
    if query.message is None:
        return
    text = (
        f"📄 <b>#{t.id} · {escape(t.name)}</b> · {len(t.body)} симв.\n\n"
        f"<b>Как введено:</b>\n<pre>{escape(t.body)}</pre>\n\n"
        f"<b>Примеры постов:</b>\n\n{samples_text(t.body, 2)}"
    )
    await query.message.answer(text[:4090], reply_markup=groups_menu_kb())


@router.callback_query(F.data.startswith("gt:del:"))
async def ask_delete(query: CallbackQuery) -> None:
    try:
        tid = int((query.data or "").removeprefix("gt:del:"))
    except ValueError:
        await query.answer("Битый id", show_alert=True)
        return
    async with session_scope() as session:
        t = await templates_repo.get_by_id(session, tid)
        used = await group_repo.template_usage(session, tid) if t else 0
    if t is None or t.kind != "group":
        await query.answer("Шаблон не найден", show_alert=True)
        return
    await query.answer()
    if query.message is None:
        return
    if used:
        await query.message.answer(
            f"Шаблон <b>{escape(t.name)}</b> используется в рассылках: {used}. Сначала "
            "смените им шаблон (кнопка 📝 в карточке рассылки).",
            reply_markup=groups_menu_kb(),
        )
        return
    await query.message.answer(
        f"Удалить шаблон <b>{escape(t.name)}</b>?", reply_markup=template_delete_kb(tid)
    )


@router.callback_query(F.data.startswith("gt:delok:"))
async def do_delete(query: CallbackQuery) -> None:
    try:
        tid = int((query.data or "").removeprefix("gt:delok:"))
    except ValueError:
        await query.answer("Битый id", show_alert=True)
        return
    async with session_scope() as session:
        t = await templates_repo.get_by_id(session, tid)
        if t is None or t.kind != "group":
            await query.answer("Шаблон не найден", show_alert=True)
            return
        if await group_repo.template_usage(session, tid):
            await query.answer("Шаблон занят рассылкой", show_alert=True)
            return
        name = t.name
        await templates_repo.delete_template(session, tid)
    await query.answer("Удалено")
    if query.message is not None:
        await query.message.answer(f"Шаблон <b>{escape(name)}</b> удалён.")
        await list_group_templates(query.message)


@router.message(Command("new_group_template"))
@router.callback_query(F.data == "gt:new")
async def new_group_template(event, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(NewGroupTemplate.waiting_name)
    if isinstance(event, CallbackQuery):
        await event.answer()
        target = event.message
    else:
        target = event
    if target is not None:
        await target.answer(
            "Как назвать шаблон? Например: <code>Пиар IT-услуг</code>.",
            reply_markup=cancel_kb(),
        )


@router.message(NewGroupTemplate.waiting_name, F.text, ~F.text.startswith("/"))
async def on_name(message: Message, state: FSMContext) -> None:
    name = (message.text or "").strip()
    if not name or len(name) > 128:
        await message.answer("Имя 1–128 символов. Повторите.", reply_markup=cancel_kb())
        return
    async with session_scope() as session:
        if await templates_repo.get_by_name(session, name) is not None:
            await message.answer(
                "Такое имя уже занято. Пришлите другое.", reply_markup=cancel_kb()
            )
            return
    await state.update_data(name=name)
    await state.set_state(NewGroupTemplate.waiting_body)
    await message.answer(BODY_HELP, reply_markup=cancel_kb())


@router.message(NewGroupTemplate.waiting_body, F.text, ~F.text.startswith("/"))
async def on_body(message: Message, state: FSMContext) -> None:
    body = message.text or ""
    error = check_group_template(body)
    if error:
        await message.answer(
            f"Шаблон не прошёл проверку: {escape(error)}\nИсправьте и пришлите снова.",
            reply_markup=cancel_kb(),
        )
        return
    await state.update_data(body=body)
    await state.set_state(NewGroupTemplate.waiting_confirm)
    await message.answer(
        f"<b>Три примера поста:</b>\n\n{samples_text(body)}\n\nСохранить шаблон?",
        reply_markup=confirm_kb("gt:save"),
    )


@router.callback_query(NewGroupTemplate.waiting_confirm, F.data == "gt:save")
async def on_save(query: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    name, body = data.get("name"), data.get("body")
    if not name or not body:
        await query.answer("Сессия пустая, начните заново", show_alert=True)
        await state.clear()
        return
    tid, final = await create_group_template(name, body)
    await state.clear()
    await query.answer("Сохранено")
    if query.message is not None:
        await query.message.answer(
            f"Шаблон для групп <b>{escape(final)}</b> сохранён (#{tid}).",
            reply_markup=groups_menu_kb(),
        )
