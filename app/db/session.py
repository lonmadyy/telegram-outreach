"""Async SQLAlchemy session factory."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config import settings

_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def get_engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        _engine = create_async_engine(
            settings.database_url,
            pool_pre_ping=True,
            pool_size=10,
            max_overflow=10,
            # Надёжность при сетевых сбоях / долгом аптайме: соединения не должны
            # «виснуть» вечно на полуоткрытом TCP (см. инцидент с зависшим app).
            pool_recycle=1800,  # переоткрывать соединение старше 30 мин
            pool_timeout=30,    # не ждать свободное соединение из пула дольше 30с
            connect_args={
                "command_timeout": 30,  # asyncpg: запрос не виснет дольше 30с
                "timeout": 15,          # asyncpg: таймаут установки соединения
            },
        )
    return _engine


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    global _sessionmaker
    if _sessionmaker is None:
        _sessionmaker = async_sessionmaker(
            bind=get_engine(),
            expire_on_commit=False,
            class_=AsyncSession,
        )
    return _sessionmaker


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """Контекст для одного запроса/действия с авто-коммитом или ролбэком."""
    sm = get_sessionmaker()
    async with sm() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def dispose_engine() -> None:
    global _engine
    if _engine is not None:
        await _engine.dispose()
        _engine = None
