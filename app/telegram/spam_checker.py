"""Опрос @SpamBot и парсинг ответов. ARCHITECTURE.md §7.

Парсер ответов (§7.3) — приоритетный pattern matching:
  1. Жёсткий блок с датой → temporary + unlock_at
  2. permanently / unable to lift / навсегда → permanent
  3. good news / no limits / нет ограничений / «свободен от ограничений» → no_limits
  4. spam + may → soft_warning
  5. ничего не совпало → unknown

Реакция на смену статуса (§7.4) делает state-transitions accounts:
  • temporary → set_spam_blocked со spam_unlock_at из ответа
  • permanent → set_dead + notify_admin
  • no_limits → set_active_no_limits (§6.5 — сброс всех таймеров)
  • soft_warning / unknown → не меняем статус
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Literal

from dateutil import parser as dateutil_parser
from loguru import logger
from telethon import TelegramClient
from telethon.errors import FloodWaitError

from app.db.models import Account
from app.db.repositories import accounts as accounts_repo
from app.db.repositories import logs as logs_repo
from app.db.repositories import spam_check as spam_check_repo
from app.db.session import session_scope
from app.notifications.admin import notify_admin
from app.telegram.errors import SESSION_DEAD_ERRORS

ParsedStatus = Literal["no_limits", "soft_warning", "temporary", "permanent", "unknown"]


@dataclass(frozen=True)
class ParsedSpamStatus:
    status: ParsedStatus
    unlock_at: datetime | None = None


# Если SpamBot подтвердил временный блок, но дату снять не удалось — блокируем
# на этот fallback-период, чтобы аккаунт не продолжал слать в бан (§7.3, §16, MVP-6).
DEFAULT_TEMP_BLOCK_HOURS = 24


def resolve_temporary_unlock(
    parsed: ParsedSpamStatus, now: datetime, default_hours: int
) -> datetime:
    """Дата разблокировки для temporary: распознанная из ответа либо fallback
    `now + default_hours`, если дату распарсить не удалось. Чистая функция."""
    if parsed.unlock_at is not None:
        return parsed.unlock_at
    return now + timedelta(hours=default_hours)


# Регулярка под "until 12 January 2025, 17:00 UTC" / "до 12 января ..."
_DATE_RE = re.compile(
    r"(?:until|до)\s+([0-9]{1,2}\s+\w+\s+\d{4}.{0,40}?\d{1,2}:\d{2}.{0,20}?utc)",
    re.IGNORECASE,
)


def parse_spambot_response(text: str) -> ParsedSpamStatus:
    """Точно по псевдокоду §7.3."""
    lower = text.lower()

    # 1. Жёсткий блок с датой.
    m = _DATE_RE.search(text)
    if m:
        unlock_at: datetime | None = None
        try:
            parsed = dateutil_parser.parse(m.group(1), fuzzy=True)
            # Если tz нет — считаем что UTC (т.к. в тексте upstream было "UTC").
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            unlock_at = parsed.astimezone(timezone.utc)
        except Exception:
            logger.debug("SpamBot date parse failed: {!r}", m.group(1))
        return ParsedSpamStatus("temporary", unlock_at=unlock_at)

    # 2. Бессрочный блок.
    if any(k in lower for k in ("permanently", "unable to lift", "навсегда")):
        return ParsedSpamStatus("permanent")

    # 3. Чисто (нет ограничений). Английские и русские формулировки.
    if any(
        k in lower
        for k in (
            "good news",
            "no limits",
            "no restrictions",
            "хорошие новости",
            "нет ограничений",
            "ограничения сняты",
            "ограничений нет",
        )
    ):
        return ParsedSpamStatus("no_limits")
    # Реальный русский ответ: «Ваш аккаунт свободен от каких-либо ограничений.»
    # Распознаём по сочетанию «свобод» + «ограничен». Безопасно: блокирующие
    # ответы содержат «ограничен», но НЕ «свобод» (а дата/«навсегда» уже отсеяны
    # блоками 1–2 выше), поэтому временный/перманентный блок сюда не попадёт.
    if "свобод" in lower and "ограничен" in lower:
        return ParsedSpamStatus("no_limits")

    # 4. Soft warning.
    if "spam" in lower and "may" in lower:
        return ParsedSpamStatus("soft_warning")

    return ParsedSpamStatus("unknown")


# ---------------------------------------------------------------------------
# Контроль частоты обращений к самому SpamBot. §7.5.
# ---------------------------------------------------------------------------

# Для каждого аккаунта храним множитель интервала (по умолчанию 1.0).
# При FloodWait в ответ на /start — удваиваем для этого аккаунта до
# первого успешного контакта.
_intervals_multiplier: dict[int, float] = {}


def get_interval_multiplier(account_id: int) -> float:
    return _intervals_multiplier.get(account_id, 1.0)


def reset_interval_multiplier(account_id: int) -> None:
    _intervals_multiplier.pop(account_id, None)


def double_interval_multiplier(account_id: int) -> None:
    cur = _intervals_multiplier.get(account_id, 1.0)
    _intervals_multiplier[account_id] = min(cur * 2, 8.0)


# ---------------------------------------------------------------------------
# Основная функция — вызывается scheduler-задачей.
# ---------------------------------------------------------------------------


async def spam_check(*, account_id: int, client: TelegramClient) -> None:
    """Один опрос @SpamBot для конкретного аккаунта. §7.2."""
    if not client.is_connected():
        try:
            await client.connect()
        except Exception as e:
            await _log(
                level="warning",
                event_type="spamcheck_failed",
                account_id=account_id,
                message=f"connect failed: {e}",
            )
            return

    try:
        async with client.conversation("SpamBot", timeout=30) as conv:
            await conv.send_message("/start")
            response = await conv.get_response()
    except FloodWaitError as e:
        double_interval_multiplier(account_id)
        await _log(
            level="warning",
            event_type="spamcheck_floodwait",
            account_id=account_id,
            message=f"FloodWait {e.seconds}s от SpamBot, удваиваем интервал",
            payload={"seconds": e.seconds},
        )
        return
    except asyncio.TimeoutError:
        # Шумно, не логируем (§7.2 комментарий).
        return
    except SESSION_DEAD_ERRORS as e:
        # Сессия/аккаунт недействительны (401, напр. «The key is not registered»):
        # переводим в dead, чтобы воркер не висел на мёртвой сессии (§5.1, §16).
        await _mark_account_dead(account_id, str(e))
        return
    except Exception as e:
        await _log(
            level="warning",
            event_type="spamcheck_failed",
            account_id=account_id,
            message=str(e),
        )
        return

    raw = response.text or ""
    parsed = parse_spambot_response(raw)

    # Успешно — сбрасываем множитель.
    reset_interval_multiplier(account_id)

    async with session_scope() as session:
        prev_status = await spam_check_repo.get_last_parsed_status(
            session, account_id=account_id
        )
        if prev_status != parsed.status:
            # Запись только при смене статуса (§4.7 «В обычном режиме»).
            await spam_check_repo.save_check(
                session,
                account_id=account_id,
                raw_response=raw,
                parsed_status=parsed.status,
                unlock_at=parsed.unlock_at,
            )
        elif parsed.status == "unknown":
            # Для unknown сохраняем всегда, чтобы можно было разобрать.
            await spam_check_repo.save_check_always(
                session,
                account_id=account_id,
                raw_response=raw,
                parsed_status=parsed.status,
                unlock_at=parsed.unlock_at,
            )

        # Реакция на изменение статуса (§7.4).
        if prev_status != parsed.status:
            await _react_to_change(
                session=session,
                account_id=account_id,
                prev_status=prev_status,
                parsed=parsed,
            )


async def _react_to_change(
    *,
    session,
    account_id: int,
    prev_status: str | None,
    parsed: ParsedSpamStatus,
) -> None:
    """ARCHITECTURE.md §7.4 таблица переходов."""
    account: Account | None = await accounts_repo.get_by_id(session, account_id)
    if account is None:
        return

    if parsed.status == "temporary":
        # Блокируем даже если дату распознать не удалось: fallback-период не даёт
        # аккаунту продолжать слать в бан (§7.3, §16, MVP-6). raw уже сохранён в
        # spam_check() при смене статуса — здесь только реакция.
        now = datetime.now(timezone.utc)
        unlock_at = resolve_temporary_unlock(parsed, now, DEFAULT_TEMP_BLOCK_HOURS)
        date_known = parsed.unlock_at is not None
        await accounts_repo.set_spam_blocked(
            session,
            account_id=account_id,
            unlock_at=unlock_at,
            limit_reduce_until=now + timedelta(days=7),
        )
        if date_known:
            log_msg = f"SpamBot: блок до {unlock_at:%Y-%m-%d %H:%M UTC}"
            admin_msg = (
                f"⚠ Аккаунт {account.phone}: SpamBot подтвердил блок до "
                f"{unlock_at:%Y-%m-%d %H:%M UTC}"
            )
        else:
            log_msg = (
                f"SpamBot: ограничение без распознанной даты, fallback-блок "
                f"{DEFAULT_TEMP_BLOCK_HOURS}ч (до {unlock_at:%Y-%m-%d %H:%M UTC})"
            )
            admin_msg = (
                f"⚠ Аккаунт {account.phone}: SpamBot подтвердил ограничение, но "
                f"дату снять не удалось. Заблокирован на {DEFAULT_TEMP_BLOCK_HOURS}ч "
                f"(до {unlock_at:%Y-%m-%d %H:%M UTC}). Проверьте вручную через "
                f"/spamcheck и при необходимости скорректируйте."
            )
        await logs_repo.log_event(
            session,
            level="error",
            event_type="spam_check_temporary",
            account_id=account_id,
            message=log_msg,
            payload={"unlock_at": unlock_at.isoformat(), "date_known": date_known},
        )
        await notify_admin(admin_msg)
        return

    if parsed.status == "permanent":
        await accounts_repo.set_dead(session, account_id=account_id)
        await logs_repo.log_event(
            session,
            level="critical",
            event_type="spam_check_permanent",
            account_id=account_id,
            message="SpamBot: перманентное ограничение, аккаунт переведён в dead",
        )
        await notify_admin(
            f"❌ Аккаунт {account.phone} получил ПЕРМАНЕНТНОЕ ограничение "
            f"от Telegram. Переведён в dead, требует ручного решения."
        )
        return

    if parsed.status == "no_limits" and prev_status in (
        "temporary",
        "soft_warning",
        None,
    ):
        # Для соответствия таблице §7.4 строка "temporary/spam_blocked → no_limits"
        # сравниваем с предыдущим parsed_status. None — впервые опрашиваем.
        # И если в БД сейчас pause/spam_blocked/limit_reduced — освобождаем.
        if (
            account.status.value in ("pause", "spam_blocked")
            or account.spam_unlock_at is not None
            or account.limit_reduced_until is not None
        ):
            await accounts_repo.set_active_no_limits(
                session, account_id=account_id
            )
            await logs_repo.log_event(
                session,
                level="info",
                event_type="limit_restored",
                account_id=account_id,
                message="SpamBot: no_limits → аккаунт возвращён в active",
            )
            await notify_admin(
                f"✓ Аккаунт {account.phone}: SpamBot подтвердил отсутствие "
                f"ограничений, лимит восстановлен."
            )
        return

    if parsed.status == "soft_warning":
        await logs_repo.log_event(
            session,
            level="warning",
            event_type="spam_check_soft_warning",
            account_id=account_id,
            message="SpamBot: soft warning (some messages may be considered spam)",
        )
        return

    if parsed.status == "unknown":
        await logs_repo.log_event(
            session,
            level="warning",
            event_type="spam_check_unknown",
            account_id=account_id,
            message="SpamBot ответил нераспознанным текстом, см. raw_response",
        )


async def _mark_account_dead(account_id: int, reason: str) -> None:
    """Сессия аккаунта недействительна (auth 401) — в dead + уведомление админу.
    ARCHITECTURE.md §5.1 (dead), §16. Воркер подхватит статус в начале цикла."""
    logger.error("spam_check: account {} session dead — {}", account_id, reason)
    phone = f"#{account_id}"
    try:
        async with session_scope() as session:
            account = await accounts_repo.get_by_id(session, account_id)
            if account is not None:
                phone = account.phone
            await accounts_repo.set_dead(session, account_id=account_id)
            await logs_repo.log_event(
                session,
                level="error",
                event_type="account_dead",
                account_id=account_id,
                message=f"Сессия невалидна ({reason[:120]}), требуется реавторизация",
            )
    except Exception:
        logger.exception("spam_check: set_dead failed for {}", account_id)
    try:
        await notify_admin(
            f"❌ Аккаунт {phone}: сессия недействительна ({reason[:120]}). "
            f"Переведён в dead — нужна реавторизация через /add_account."
        )
    except Exception:
        pass


async def _log(
    *,
    level: str,
    event_type: str,
    account_id: int,
    message: str,
    payload: dict | None = None,
) -> None:
    try:
        async with session_scope() as session:
            await logs_repo.log_event(
                session,
                level=level,
                event_type=event_type,
                message=message,
                account_id=account_id,
                payload=payload or {},
            )
    except Exception:
        logger.exception("Failed to log spam_check event")
