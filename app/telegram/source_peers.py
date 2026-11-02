"""Получатели из исходных групп парсинга вместо поиска по имени. ARCHITECTURE.md §21.1.

Кампания, созданная из итога /parse, помнит задание парсинга. Перед действием с
получателем аккаунт один раз читает список участников той исходной группы, где этот
получатель нашёлся (200 человек за запрос), и кладёт всех в свой peer_cache. Так на
группу уходит ~N/200 запросов GetParticipants вместо N запросов ResolveUsername.

access_hash привязан к аккаунту, поэтому каждый аккаунт читает группу сам. Кого в
списке нет (был только писавшим, список скрыт, вышел) — worker ищет по имени, как раньше.
Состояние — в памяти процесса; после рестарта группа перечитывается тем же способом.
"""

from __future__ import annotations

import asyncio
import random

from loguru import logger
from telethon import TelegramClient
from telethon.errors import FloodWaitError

from app.db.repositories import parses as parses_repo
from app.db.session import session_scope
from app.telegram.peer_cache import peer_cache, peer_items_from_user

MEMBERS_CAP = 10_000            # без поиска по буквам: дороже экономии на малых кампаниях
CHUNK = 200
CHUNK_PAUSE_SEC = (0.5, 1.5)


def group_entity_input(kind: str, value) -> str | int | None:
    """Токен группы из parse_jobs.groups → вход для get_entity, или None, если этот
    способ недоступен любому аккаунту (приглашение, название — только у парсившего)."""
    if kind == "username":
        return str(value)
    if kind == "id":
        return int(value)
    return None


class SourcePeers:
    def __init__(self) -> None:
        self._index: dict[int, dict[str, list[int]]] = {}   # job_id → username → [group_idx]
        self._groups: dict[int, list] = {}                  # job_id → [[kind, value], …]
        self._done: set[tuple[int, int, int]] = set()       # (account_id, job_id, group_idx)
        self._locks: dict[tuple[int, int, int], asyncio.Lock] = {}
        self._index_lock = asyncio.Lock()

    async def _job_index(self, job_id: int) -> dict[str, list[int]]:
        async with self._index_lock:
            if job_id in self._index:
                return self._index[job_id]
            async with session_scope() as session:
                job = await parses_repo.get_job(session, job_id)
                by_group = (
                    await parses_repo.results_by_group(session, job_id) if job else {}
                )
            index: dict[str, list[int]] = {}
            for idx, names in sorted(by_group.items()):
                for name in names:
                    index.setdefault(name, []).append(idx)
            self._index[job_id] = index
            self._groups[job_id] = list(job.groups) if job else []
            return index

    async def prime(
        self, client: TelegramClient, *, account_id: int, job_id: int, username: str
    ) -> int:
        """Читает (один раз на аккаунт) исходные группы, где есть username. Возвращает,
        сколько peer'ов положено в кэш. FloodWaitError пробрасывается — группа не
        помечается прочитанной, повторим позже."""
        if peer_cache.has(account_id, username):
            return 0
        index = await self._job_index(job_id)
        added = 0
        for idx in index.get(username, []):
            key = (account_id, job_id, idx)
            if key in self._done:
                continue
            lock = self._locks.setdefault(key, asyncio.Lock())
            async with lock:
                if key in self._done:
                    continue
                added += await self._load_group(client, account_id, job_id, idx)
                self._done.add(key)
            if peer_cache.has(account_id, username):
                break
        return added

    async def _load_group(
        self, client: TelegramClient, account_id: int, job_id: int, idx: int
    ) -> int:
        groups = self._groups.get(job_id) or []
        if not 1 <= idx <= len(groups):
            return 0
        kind, value = groups[idx - 1]
        target = group_entity_input(kind, value)
        if target is None:
            return 0
        items = []
        n = 0
        try:
            entity = await client.get_entity(target)
            async for user in client.iter_participants(entity, limit=MEMBERS_CAP):
                n += 1
                items.extend(peer_items_from_user(user))
                if n % CHUNK == 0:
                    await asyncio.sleep(random.uniform(*CHUNK_PAUSE_SEC))
        except FloodWaitError:
            if items:
                await peer_cache.put_many(account_id, items)
            raise
        except Exception as e:  # noqa: BLE001 — нет доступа: ищем по имени, как раньше
            logger.info(
                "source_peers: account {} job {} group {} unavailable: {}",
                account_id, job_id, idx, type(e).__name__,
            )
        await peer_cache.put_many(account_id, items)
        logger.info(
            "source_peers: account {} job {} group {}: {} participants, {} cached",
            account_id, job_id, idx, n, len(items),
        )
        return len(items)


source_peers = SourcePeers()
