"""Управляющий бот: Bot, Dispatcher, polling. ARCHITECTURE.md §10."""

from __future__ import annotations

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from loguru import logger

from app.bot.handlers import accounts as accounts_handlers
from app.bot.handlers import common as common_handlers
from app.bot.middlewares import AuthMiddleware
from app.config import settings


def build_bot() -> Bot:
    return Bot(
        token=settings.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )


def build_dispatcher() -> Dispatcher:
    dp = Dispatcher(storage=MemoryStorage())

    auth = AuthMiddleware()
    dp.message.middleware(auth)
    dp.callback_query.middleware(auth)

    dp.include_router(common_handlers.router)
    dp.include_router(accounts_handlers.router)

    return dp


async def run_bot() -> None:
    bot = build_bot()
    dp = build_dispatcher()
    logger.info("Starting bot polling, allowed_user_ids={}", settings.allowed_user_ids)
    try:
        await bot.delete_webhook(drop_pending_updates=True)
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        await bot.session.close()
