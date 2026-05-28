"""Entry point приложения. ARCHITECTURE.md §18.4 (порядок старта).

MVP-1 поднимает:
1. Логирование
2. Alembic upgrade head
3. Bot polling

Worker pool / Scheduler / LISTEN-loop добавляются в MVP-3.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from alembic import command
from alembic.config import Config
from loguru import logger

from app.bot.main import run_bot
from app.campaigns.manager import resume_running_campaigns_at_startup
from app.db.session import dispose_engine
from app.utils.logging import setup_logging


def _run_migrations() -> None:
    """Накатывает все миграции до head."""
    cfg_path = Path(__file__).resolve().parent.parent / "alembic.ini"
    cfg = Config(str(cfg_path))
    logger.info("Running migrations: alembic upgrade head")
    command.upgrade(cfg, "head")
    logger.info("Migrations applied")


async def _async_main() -> None:
    try:
        # §9.3 + §18.4: recovery «зависших» задач и возобновление running кампаний.
        resumed = await resume_running_campaigns_at_startup()
        if resumed:
            logger.info("Resumed {} running campaign(s)", resumed)

        await run_bot()
    finally:
        await dispose_engine()


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
