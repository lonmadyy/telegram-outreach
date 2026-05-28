"""Конфигурация loguru. ARCHITECTURE.md §15.3.

Файловый лог с ротацией + ретенцией + сжатием. Структурированные события
параллельно пишутся в БД через app.db.repositories.logs.log_event.
"""

from __future__ import annotations

import sys
from pathlib import Path

from loguru import logger

from app.config import settings


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
