"""Singleton-пул долгоживущих воркеров. ARCHITECTURE.md §3, §18.4.

Хранит mapping account_id → WorkerAccount. Каждый воркер сам по себе:
открывает Telethon-клиент, подбирает задачи через SKIP LOCKED, спит между
действиями. Pool отвечает за их lifecycle (start/stop) и предоставляет
get_client для spam_checker.
"""

from __future__ import annotations

import asyncio

from loguru import logger
from telethon import TelegramClient

from app.db.models import Account
from app.db.repositories import accounts as accounts_repo
from app.db.session import session_scope
from app.telegram.client_factory import create_client
from app.telegram.worker import WorkerAccount


class WorkerPool:
    def __init__(self) -> None:
        self._workers: dict[int, WorkerAccount] = {}
        self._lock = asyncio.Lock()

    async def start_for(self, account: Account) -> WorkerAccount | None:
        """Создаёт и запускает воркер для аккаунта. Уже запущенные пропускаются."""
        async with self._lock:
            existing = self._workers.get(account.id)
            if existing is not None and existing.is_running:
                return existing

            client = create_client(phone=account.phone, proxy_url=account.proxy_url)
            try:
                await client.connect()
            except Exception:
                logger.exception(
                    "WorkerPool: connect failed for account_id={}", account.id
                )
                try:
                    await client.disconnect()
                except Exception:
                    pass
                return None

            worker = WorkerAccount(account_id=account.id, client=client)
            self._workers[account.id] = worker
            worker.start()
            logger.info(
                "WorkerPool: started worker for account_id={} ({})",
                account.id, account.phone,
            )
            return worker

    async def stop_for(self, account_id: int) -> None:
        async with self._lock:
            worker = self._workers.pop(account_id, None)
        if worker is not None:
            await worker.stop()
            logger.info("WorkerPool: stopped worker for account_id={}", account_id)

    async def start_all(self) -> int:
        """Поднимает воркеры для всех не-disabled/dead аккаунтов. §18.4 шаг 4-5."""
        async with session_scope() as session:
            accounts = await accounts_repo.list_for_worker_pool(session)
        started = 0
        for acc in accounts:
            worker = await self.start_for(acc)
            if worker is not None:
                started += 1
        logger.info("WorkerPool: started {} workers", started)
        return started

    async def stop_all(self) -> None:
        async with self._lock:
            ids = list(self._workers.keys())
            workers = list(self._workers.values())
            self._workers.clear()
        await asyncio.gather(
            *(w.stop() for w in workers), return_exceptions=True
        )
        logger.info("WorkerPool: stopped {} workers", len(ids))

    # ------------------ Accessors ------------------

    def get_client(self, account_id: int) -> TelegramClient | None:
        worker = self._workers.get(account_id)
        if worker is None:
            return None
        return worker.client

    def all_account_ids(self) -> list[int]:
        return list(self._workers.keys())


worker_pool = WorkerPool()
