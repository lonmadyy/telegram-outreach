"""Хендлеры шаблонов. ARCHITECTURE.md §8.4, §10.2."""

from __future__ import annotations

from html import escape as html_escape

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import BufferedInputFile, CallbackQuery, Message
from loguru import logger

from app.bot import formatting as fmt
from app.bot.keyboards import cancel_kb, confirm_kb, main_menu, templates_view_kb
from app.bot.states import NewTemplate
from app.campaigns.template_engine import (
    TemplateError,
    preview,
    validate_template,
)
from app.db.repositories import templates as templates_repo
from app.db.session import session_scope

router = Router(name="templates")


@router.message(Command("templates"))
@router.callback_query(F.data == "menu:templates")
async def list_templates(event) -> None:
    async with session_scope() as session:
        items = await templates_repo.list_templates(session)

    if isinstance(event, CallbackQuery):
        target = event.message
        await event.answer()
    else:
        target = event
    if target is None:
        return

    if not items:
        await target.answer(
            "Шаблонов пока нет.\nСоздайте через /new_template.",
            reply_markup=main_menu(),
        )
        return

    lines = [fmt.section_header("📝", "Шаблоны", str(len(items)))]
    for t in items:
        used_vars = ", ".join(t.variables) if t.variables else "—"
        head = html_escape(t.body.replace("\n", " ")[:80])
        lines.append(
            f"📄 <b>#{t.id} · {t.name}</b>\n"
            f"    Переменные: {used_vars} · {len(t.body)} симв.\n"
            f"    <code>{head}…</code>"
        )
    lines.append("\n<i>Нажмите 👁, чтобы увидеть шаблон целиком.</i>")
    await target.answer("\n\n".join(lines), reply_markup=templates_view_kb(items))


@router.callback_query(F.data.startswith("tpl:view:"))
async def on_template_view(query: CallbackQuery) -> None:
    """Показать шаблон целиком (§8.4): необрезанное тело + один пример отправки.
    Тело длиннее лимита сообщения отдаём файлом."""
    raw = (query.data or "").removeprefix("tpl:view:")
    try:
        tpl_id = int(raw)
    except ValueError:
        await query.answer("Битый id", show_alert=True)
        return
    async with session_scope() as session:
        template = await templates_repo.get_by_id(session, tpl_id)
    if template is None:
        await query.answer("Шаблон не найден", show_alert=True)
        return
    await query.answer()
    if query.message is None:
        return

    used_vars = ", ".join(template.variables) if template.variables else "—"
    header = (
        f"📄 <b>#{template.id} · {template.name}</b>\n"
        f"Переменные: {used_vars} · длина {len(template.body)} симв."
    )
    sample = html_escape(preview(template.body, n=1)[0])
    full = (
        f"{header}\n\n<b>Тело (как введено):</b>\n"
        f"<pre>{html_escape(template.body)}</pre>\n\n"
        f"<b>Пример отправки:</b>\n{sample}"
    )
    if len(full) <= 4000:
        await query.message.answer(full, reply_markup=main_menu())
        return
    # Слишком длинное для одного сообщения — тело отдаём файлом.
    await query.message.answer(
        f"{header}\n\n<b>Пример отправки:</b>\n{sample}", reply_markup=main_menu()
    )
    doc = BufferedInputFile(
        template.body.encode("utf-8"), filename=f"template_{template.id}.txt"
    )
    await query.message.answer_document(doc, caption="Тело шаблона (необрезанное)")


@router.message(Command("new_template"))
async def cmd_new_template(message: Message, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(NewTemplate.waiting_name)
    await message.answer(
        "Введите короткое имя шаблона (например, <code>spring_promo</code>).",
        reply_markup=cancel_kb(),
    )


@router.message(NewTemplate.waiting_name, F.text)
async def on_template_name(message: Message, state: FSMContext) -> None:
    if message.text is None:
        return
    name = message.text.strip()
    if not name or len(name) > 128:
        await message.answer("Имя 1–128 символов. Повторите ввод.", reply_markup=cancel_kb())
        return

    async with session_scope() as session:
        existing = await templates_repo.get_by_name(session, name)
        if existing is not None:
            await message.answer(
                f"Шаблон с именем {name!r} уже существует (id={existing.id}). "
                "Введите другое имя.",
                reply_markup=cancel_kb(),
            )
            return

    await state.update_data(name=name)
    await state.set_state(NewTemplate.waiting_body)
    await message.answer(
        "Теперь пришлите <b>текст сообщения</b>, которое будут получать клиенты "
        "при рассылке.\n\n"
        "<b>Простой пример:</b>\n"
        "<code>Привет, {first_name}! У нас новая акция со скидкой 25%.</code>\n\n"
        "<b>Пример с рандомизацией</b> (рекомендую — снижает риск антиспам-фильтра):\n"
        "<code>{Привет|Здравствуй|Хей}, {first_name}! "
        "{Хотел рассказать|Думаю интересно} про новую акцию.</code>\n\n"
        "<b>Что подставляется:</b>\n"
        "• <code>{first_name}</code> — имя получателя из его профиля Telegram\n"
        "• <code>{username}</code> — его @username\n"
        "• <code>{last_name}</code>, <code>{full_name}</code> — фамилия / полное имя\n"
        "• <code>{вариант1|вариант2|вариант3}</code> — случайный выбор для каждого сообщения\n"
        "• <code>\\{ \\}</code> — если нужны буквальные фигурные скобки\n\n"
        "После ввода я покажу 5 пробных рендеров для проверки.\n"
        "Лимит — 4096 символов в каждом рендере.",
        reply_markup=cancel_kb(),
    )


@router.message(NewTemplate.waiting_body, F.text)
async def on_template_body(message: Message, state: FSMContext) -> None:
    if message.text is None:
        return
    body = message.text

    try:
        used_vars = validate_template(body)
    except TemplateError as e:
        await message.answer(
            f"Шаблон не прошёл проверку: {e}\nИсправьте и пришлите снова.",
            reply_markup=cancel_kb(),
        )
        return

    samples = preview(body, n=5)
    samples_text = "\n\n".join(f"<b>{i + 1}.</b> {s}" for i, s in enumerate(samples))

    await state.update_data(body=body, variables=used_vars)
    await state.set_state(NewTemplate.waiting_confirm)
    await message.answer(
        f"<b>Переменные:</b> {', '.join(used_vars) if used_vars else 'нет'}\n\n"
        f"<b>5 пробных рендеров:</b>\n\n{samples_text}\n\n"
        f"Сохранить шаблон?",
        reply_markup=confirm_kb("tpl:save"),
    )


@router.callback_query(NewTemplate.waiting_confirm, F.data == "tpl:save")
async def on_template_save(query: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    name = data.get("name")
    body = data.get("body")
    used_vars = data.get("variables", [])

    if not name or not body:
        await query.answer("Сессия пустая, начните заново", show_alert=True)
        await state.clear()
        return

    async with session_scope() as session:
        template = await templates_repo.create_template(
            session, name=name, body=body, variables=used_vars
        )

    await state.clear()
    if query.message is not None:
        await query.message.answer(
            f"Шаблон <b>{template.name}</b> сохранён (id={template.id}).",
            reply_markup=main_menu(),
        )
    await query.answer("Сохранено")


@router.message(Command("del_template"))
async def cmd_del_template(message: Message) -> None:
    if message.text is None:
        return
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        await message.answer(
            "Использование: <code>/del_template имя</code> или "
            "<code>/del_template id</code>",
            reply_markup=main_menu(),
        )
        return

    key = parts[1].strip()
    async with session_scope() as session:
        template = None
        if key.isdigit():
            template = await templates_repo.get_by_id(session, int(key))
        if template is None:
            template = await templates_repo.get_by_name(session, key)
        if template is None:
            await message.answer(f"Шаблон {key!r} не найден.", reply_markup=main_menu())
            return
        await templates_repo.delete_template(session, template.id)
        logger.info("Template deleted: id={}, name={}", template.id, template.name)

    await message.answer(
        f"Шаблон <b>{template.name}</b> удалён.", reply_markup=main_menu()
    )
