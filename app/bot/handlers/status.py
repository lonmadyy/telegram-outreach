"""Расширенный /status и /spamcheck. ARCHITECTURE.md §10.2.

В /accounts уже есть базовый список. Здесь — сводка по системе целиком:
кампании, аккаунты с детальными статусами, последние SpamBot-проверки.
"""

from __future__ import annotations

from datetime import datetime, timezone

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import Message
from loguru import logger
from sqlalchemy import select

from app.bot.keyboards import main_menu
from app.db.models import Account, CampaignStatus
from app.db.repositories import accounts as accounts_repo
from app.db.repositories import campaigns as campaigns_repo
from app.db.repositories import spam_check as spam_check_repo
from app.db.session import session_scope

router = Router(name="status")


def _fmt_account_brief(acc: Account, last_check: str | None) -> str:
    parts = [f"<b>{acc.phone}</b>", acc.status.value]
    if acc.spam_unlock_at is not None and acc.spam_unlock_at > datetime.now(
        timezone.utc
    ):
        parts.append(f"unlock {acc.spam_unlock_at:%H:%M %d.%m}")
    if acc.limit_reduced_until is not None and acc.limit_reduced_until > datetime.now(
        timezone.utc
    ):
        parts.append("75% лимит")
    parts.append(f"sent {acc.daily_sent}")
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
        for acc in accounts:
            lines.append(
                _fmt_account_brief(acc, per_account_check.get(acc.id))
            )

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
