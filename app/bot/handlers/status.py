"""Расширенный /status и /spamcheck. ARCHITECTURE.md §10.2.

В /accounts уже есть базовый список. Здесь — сводка по системе целиком:
кампании, аккаунты с детальными статусами, последние SpamBot-проверки.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message
from loguru import logger
from sqlalchemy import select

from app.bot.keyboards import main_menu
from app.db.models import Account, AccountStatus, CampaignStatus
from app.db.repositories import accounts as accounts_repo
from app.db.repositories import campaigns as campaigns_repo
from app.db.repositories import logs as logs_repo
from app.db.repositories import spam_check as spam_check_repo
from app.db.session import session_scope

router = Router(name="status")


def _fmt_account_brief(acc: Account, last_check: str | None) -> str:
    now = datetime.now(timezone.utc)
    parts = [f"<b>{acc.phone}</b>", acc.status.value]
    # Причинная метка: FloodWait (§6.3) ≠ ночной quiet-сон (§5.3) ≠ spam-блок (§6.4).
    if accounts_repo.is_flood_waiting(acc, now=now) and acc.spam_unlock_at is not None:
        mins = max(0, int((acc.spam_unlock_at - now).total_seconds() // 60))
        parts.append(f"⏳ FloodWait до {acc.spam_unlock_at:%H:%M} (ещё {mins}м)")
    elif acc.status == AccountStatus.pause and acc.pause_reason == "quiet_hours":
        tail = f" до {acc.spam_unlock_at:%H:%M}" if acc.spam_unlock_at else ""
        parts.append(f"🌙 quiet{tail}")
    elif acc.status == AccountStatus.spam_blocked and acc.spam_unlock_at is not None:
        parts.append(f"🚫 spam до {acc.spam_unlock_at:%H:%M %d.%m}")
    elif acc.spam_unlock_at is not None and acc.spam_unlock_at > now:
        parts.append(f"unlock {acc.spam_unlock_at:%H:%M %d.%m}")
    if acc.limit_reduced_until is not None and acc.limit_reduced_until > now:
        parts.append("75% лимит")
    parts.append(f"sent {acc.daily_sent}")
    parts.append(f"inv {acc.daily_invited}")
    if last_check:
        parts.append(f"SB:{last_check}")
    return " | ".join(parts)


@router.message(Command("status"))
async def cmd_status(message: Message) -> None:
    async with session_scope() as session:
        active_campaigns = await campaigns_repo.list_campaigns(
            session,
            statuses=[CampaignStatus.running, CampaignStatus.paused],
            limit=10,
        )
        result = await session.execute(select(Account).order_by(Account.id))
        accounts = list(result.scalars().all())

        per_account_check: dict[int, str] = {}
        for acc in accounts:
            last = await spam_check_repo.get_last(session, account_id=acc.id)
            if last is not None:
                per_account_check[acc.id] = last.parsed_status

    lines = ["<b>=== Кампании ===</b>"]
    if not active_campaigns:
        lines.append("<i>нет активных или приостановленных</i>")
    else:
        for c in active_campaigns:
            progress_pct = (
                int(c.sent_count / c.total_count * 100) if c.total_count else 0
            )
            lines.append(
                f"#{c.id} [{c.type.value}] <b>{c.status.value}</b> | "
                f"{c.sent_count}/{c.total_count} ({progress_pct}%) | "
                f"skip {c.skipped_count} | fail {c.failed_count}"
            )

    lines.append("")
    lines.append("<b>=== Аккаунты ===</b>")
    if not accounts:
        lines.append("<i>нет аккаунтов</i>")
    else:
        now = datetime.now(timezone.utc)
        flood_now = sum(
            1 for acc in accounts if accounts_repo.is_flood_waiting(acc, now=now)
        )
        for acc in accounts:
            lines.append(
                _fmt_account_brief(acc, per_account_check.get(acc.id))
            )
        if flood_now:
            lines.append("")
            lines.append(f"⏳ В FloodWait сейчас: <b>{flood_now}</b> (детали — /floodwait)")

    await message.answer("\n".join(lines), reply_markup=main_menu())


# ---------------------------------------------------------------------------
# /floodwait — кто сейчас в FloodWait + агрегат за 24ч (§6.3, §10.2)
# ---------------------------------------------------------------------------


@router.message(Command("floodwait"))
@router.callback_query(F.data == "menu:floodwait")
async def cmd_floodwait(event) -> None:
    """Кто сейчас в FloodWait + сколько раз ловили за 24ч. Отдельно от SpamBot:
    FloodWait — локальный rate-limit на действие, а не спам-блок (§6.3)."""
    message = event.message if isinstance(event, CallbackQuery) else event
    if isinstance(event, CallbackQuery):
        await event.answer()
    now = datetime.now(timezone.utc)
    async with session_scope() as session:
        result = await session.execute(select(Account).order_by(Account.id))
        accounts = list(result.scalars().all())
        counts = await logs_repo.flood_wait_counts_since(
            session, since=now - timedelta(hours=24)
        )

    waiting = [a for a in accounts if accounts_repo.is_flood_waiting(a, now=now)]
    lines = ["<b>=== FloodWait ===</b>"]
    if not waiting:
        lines.append("<i>Сейчас никто не в FloodWait.</i>")
    else:
        for a in waiting:
            mins = (
                max(0, int((a.spam_unlock_at - now).total_seconds() // 60))
                if a.spam_unlock_at
                else 0
            )
            lines.append(
                f"<b>{a.phone}</b> — до {a.spam_unlock_at:%H:%M} (ещё {mins}м)"
            )

    lines.append("")
    lines.append("<b>За 24ч (раз ловил FloodWait):</b>")
    if not counts:
        lines.append("<i>нет событий</i>")
    else:
        by_phone = {a.id: a.phone for a in accounts}
        for acc_id, n in sorted(counts.items(), key=lambda kv: kv[1], reverse=True):
            lines.append(f"{by_phone.get(acc_id, acc_id)}: {n}")

    await message.answer("\n".join(lines), reply_markup=main_menu())


# ---------------------------------------------------------------------------
# /spamcheck — принудительный опрос
# ---------------------------------------------------------------------------


async def _trigger_spamcheck_for(message: Message, account_id: int) -> None:
    """Импортируем pool ленивым импортом чтобы избежать circular import."""
    from app.telegram.worker_pool import worker_pool
    from app.telegram.spam_checker import spam_check

    client = worker_pool.get_client(account_id)
    if client is None:
        await message.answer(
            f"Не нашёл активного клиента для аккаунта #{account_id}. "
            "Возможно воркер ещё не поднялся.",
            reply_markup=main_menu(),
        )
        return

    try:
        await spam_check(account_id=account_id, client=client)
    except Exception as e:
        logger.exception("manual spamcheck failed")
        await message.answer(f"Ошибка SpamBot-проверки: {e}", reply_markup=main_menu())
        return

    async with session_scope() as session:
        last = await spam_check_repo.get_last(session, account_id=account_id)

    if last is None:
        await message.answer(
            "SpamBot ответил, но изменений статуса нет. Текущий статус не сохранён.",
            reply_markup=main_menu(),
        )
        return

    text = (
        f"<b>SpamBot для аккаунта #{account_id}:</b>\n"
        f"Распознанный статус: <b>{last.parsed_status}</b>\n"
    )
    if last.unlock_at:
        text += f"Разблокировка до: {last.unlock_at:%Y-%m-%d %H:%M UTC}\n"
    text += f"\n<i>Сырой ответ:</i>\n<code>{last.raw_response[:1500]}</code>"

    await message.answer(text, reply_markup=main_menu())


@router.message(Command("spamcheck"))
async def cmd_spamcheck(message: Message) -> None:
    if message.text is None:
        return
    parts = message.text.split(maxsplit=1)

    if len(parts) < 2:
        # Без аргумента — проверить все.
        async with session_scope() as session:
            accounts = await accounts_repo.list_for_spamcheck(session)
        if not accounts:
            await message.answer(
                "Нет аккаунтов для SpamBot-проверки.", reply_markup=main_menu()
            )
            return
        await message.answer(
            f"Запускаю SpamBot-проверку для {len(accounts)} аккаунт(ов). "
            f"Результаты придут отдельными сообщениями.",
            reply_markup=main_menu(),
        )
        for acc in accounts:
            await _trigger_spamcheck_for(message, acc.id)
        return

    # С аргументом — phone или id.
    key = parts[1].strip().lstrip("@")
    async with session_scope() as session:
        if key.isdigit() and len(key) < 6:
            account = await accounts_repo.get_by_id(session, int(key))
        else:
            phone = key if key.startswith("+") else "+" + key.lstrip("+")
            account = await accounts_repo.get_by_phone(session, phone)
    if account is None:
        await message.answer(
            f"Аккаунт {key!r} не найден.", reply_markup=main_menu()
        )
        return
    await _trigger_spamcheck_for(message, account.id)
