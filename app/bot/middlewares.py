"""Bot middlewares. ARCHITECTURE.md §10.1, §14.2.

AuthMiddleware фильтрует ВСЕ апдейты по whitelist `ALLOWED_USER_IDS`.
Посторонним бот молчит (не отвечает) — это намеренно, чтобы не палить
факт существования бота.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject
from loguru import logger

from app.config import settings


class AuthMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = data.get("event_from_user")
        if user is None:
            return None
        if user.id not in settings.allowed_user_ids:
            logger.warning(
                "Unauthorized access attempt: user_id={}, username={!r}",
                user.id,
                user.username,
            )
            return None
        return await handler(event, data)
