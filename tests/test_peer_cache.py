"""Юнит-тесты PeerCache. ARCHITECTURE.md §5.4.

access_hash привязан к аккаунту → кэш ключуется по (account_id, username).
Без сети: фейковый клиент имитирует get_entity.
"""

from __future__ import annotations

from telethon.tl.types import User

from app.telegram.peer_cache import PeerCache


class _FakeClient:
    """Имитация TelegramClient: считает вызовы get_entity, отдаёт свой access_hash."""

    def __init__(self, access_hash: int, *, not_found: bool = False) -> None:
        self.access_hash = access_hash
        self.not_found = not_found
        self.calls = 0

    async def get_entity(self, username: str):
        self.calls += 1
        if self.not_found:
            raise ValueError("username not found")
        return User(id=1000, access_hash=self.access_hash, username=username.lstrip("@"))


async def test_peer_cache_per_account_isolation():
    cache = PeerCache()
    a = _FakeClient(access_hash=111)
    b = _FakeClient(access_hash=222)

    # аккаунт 1 резолвит — получает СВОЙ access_hash, один вызов get_entity
    p1 = await cache.get_or_resolve(a, "user", account_id=1)
    assert p1 is not None and p1.access_hash == 111
    assert a.calls == 1

    # тот же аккаунт повторно — кэш-хит, повторного резолва нет
    p1b = await cache.get_or_resolve(a, "user", account_id=1)
    assert p1b is not None and p1b.access_hash == 111
    assert a.calls == 1

    # другой аккаунт на тот же username — ОТДЕЛЬНЫЙ резолв со своим access_hash
    p2 = await cache.get_or_resolve(b, "user", account_id=2)
    assert p2 is not None and p2.access_hash == 222
    assert b.calls == 1


async def test_peer_cache_username_not_found():
    cache = PeerCache()
    c = _FakeClient(access_hash=1, not_found=True)
    assert await cache.get_or_resolve(c, "ghost", account_id=1) is None


async def test_peer_cache_invalidate_per_account():
    cache = PeerCache()
    a = _FakeClient(access_hash=111)
    await cache.get_or_resolve(a, "user", account_id=1)
    await cache.invalidate("user", account_id=1)
    # после инвалидации ключа — повторный резолв
    await cache.get_or_resolve(a, "user", account_id=1)
    assert a.calls == 2
