"""Раннер парсинга на фейковом клиенте (app/parsing/runner.py): без сети и БД."""

from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from telethon.errors import ChatAdminRequiredError, FloodWaitError

from app.parsing import runner
from app.utils.group_targets import GroupToken

NOW = datetime.now(timezone.utc)


class FakeUser:
    def __init__(self, uid, username=None, *, bot=False):
        self.id, self.username, self.bot = uid, username, bot
        self.deleted, self.usernames = False, None


class FakeChannel:
    def __init__(self, title, megagroup=True):
        self.title, self.megagroup = title, megagroup


class FakeChat:
    def __init__(self, title):
        self.title = title


class FakeService:
    def __init__(self, mid, sender):
        self.id, self.sender, self.date = mid, sender, NOW


def msg(mid, sender, age_days=0):
    return SimpleNamespace(id=mid, sender=sender, date=NOW - timedelta(days=age_days))


class FakeClient:
    def __init__(self, groups, *, connected=True):
        self.groups = groups  # name -> dict(entity, members, admins, messages, hidden, flood_at)
        self.connected = connected
        self.flood_raised = False
        self.message_calls = []

    def is_connected(self):
        return self.connected

    async def get_entity(self, value):
        if value not in self.groups:
            raise ValueError("not found")
        return self.groups[value]["entity"]

    def _by_entity(self, entity):
        return next(g for g in self.groups.values() if g["entity"] is entity)

    async def get_participants(self, entity, limit=0):
        g = self._by_entity(entity)
        if g.get("hidden"):
            raise ChatAdminRequiredError(request=None)
        return SimpleNamespace(total=len(g.get("members", [])))

    async def iter_participants(self, entity, search="", filter=None):
        g = self._by_entity(entity)
        users = g.get("admins", []) if filter is not None else g.get("members", [])
        if g.get("hidden") and filter is None:
            raise ChatAdminRequiredError(request=None)
        for u in users:
            yield u

    async def iter_messages(self, entity, limit=None, offset_id=0, wait_time=None):
        self.message_calls.append(offset_id)
        g = self._by_entity(entity)
        items = [m for m in g.get("messages", []) if not offset_id or m.id < offset_id]
        for i, m in enumerate(items):
            if g.get("flood_at") == m.id and not self.flood_raised:
                self.flood_raised = True
                raise FloodWaitError(request=None, capture=3)
            if limit is not None and i >= limit:
                return
            yield m

    async def iter_dialogs(self):
        return
        yield  # pragma: no cover


@pytest.fixture
def env(monkeypatch):
    saved, finished, holds = {}, {}, []

    @asynccontextmanager
    async def fake_scope():
        yield None

    async def save_group(session, *, job_id, group_idx, usernames, report, dropped):
        saved[group_idx] = set(usernames)

    async def finish_job(session, *, job_id, status, report, dropped, error=None):
        finished.update(status=status, report=list(report), dropped=dropped, error=error)

    async def no_pause(self, lo, hi):
        return None

    monkeypatch.setattr(runner, "session_scope", fake_scope)
    monkeypatch.setattr(runner.parses_repo, "save_group", save_group)
    monkeypatch.setattr(runner.parses_repo, "finish_job", finish_job)
    monkeypatch.setattr(runner.worker_pool, "set_hold", lambda aid, on: holds.append(on))
    monkeypatch.setattr(runner.ParseRun, "_pause", no_pause)
    monkeypatch.setattr(runner, "Channel", FakeChannel)
    monkeypatch.setattr(runner, "Chat", FakeChat)
    monkeypatch.setattr(runner, "User", FakeUser)
    monkeypatch.setattr(runner, "MessageService", FakeService)
    return SimpleNamespace(saved=saved, finished=finished, holds=holds)


def make_run(client, tokens, **opts):
    done = []

    async def on_done(job_id, status):
        done.append(status)

    async def on_progress(p):
        return None

    run = runner.ParseRun(
        job_id=1,
        account_id=5,
        client=client,
        tokens=tokens,
        options=runner.ParseOptions(**opts),
        exclude_user_ids={99},
        on_progress=on_progress,
        on_done=on_done,
    )
    runner._runs[1] = run
    return run, done


async def test_members_and_senders_with_filters(env):
    alice, bob = FakeUser(1, "Alice"), FakeUser(2, "bob")
    admin, ours = FakeUser(7, "boss"), FakeUser(99, "our_acc")
    client = FakeClient({
        "grp": {
            "entity": FakeChannel("Группа"),
            "members": [alice, FakeUser(3, "helper_bot", bot=True), admin, ours, FakeUser(4)],
            "admins": [admin],
            "messages": [
                msg(50, bob),
                msg(49, alice),
                FakeService(48, FakeUser(8, "joined_only")),
                msg(10, FakeUser(9, "old_writer"), age_days=60),
            ],
        },
        "news": {"entity": FakeChannel("Канал", megagroup=False)},
    })
    tokens = [GroupToken("username", "grp"), GroupToken("username", "news"), GroupToken("id", -1)]
    run, done = make_run(client, tokens, days=30)
    await run.run()

    assert env.saved[1] == {"alice", "bob"}
    assert env.saved[2] == set() and env.saved[3] == set()
    rep = env.finished["report"]
    assert rep[0]["ok"] and rep[0]["title"] == "Группа" and rep[0]["added"] == 2
    assert "канал" in rep[1]["note"] and not rep[1]["ok"]
    assert "не найдено" in rep[2]["note"]
    assert env.finished["dropped"] == {"bot": 1, "admin": 1, "own_account": 1, "no_username": 1}
    assert env.finished["status"] == "done" and done == ["done"]
    assert env.holds == [False]  # hold снимается в finally (ставит start_parse)


async def test_hidden_members_falls_back_to_senders(env):
    client = FakeClient({
        "grp": {
            "entity": FakeChannel("Скрытая"),
            "hidden": True,
            "messages": [msg(5, FakeUser(1, "writer1"))],
        }
    })
    run, _ = make_run(client, [GroupToken("username", "grp")])
    await run.run()
    assert env.saved[1] == {"writer1"}
    assert "скрыт" in env.finished["report"][0]["note"]


async def test_flood_wait_resumes_history_from_offset(env):
    users = [FakeUser(i, f"user{i}") for i in range(1, 5)]
    client = FakeClient({
        "grp": {
            "entity": FakeChannel("G"),
            "messages": [msg(40, users[0]), msg(30, users[1]), msg(20, users[2]), msg(10, users[3])],
            "flood_at": 20,
        }
    })
    run, _ = make_run(client, [GroupToken("username", "grp")], members=False)
    await run.run()
    assert env.saved[1] == {"user1", "user2", "user3", "user4"}
    assert client.message_calls == [0, 30]  # второй проход — с места остановки


async def test_long_flood_wait_skips_group(env, monkeypatch):
    monkeypatch.setattr(runner, "FLOOD_WAIT_MAX", 2)
    client = FakeClient({
        "grp": {"entity": FakeChannel("G"), "messages": [msg(2, FakeUser(1, "aaaa1")), msg(1, FakeUser(2, "bbbb2"))], "flood_at": 1},
    })
    run, _ = make_run(client, [GroupToken("username", "grp")], members=False)
    await run.run()
    assert env.saved[1] == {"aaaa1"}  # собранное до FloodWait не теряем
    assert "подождать" in env.finished["report"][0]["note"]


async def test_disconnected_account_fails_and_releases_hold(env):
    client = FakeClient({"grp": {"entity": FakeChannel("G")}}, connected=False)
    run, done = make_run(client, [GroupToken("username", "grp")])
    await run.run()
    assert env.finished["status"] == "failed"
    assert "отключился" in env.finished["error"]
    assert env.holds == [False] and done == ["failed"]
    assert 1 not in runner._runs


async def test_stop_keeps_partial(env):
    client = FakeClient({
        "g1": {"entity": FakeChannel("G1"), "members": [FakeUser(1, "first_user")]},
        "g2": {"entity": FakeChannel("G2"), "members": [FakeUser(2, "second_user")]},
    })
    run, done = make_run(client, [GroupToken("username", "g1"), GroupToken("username", "g2")], senders=False)

    original = runner.ParseRun._parse_group

    async def stop_after_first(self, token, entry):
        result = await original(self, token, entry)
        self.stop_event.set()
        return result

    runner.ParseRun._parse_group = stop_after_first
    try:
        await run.run()
    finally:
        runner.ParseRun._parse_group = original
    assert env.saved == {1: {"first_user"}}
    assert env.finished["status"] == "stopped" and done == ["stopped"]


async def test_unexpected_error_in_stage_keeps_other_stage(env):
    class Boom(FakeClient):
        async def get_participants(self, entity, limit=0):
            raise RuntimeError("telegram glitch")

    client = Boom({"grp": {"entity": FakeChannel("G"), "messages": [msg(1, FakeUser(1, "still_here"))]}})
    run, done = make_run(client, [GroupToken("username", "grp")])
    await run.run()
    assert env.saved[1] == {"still_here"}
    assert "участники недоступны: RuntimeError" in env.finished["report"][0]["note"]
    assert done == ["done"]
