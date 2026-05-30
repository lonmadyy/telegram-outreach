"""Entry point приложения. ARCHITECTURE.md §18.4 (точный порядок старта).

Порядок:
  1. setup_logging
  2. alembic upgrade head
  3. settings_cache.refresh_all + PubSubListener('settings_changed')
  4. recover_stuck_in_progress (§9.3)
  5. WorkerPool.start_all для всех не-disabled/dead аккаунтов
  6. Scheduler.start + add_spamcheck_for_account для каждого аккаунта (§7.5)
  7. aiogram polling
  При shutdown — graceful stop пула, scheduler'а, listener'а.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from alembic import command
from alembic.config import Config
from loguru import logger

from app.bot.commands import setup_bot_commands
from app.bot.main import build_bot, build_dispatcher
from app.campaigns.manager import recover_at_startup
from app.db.repositories.settings import settings_cache
from app.db.session import dispose_engine
from app.scheduler.jobs import SchedulerService, set_scheduler
from app.telegram.worker_pool import worker_pool
from app.utils.logging import setup_logging
from app.utils.pubsub import SETTINGS_CHANNEL, PubSubListener


def _run_migrations() -> None:
    cfg_path = Path(__file__).resolve().parent.parent / "alembic.ini"
    cfg = Config(str(cfg_path))
    logger.info("Running migrations: alembic upgrade head")
    command.upgrade(cfg, "head")
    logger.info("Migrations applied")


async def _async_main() -> None:
    bot = None
    listener = PubSubListener(SETTINGS_CHANNEL, settings_cache.invalidate)
    scheduler = SchedulerService(client_provider=worker_pool.get_client)
    # Дать bot-хендлерам доступ к scheduler'у (регистрация spamcheck для
    # аккаунтов, добавленных в рантайме без рестарта — MVP-5).
    set_scheduler(scheduler)

    try:
        # Шаг 3: настройки + LISTEN/NOTIFY.
        await settings_cache.refresh_all()
        listener.start()

        # Шаг 4: recovery зависших задач.
        await recover_at_startup()

        # Шаг 5: пул воркеров.
        started = await worker_pool.start_all()
        logger.info("Started {} worker(s)", started)

        # Шаг 6: scheduler + spamcheck-задачи на каждый аккаунт.
        scheduler.start()
        for account_id in worker_pool.all_account_ids():
            scheduler.add_spamcheck_for_account(account_id)

        # Шаг 7: бот.
        bot = build_bot()
        dp = build_dispatcher()
        logger.info("Starting bot polling")
        await bot.delete_webhook(drop_pending_updates=True)
        await setup_bot_commands(bot)  # нативная кнопка «Меню» (список команд)
        await dp.start_polling(
            bot, allowed_updates=dp.resolve_used_update_types()
        )

    finally:
        logger.info("Shutting down...")
        try:
            await scheduler.stop()
        except Exception:
            pass
        try:
            await listener.stop()
        except Exception:
            pass
        try:
            await worker_pool.stop_all()
        except Exception:
            pass
        if bot is not None:
            try:
                await bot.session.close()
            except Exception:
                pass
        await dispose_engine()
        logger.info("Shutdown complete")


def main() -> None:
    setup_logging()
    logger.info("=== telegram-outreach starting ===")
    try:
        _run_migrations()
    except Exception:
        logger.exception("Migrations failed, aborting startup")
        sys.exit(1)

    try:
        asyncio.run(_async_main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Shutdown by signal")


if __name__ == "__main__":
    main()
