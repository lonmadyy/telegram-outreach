"""Маппинг исключений Telethon → result_code. ARCHITECTURE.md §16.1.

В MVP-2 покрыты только ошибки рассылки (для invite — добавится в MVP-4).
FloodWaitError и PeerFloodError обрабатываются отдельно в worker (MVP-3
получит полноценные state-transitions; в MVP-2 — простая пауза кампании).
"""

from __future__ import annotations

from telethon.errors import (
    ChatWriteForbiddenError,
    InputUserDeactivatedError,
    UserDeactivatedError,
    UserIsBlockedError,
    UserIsBotError,
    UsernameInvalidError,
    UsernameNotOccupiedError,
    UserPrivacyRestrictedError,
)

from app.db.models import ResultCode

# severity: 'skip' — отметить задачу skipped с этим кодом, продолжить;
# 'fatal' — кампания не может идти дальше (актуально для invite, MVP-4).
ERROR_MAP: dict[type[Exception], tuple[ResultCode, str]] = {
    UserPrivacyRestrictedError: (ResultCode.privacy_restricted, "skip"),
    UsernameInvalidError: (ResultCode.not_found, "skip"),
    UsernameNotOccupiedError: (ResultCode.not_found, "skip"),
    UserDeactivatedError: (ResultCode.deactivated, "skip"),
    InputUserDeactivatedError: (ResultCode.deactivated, "skip"),
    UserIsBlockedError: (ResultCode.privacy_restricted, "skip"),
    UserIsBotError: (ResultCode.not_found, "skip"),
    ChatWriteForbiddenError: (ResultCode.privacy_restricted, "skip"),
}


def classify_error(exc: Exception) -> tuple[ResultCode, str] | None:
    """Возвращает (result_code, severity) или None если исключение не из словаря."""
    for cls, mapping in ERROR_MAP.items():
        if isinstance(exc, cls):
            return mapping
    return None
