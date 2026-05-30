"""Управляющий бот: Bot, Dispatcher, polling. ARCHITECTURE.md §10."""

from __future__ import annotations

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from loguru import logger

from app.bot.handlers import accounts as accounts_handlers
from app.bot.handlers import campaigns as campaigns_handlers
from app.bot.handlers import common as common_handlers
from app.bot.handlers import export as export_handlers
from app.bot.handlers import settings as settings_handlers
from app.bot.handlers import status as status_handlers
from app.bot.handlers import templates as templates_handlers
from app.bot.middlewares import AuthMiddleware
from app.config import settings
from app.notifications.admin import set_bot as set_notification_bot


def build_bot() -> Bot:
    bot = Bot(
        token=settings.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    # Дать другим модулям возможность отправлять уведомления через тот же инстанс.
    set_notification_bot(bot)
    return bot


def build_dispatcher() -> Dispatcher:
    dp = Dispatcher(storage=MemoryStorage())

    auth = AuthMiddleware()
    dp.message.middleware(auth)
    dp.callback_query.middleware(auth)

    dp.include_router(common_handlers.router)
    dp.include_router(accounts_handlers.router)
    dp.include_router(templates_handlers.router)
    dp.include_router(campaigns_handlers.router)
    dp.include_router(export_handlers.router)
    dp.include_router(settings_handlers.router)
    dp.include_router(status_handlers.router)

    return dp


async def run_bot() -> None:
    """Standalone-режим (без worker pool). Оставлено для возможного использования."""
    bot = build_bot()
    dp = build_dispatcher()
    logger.info("Starting bot polling, allowed_user_ids={}", settings.allowed_user_ids)
    try:
        await bot.delete_webhook(drop_pending_updates=True)
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        await bot.session.close()
