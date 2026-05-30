"""Маппинг исключений Telethon → result_code. ARCHITECTURE.md §16.

Wrapper safe_telegram_action не обрабатывает FloodWait/PeerFlood —
их пробрасывает наружу для специальной логики §6.3/§6.4 в worker.
Skip-исключения превращаются в TaskOutcome с result_code и severity.
ConnectionError/TimeoutError — RetryableError после короткой паузы.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from dataclasses import dataclass

from loguru import logger
from telethon.errors import (
    ChannelPrivateError,
    ChatAdminRequiredError,
    ChatWriteForbiddenError,
    FloodWaitError,
    InputUserDeactivatedError,
    PeerFloodError,
    UnauthorizedError,
    UserAlreadyParticipantError,
    UserBannedInChannelError,
    UserIsBlockedError,
    UserIsBotError,
    UsernameInvalidError,
    UsernameNotOccupiedError,
    UserNotMutualContactError,
    UserPrivacyRestrictedError,
    UsersTooMuchError,
)

from app.db.models import ResultCode

# severity:
#   'skip' — задача помечается skipped, продолжаем работу на этой кампании
#   'fatal' — кампания не может идти дальше (например, нет прав на инвайт),
#             worker должен паузить кампанию.
ERROR_MAP: dict[type[Exception], tuple[ResultCode, str]] = {
    UserPrivacyRestrictedError: (ResultCode.privacy_restricted, "skip"),
    UserNotMutualContactError: (ResultCode.not_mutual_contact, "skip"),
    UsernameInvalidError: (ResultCode.not_found, "skip"),
    UsernameNotOccupiedError: (ResultCode.not_found, "skip"),
    # UserDeactivatedError НЕ здесь: это 401 «наш аккаунт удалён/забанен»
    # (подкласс UnauthorizedError) → SESSION_DEAD_ERRORS, обрабатывается как
    # session-dead в воркере/spam-check (§16, MVP-6). Здесь только
    # InputUserDeactivatedError (код 400) — удалён ПОЛУЧАТЕЛЬ → корректный skip.
    InputUserDeactivatedError: (ResultCode.deactivated, "skip"),
    UserBannedInChannelError: (ResultCode.banned_in_channel, "skip"),
    UserIsBlockedError: (ResultCode.privacy_restricted, "skip"),
    UserIsBotError: (ResultCode.not_found, "skip"),
    ChatWriteForbiddenError: (ResultCode.privacy_restricted, "skip"),
    UsersTooMuchError: (ResultCode.too_many_channels, "skip"),
    # invite: получатель уже в чате (§16.1).
    UserAlreadyParticipantError: (ResultCode.already_member, "skip"),
    # invite-fatal — кампания целевой группы не может ехать дальше:
    ChannelPrivateError: (ResultCode.channel_private, "fatal"),
    ChatAdminRequiredError: (ResultCode.channel_private, "fatal"),
}

# Ошибки, означающие «ИМЕННО ЭТОТ аккаунт не может инвайтить в ЭТОТ чат»
# (не состоит в чате / нет права приглашать). В отличие от message-пути, где
# они fatal для всей кампании (целевой чат недоступен в принципе), в invite-пути
# воркер обрабатывает их per-account: помечает себя непригодным для кампании и
# возвращает задачу в очередь для другого аккаунта (ARCHITECTURE.md §5.3, §16.1).
# Кампания паузится только когда инвайтить не может НИ ОДИН аккаунт.
ACCOUNT_SCOPED_INVITE_ERRORS: tuple[type[Exception], ...] = (
    ChatAdminRequiredError,
    ChannelPrivateError,
)

# Ошибки уровня 401 «наша сессия/аккаунт недействительны»: ключ аннулирован,
# сессия отозвана/истекла, аккаунт деактивирован/забанен. В рантайме (действия,
# spam-check) означают, что аккаунт нужно перевести в dead и остановить воркер —
# а не молча ретраить. UnauthorizedError — базовый класс всех 401 в Telethon
# (включая AuthKeyUnregisteredError «The key is not registered in the system»).
SESSION_DEAD_ERRORS: tuple[type[Exception], ...] = (UnauthorizedError,)


class RetryableError(Exception):
    """Временная сетевая ошибка — стоит ретраить позже."""


@dataclass(frozen=True)
class TaskOutcome:
    code: ResultCode
    severity: str  # 'skip' | 'fatal' | 'ok'
    error: str | None = None


def classify_error(exc: Exception) -> tuple[ResultCode, str] | None:
    for cls, mapping in ERROR_MAP.items():
        if isinstance(exc, cls):
            return mapping
    return None


async def safe_telegram_action(coro: Awaitable) -> TaskOutcome | None:
    """Универсальный wrapper по §16.2.

    Возвращает:
      • None — действие выполнено успешно (вызывающий сам решает что делать).
      • TaskOutcome(skip/fatal) — известная ошибка, надо пометить задачу.

    Пробрасывает наружу:
      • FloodWaitError — для §6.3 handle_flood_wait
      • PeerFloodError — для §6.4 handle_peer_flood
      • RetryableError — после короткой паузы, для шага retry в worker
      • Любую другую неизвестную ошибку — для общего лога и mark_failed.
    """
    try:
        await coro
        return None
    except (FloodWaitError, PeerFloodError):
        raise
    except SESSION_DEAD_ERRORS:
        # 401 «наша сессия/аккаунт мертвы» — спецлогика session-dead в worker
        # (set_dead + стоп воркера). Не глушим в skip и не превращаем в
        # «неизвестную» ошибку (§16, MVP-6).
        raise
    except tuple(ERROR_MAP.keys()) as e:
        result_code, severity = ERROR_MAP[type(e)]
        return TaskOutcome(code=result_code, severity=severity, error=str(e))
    except (ConnectionError, asyncio.TimeoutError) as e:
        logger.warning("Transient network error: {} — backing off 30s", e)
        await asyncio.sleep(30)
        raise RetryableError(str(e)) from e
