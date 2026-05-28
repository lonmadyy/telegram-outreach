"""Уведомления админу из любого места кода. ARCHITECTURE.md §6.4, §6.5, §10.5.

Используется worker'ом (PeerFlood, dead account), spam_checker'ом
(permanent block), manager'ом (critical errors). Best-effort: если бот
не инициализирован или Telegram недоступен — ошибка только логируется,
не пробрасывается выше (не должна валить рабочий процесс).
"""

from __future__ import annotations

from typing import Iterable

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from loguru import logger

from app.config import settings

# Глобальный handle на инстанс бота, выставляется в bot/main.py при build_bot().
_bot: Bot | None = None


def set_bot(bot: Bot) -> None:
    """Связывает notify_admin с конкретным инстансом aiogram Bot."""
    global _bot
    _bot = bot


def get_bot() -> Bot | None:
    return _bot


async def notify_admin(text: str, *, parse_mode: str | None = None) -> None:
    """Отправляет сообщение всем ALLOWED_USER_IDS. Не бросает наружу."""
    bot = _bot
    if bot is None:
        logger.warning(
            "notify_admin called but bot is not initialized: {!r}", text[:200]
        )
        return

    recipients: Iterable[int] = settings.allowed_user_ids
    for user_id in recipients:
        try:
            await bot.send_message(
                chat_id=user_id,
                text=text,
                parse_mode=parse_mode,
                disable_web_page_preview=True,
            )
        except TelegramAPIError as e:
            logger.warning(
                "notify_admin: send to {} failed: {}", user_id, e
            )
        except Exception:
            logger.exception("notify_admin: unexpected error for user {}", user_id)
