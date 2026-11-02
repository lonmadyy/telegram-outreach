"""Получатели из исходных групп (app/telegram/source_peers.py): без сети и БД."""

from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from telethon.errors import FloodWaitError
from telethon.tl.types import User, Username

from app.telegram import source_peers as sp
from app.telegram.peer_cache import PeerCache, peer_items_from_user


def tg_user(uid, username=None, *, bot=False, usernames=None, access_hash=111):
    return User(
        id=uid, access_hash=access_hash, username=username, bot=bot, usernames=usernames
    )


def test_peer_items_from_user():
    assert peer_items_from_user(tg_user(1, "Alice"))[0][0] == "alice"
    multi = peer_items_from_user(
        tg_user(2, None, usernames=[Username(username="Old", active=False), Username(username="Nick", active=True)])
    )
    assert [n for n, _ in multi] == ["nick"]
    assert peer_items_from_user(tg_user(3, "robot", bot=True)) == []
    assert peer_items_from_user(tg_user(4, None)) == []
    assert peer_items_from_user(tg_user(5, "nohash", access_hash=None)) == []


def test_group_entity_input():
    assert sp.group_entity_input("username", "grp") == "grp"
    assert sp.group_entity_input("id", -100) == -100
    assert sp.group_entity_input("invite", "HASH") is None
    assert sp.group_entity_input("name", "Чат") is None


class FakeClient:
    def __init__(self, members, *, flood=False):
        self.members = members
        self.flood = flood
        self.participant_calls = 0

    async def get_entity(self, value):
        return SimpleNamespace(value=value)

    async def iter_participants(self, entity, limit=None):
        self.participant_calls += 1
        for i, u in enumerate(self.members[entity.value]):
            if self.flood and i == 1:
                raise FloodWaitError(request=None, capture=100)
            yield u


@pytest.fixture
def env(monkeypatch):
    cache = PeerCache()
    monkeypatch.setattr(sp, "peer_cache", cache)

    @asynccontextmanager
    async def fake_scope():
        yield None

    job = SimpleNamespace(groups=[["username", "g1"], ["invite", "H"], ["username", "g3"]])

    async def get_job(session, job_id):
        return job

    async def results_by_group(session, job_id):
        return {1: ["alice", "bob"], 2: ["carol"], 3: ["bob", "dave"]}

    async def no_sleep(_):
        return None

    monkeypatch.setattr(sp, "session_scope", fake_scope)
    monkeypatch.setattr(sp.parses_repo, "get_job", get_job)
    monkeypatch.setattr(sp.parses_repo, "results_by_group", results_by_group)
    monkeypatch.setattr(sp.asyncio, "sleep", no_sleep)
    return cache


async def test_prime_reads_group_once_per_account(env):
    client = FakeClient({"g1": [tg_user(1, "Alice"), tg_user(2, "bob")], "g3": [tg_user(4, "dave")]})
    store = sp.SourcePeers()
    assert await store.prime(client, account_id=7, job_id=1, username="alice") == 2
    assert env.has(7, "alice") and env.has(7, "bob")
    # bob уже в кэше — новых запросов нет; другой аккаунт читает сам.
    assert await store.prime(client, account_id=7, job_id=1, username="bob") == 0
    assert client.participant_calls == 1
    assert not env.has(8, "alice")
    await store.prime(client, account_id=8, job_id=1, username="alice")
    assert client.participant_calls == 2 and env.has(8, "alice")


async def test_invite_only_group_skipped_falls_back(env):
    client = FakeClient({})
    store = sp.SourcePeers()
    assert await store.prime(client, account_id=7, job_id=1, username="carol") == 0
    assert client.participant_calls == 0  # приглашение: аккаунту недоступно → поиск по имени


async def test_unknown_username_no_requests(env):
    client = FakeClient({})
    assert await sp.SourcePeers().prime(client, account_id=7, job_id=1, username="zed") == 0
    assert client.participant_calls == 0


async def test_flood_wait_keeps_partial_and_retries_later(env):
    client = FakeClient({"g1": [tg_user(1, "Alice"), tg_user(2, "bob")]}, flood=True)
    store = sp.SourcePeers()
    with pytest.raises(FloodWaitError):
        await store.prime(client, account_id=7, job_id=1, username="bob")
    assert env.has(7, "alice")          # собранное до FloodWait не теряем
    client.flood = False
    await store.prime(client, account_id=7, job_id=1, username="bob")
    assert env.has(7, "bob") and client.participant_calls == 2
