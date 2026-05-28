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
    UserBannedInChannelError,
    UserDeactivatedError,
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
    UserDeactivatedError: (ResultCode.deactivated, "skip"),
    InputUserDeactivatedError: (ResultCode.deactivated, "skip"),
    UserBannedInChannelError: (ResultCode.banned_in_channel, "skip"),
    UserIsBlockedError: (ResultCode.privacy_restricted, "skip"),
    UserIsBotError: (ResultCode.not_found, "skip"),
    ChatWriteForbiddenError: (ResultCode.privacy_restricted, "skip"),
    UsersTooMuchError: (ResultCode.too_many_channels, "skip"),
    # invite-fatal — кампания целевой группы не может ехать дальше:
    ChannelPrivateError: (ResultCode.channel_private, "fatal"),
    ChatAdminRequiredError: (ResultCode.channel_private, "fatal"),
}


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
    except tuple(ERROR_MAP.keys()) as e:
        result_code, severity = ERROR_MAP[type(e)]
        return TaskOutcome(code=result_code, severity=severity, error=str(e))
    except (ConnectionError, asyncio.TimeoutError) as e:
        logger.warning("Transient network error: {} — backing off 30s", e)
        await asyncio.sleep(30)
        raise RetryableError(str(e)) from e
