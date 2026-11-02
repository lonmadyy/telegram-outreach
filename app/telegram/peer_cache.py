"""Кэш разрезолвленных peer'ов (username → user_id, access_hash). ARCHITECTURE.md §5.4.

Не палить ResolveUsername без необходимости: после первого `get_entity`
сохраняем (user_id, access_hash) и переиспользуем через InputPeerUser.

ВАЖНО: `access_hash` в Telegram привязан к запрашивающему аккаунту (его auth-key) —
hash, полученный аккаунтом A, невалиден для аккаунта B (Telegram вернёт PEER_ID_INVALID).
Поэтому кэш ключуется по `(account_id, username)`, а не по одному username.

TTL — 7 дней (юзер мог сменить username). При истечении или сбое — повторный resolve.

В MVP-2 кэш в памяти процесса. Сохранение снимка в БД — отложено (MVP-3+).
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

from telethon import TelegramClient
from telethon.errors import (
    UsernameInvalidError,
    UsernameNotOccupiedError,
)
from telethon.tl.types import InputPeerUser, User

DEFAULT_TTL_SECONDS = 7 * 24 * 3600


@dataclass(frozen=True)
class CachedPeer:
    user_id: int
    access_hash: int
    first_name: str | None
    last_name: str | None
    username: str | None
    resolved_at: float

    def to_input_peer(self) -> InputPeerUser:
        return InputPeerUser(user_id=self.user_id, access_hash=self.access_hash)


class PeerCache:
    def __init__(self, ttl_seconds: int = DEFAULT_TTL_SECONDS) -> None:
        self._ttl = ttl_seconds
        # Ключ — (account_id, username): access_hash привязан к аккаунту.
        self._by_key: dict[tuple[int, str], CachedPeer] = {}
        self._lock = asyncio.Lock()

    def _is_fresh(self, peer: CachedPeer) -> bool:
        return (time.time() - peer.resolved_at) < self._ttl

    async def get_or_resolve(
        self, client: TelegramClient, username: str, account_id: int
    ) -> CachedPeer | None:
        """Резолвит `username` ОТ ИМЕНИ `account_id` (access_hash валиден только
        для него). Возвращает CachedPeer или None если username невалиден/не существует."""
        key = (account_id, username)
        async with self._lock:
            cached = self._by_key.get(key)
            if cached is not None and self._is_fresh(cached):
                return cached

        try:
            entity = await client.get_entity(username)
        except (UsernameInvalidError, UsernameNotOccupiedError, ValueError):
            return None

        if not isinstance(entity, User) or entity.access_hash is None:
            return None

        peer = CachedPeer(
            user_id=entity.id,
            access_hash=entity.access_hash,
            first_name=getattr(entity, "first_name", None),
            last_name=getattr(entity, "last_name", None),
            username=getattr(entity, "username", None),
            resolved_at=time.time(),
        )

        async with self._lock:
            self._by_key[key] = peer
        return peer

    async def put_many(
        self, account_id: int, items: list[tuple[str, CachedPeer]]
    ) -> None:
        """Положить готовые peer'ы (напр. из списка участников группы, §21.1) — без
        ResolveUsername. items: (username в нижнем регистре, как в задачах; peer)."""
        async with self._lock:
            for name, peer in items:
                self._by_key[(account_id, name)] = peer

    def has(self, account_id: int, username: str) -> bool:
        cached = self._by_key.get((account_id, username))
        return cached is not None and self._is_fresh(cached)

    async def invalidate(self, username: str, account_id: int) -> None:
        async with self._lock:
            self._by_key.pop((account_id, username), None)


def peer_items_from_user(user) -> list[tuple[str, CachedPeer]]:
    """Telethon User → [(username lower, CachedPeer)] по основному и активным
    коллекционным username. Пусто для ботов/удалённых/без access_hash или username."""
    if not isinstance(user, User) or user.access_hash is None:
        return []
    if getattr(user, "bot", False) or getattr(user, "deleted", False):
        return []
    names: list[str] = []
    if getattr(user, "username", None):
        names.append(user.username)
    for item in getattr(user, "usernames", None) or []:
        if getattr(item, "active", False) and getattr(item, "username", None):
            names.append(item.username)
    if not names:
        return []
    peer = CachedPeer(
        user_id=user.id,
        access_hash=user.access_hash,
        first_name=getattr(user, "first_name", None),
        last_name=getattr(user, "last_name", None),
        username=names[0],
        resolved_at=time.time(),
    )
    return [(n.lower(), peer) for n in dict.fromkeys(names)]


# Глобальный singleton — один кэш на процесс, ключ (account_id, username):
# access_hash привязан к аккаунту, межаккаунтное переиспользование недопустимо.
peer_cache = PeerCache()
