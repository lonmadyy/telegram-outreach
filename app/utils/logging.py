"""Конфигурация loguru. ARCHITECTURE.md §15.3.

Файловый лог с ротацией + ретенцией + сжатием. Структурированные события
параллельно пишутся в БД через app.db.repositories.logs.log_event.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

from loguru import logger

from app.config import settings


class _InterceptHandler(logging.Handler):
    """Перенаправляет стандартный `logging` (aiogram, asyncpg, sqlalchemy, alembic,
    apscheduler) в loguru — чтобы их ошибки/трейсбеки были видны в общих логах,
    а не терялись (раньше исключения хэндлеров aiogram уходили «в никуда»)."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            level: str | int = logger.level(record.levelname).name
        except ValueError:
            level = record.levelno
        frame, depth = logging.currentframe(), 2
        while frame is not None and frame.f_code.co_filename == logging.__file__:
            frame = frame.f_back
            depth += 1
        logger.opt(depth=depth, exception=record.exc_info).log(
            level, record.getMessage()
        )


def setup_logging() -> None:
    logger.remove()
    logger.add(
        sys.stderr,
        level=settings.log_level,
        format=(
            "<green>{time:YYYY-MM-DD HH:mm:ss}</green> | "
            "<level>{level: <8}</level> | "
            "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - "
            "<level>{message}</level>"
        ),
        backtrace=True,
        diagnose=False,
    )
    log_dir = Path(settings.logs_path)
    log_dir.mkdir(parents=True, exist_ok=True)
    logger.add(
        log_dir / "app_{time:YYYY-MM-DD}.log",
        rotation="1 day",
        retention="30 days",
        compression="zip",
        level=settings.log_level,
        enqueue=True,
        serialize=False,
        encoding="utf-8",
    )

    # Перехват стандартного logging → loguru: ошибки/трейсбеки aiogram, asyncpg,
    # sqlalchemy, alembic, apscheduler теперь видны в общих логах (а не теряются).
    logging.basicConfig(handlers=[_InterceptHandler()], level=logging.INFO, force=True)
    for _name in ("aiogram", "asyncpg", "sqlalchemy.engine", "alembic", "apscheduler"):
        _std = logging.getLogger(_name)
        _std.handlers = [_InterceptHandler()]
        _std.propagate = False
