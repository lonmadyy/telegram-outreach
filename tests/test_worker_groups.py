"""Роли и групповая линия в цикле воркера (§6.1, §22.1, §22.5).

Главное — аккаунт без роли «группы» проходит прежний путь §6.1 (захват → выполнение →
сон), а с ролью — чередует линии по таймерам и не делает два действия подряд.
"""

from __future__ import annotations

import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
from telethon.errors import FloodWaitError

from app.db.models import Account, AccountStatus
from app.telegram import worker as worker_mod
from app.telegram.worker import WorkerAccount


@asynccontextmanager
async def _fake_scope():
    yield MagicMock()


def _account(*, role_dm=True, role_groups=False) -> Account:
    now = datetime.now(timezone.utc)
    return Account(
        id=1, phone="+10000000000", status=AccountStatus.active,
        created_at=now - timedelta(days=30), warmup_until=None,
        daily_sent=0, daily_invited=0, daily_group_posts=0, daily_group_joins=0,
        spam_unlock_at=None, limit_reduced_until=None, pause_reason=None,
        role_dm=role_dm, role_groups=role_groups,
    )


@pytest.fixture
def w(monkeypatch):
    monkeypatch.setattr(worker_mod, "session_scope", _fake_scope)
    monkeypatch.setattr(worker_mod, "is_in_quiet_hours", lambda **kw: False)
    monkeypatch.setattr(worker_mod, "mark_online", AsyncMock())
    claim = AsyncMock(return_value={"id": 5, "campaign_id": 9, "username": "bob"})
    monkeypatch.setattr(worker_mod.tasks_repo, "claim_next_task", claim)
    worker = WorkerAccount(account_id=1, client=MagicMock())
    worker._next_presence_at = time.monotonic() + 3600
    worker._execute_claimed = AsyncMock()
    worker._sleep = AsyncMock()
    worker._group.step = AsyncMock(return_value=300.0)
    worker._handle_flood_wait = AsyncMock()
    worker._claim = claim
    return worker


async def test_dm_only_account_keeps_old_path(w):
    w._load_account = AsyncMock(return_value=_account(role_dm=True, role_groups=False))
    w._tick_mixed = AsyncMock()
    await w._tick()
    w._tick_mixed.assert_not_called()
    w._claim.assert_awaited_once()
    w._execute_claimed.assert_awaited_once()
    w._group.step.assert_not_called()
    w._sleep.assert_awaited_once()  # пауза §5.2 сразу после задачи, как раньше


async def test_groups_role_goes_mixed_and_dm_off_gives_no_types(w):
    w._load_account = AsyncMock(return_value=_account(role_dm=False, role_groups=True))
    w._tick_mixed = AsyncMock()
    await w._tick()
    w._tick_mixed.assert_awaited_once()
    assert w._tick_mixed.await_args.args[1] == []  # без роли «личка» задач не берём
    w._claim.assert_not_called()


async def test_no_roles_idle_short_sleep(w):
    w._load_account = AsyncMock(return_value=_account(role_dm=False, role_groups=False))
    await w._tick()
    w._claim.assert_not_called()
    w._sleep.assert_awaited_once_with(worker_mod.IDLE_POLL_SECONDS)


async def test_warmup_account_has_no_group_line(w):
    acc = _account(role_dm=False, role_groups=True)
    acc.warmup_until = datetime.now(timezone.utc) + timedelta(hours=10)
    w._load_account = AsyncMock(return_value=acc)
    w._tick_mixed = AsyncMock()
    await w._tick()
    w._tick_mixed.assert_not_called()


async def test_mixed_alternates_and_respects_any_gap(w):
    acc = _account(role_dm=True, role_groups=True)
    # 1) Обе линии готовы: первой идёт личка, группы ждут общий зазор.
    await w._tick_mixed(acc, ["message"])
    w._execute_claimed.assert_awaited_once()
    w._group.step.assert_not_called()
    assert w._next_any_at > time.monotonic()
    # 2) Сразу следующий тик: зазор между действиями не прошёл — только сон.
    await w._tick_mixed(acc, ["message"])
    w._group.step.assert_not_called()
    assert w._execute_claimed.await_count == 1
    # 3) Зазор прошёл, личка ещё на своём интервале — ход групп.
    w._next_any_at = 0.0
    await w._tick_mixed(acc, ["message"])
    w._group.step.assert_awaited_once()
    assert w._next_group_at > time.monotonic() + 200  # пауза рассылки из step()


async def test_mixed_group_floodwait_pauses_account(w):
    acc = _account(role_dm=False, role_groups=True)
    w._group.step = AsyncMock(side_effect=FloodWaitError(request=None, capture=120))
    await w._tick_mixed(acc, [])
    w._handle_flood_wait.assert_awaited_once()
    kwargs = w._handle_flood_wait.await_args.kwargs
    assert kwargs["task_id"] is None and kwargs["seconds"] == 120


async def test_mixed_nothing_to_do_sleeps_bounded(w):
    acc = _account(role_dm=False, role_groups=True)
    w._group.step = AsyncMock(return_value=None)
    await w._tick_mixed(acc, [])
    slept = w._sleep.await_args.args[0]
    assert 1.0 <= slept <= worker_mod.IDLE_POLL_SECONDS
