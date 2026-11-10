"""Роли аккаунтов: личка / группы. ARCHITECTURE.md §22.1, §22.9."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from app.bot import formatting as fmt
from app.bot.group_keyboards import roles_kb
from app.db.models import AccountStatus
from app.db.repositories import accounts as accounts_repo
from app.db.repositories import group_campaigns as group_repo
from app.db.session import session_scope

router = Router(name="roles")

INTRO = (
    "👥 <b>Аккаунты и роли</b>\n\n"
    "💬 <b>Личка</b> — аккаунт берёт задачи кампаний в личные сообщения и инвайты.\n"
    "📢 <b>Группы</b> — аккаунт можно выбрать в рассылке по группам.\n"
    "Можно включить обе роли: аккаунт будет чередовать задачи. Ограничения (FloodWait, "
    "спамблок, тихие часы) действуют на обе линии сразу. На прогреве аккаунт в группы "
    "не пишет."
)


async def _alive_accounts() -> list:
    async with session_scope() as session:
        items = await accounts_repo.list_accounts(session)
    return [a for a in items if a.status not in (AccountStatus.dead, AccountStatus.disabled)]


def _text(accounts: list) -> str:
    if not accounts:
        return INTRO + "\n\nЖивых аккаунтов нет. Добавьте через /add_account."
    lines = [INTRO, ""]
    for a in accounts:
        emoji, status = fmt.account_status(a)
        roles = []
        if a.role_dm:
            roles.append("💬")
        if a.role_groups:
            roles.append("📢")
        warm = " · прогрев" if accounts_repo.is_in_warmup(a) and a.role_groups else ""
        lines.append(
            f"{emoji} #{a.id} · {a.phone} — {status} · роли: {' '.join(roles) or 'нет'}{warm}"
        )
    return "\n".join(lines)


@router.message(Command("roles"))
@router.callback_query(F.data == "g:roles")
async def show_roles(event) -> None:
    accounts = await _alive_accounts()
    if isinstance(event, CallbackQuery):
        await event.answer()
        target = event.message
    else:
        target = event
    if target is None:
        return
    await target.answer(_text(accounts), reply_markup=roles_kb(accounts))


@router.callback_query(F.data.startswith("role:"))
async def toggle_role(query: CallbackQuery) -> None:
    try:
        _, kind, raw = (query.data or "").split(":", 2)
        account_id = int(raw)
    except ValueError:
        await query.answer("Битые данные", show_alert=True)
        return
    role = "dm" if kind == "dm" else "groups"
    async with session_scope() as session:
        acc = await accounts_repo.get_by_id(session, account_id)
        if acc is None:
            await query.answer("Аккаунт не найден", show_alert=True)
            return
        current = acc.role_dm if role == "dm" else acc.role_groups
        await group_repo.set_role(session, account_id, role=role, value=not current)
    label = "💬 Личка" if role == "dm" else "📢 Группы"
    await query.answer(f"#{account_id}: {label} — {'выключено' if current else 'включено'}")
    accounts = await _alive_accounts()
    if query.message is not None:
        try:
            await query.message.edit_text(_text(accounts), reply_markup=roles_kb(accounts))
        except Exception:
            await query.message.answer(_text(accounts), reply_markup=roles_kb(accounts))
