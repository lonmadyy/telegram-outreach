"""Юнит-тесты invite-логики. ARCHITECTURE.md §5.2, §5.3, §16.1.

Чистая логика без БД/Telethon (как остальные тесты в tests/).
"""

from __future__ import annotations

import pytest

from app.db.models import ResultCode
from app.telegram import invite as inv


# --- normalize_target_input ---


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("@Foo_Bar", "Foo_Bar"),
        ("Foo_Bar", "Foo_Bar"),
        ("https://t.me/mychat", "mychat"),
        ("http://t.me/mychat", "mychat"),
        ("t.me/mychat", "mychat"),
        ("t.me/mychat/", "mychat"),
        ("telegram.me/mychat", "mychat"),
        ("-1001234567890", -1001234567890),
        ("1234567890", 1234567890),
        ("  @spaced  ", "spaced"),
    ],
)
def test_normalize_valid(raw, expected):
    assert inv.normalize_target_input(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   ",
        None,
        "+AbCdEf",                 # приватная инвайт-ссылка — вне scope §1.3
        "t.me/joinchat/xxx",       # joinchat — вне scope
        "joinchat/xxx",
        "https://t.me/+AbCdEf",
        "a b!c",                   # пробелы/спецсимволы
        "bad name",
    ],
)
def test_normalize_invalid(raw):
    assert inv.normalize_target_input(raw) is None


# --- ParticipantsCache (sync-методы) ---


def test_participants_cache_membership():
    pc = inv.ParticipantsCache()
    assert pc.is_member(-100, 42) is False
    pc.add_member(-100, 42)
    assert pc.is_member(-100, 42) is True
    # другой чат не задет
    assert pc.is_member(-200, 42) is False
    # повторный add идемпотентен
    pc.add_member(-100, 42)
    assert pc.is_member(-100, 42) is True


def test_participants_cache_merge_and_db_loaded():
    # §19 #11: merge подгружает наших ранее приглашённых, db_loaded — флаг «раз на чат».
    pc = inv.ParticipantsCache()
    assert pc.db_loaded(-100) is False
    pc.merge_members(-100, {1, 2, 3})
    assert pc.is_member(-100, 2) is True
    assert pc.is_member(-100, 9) is False
    pc.merge_members(-100, set())  # пустой merge безопасен
    pc.mark_db_loaded(-100)
    assert pc.db_loaded(-100) is True
    assert pc.db_loaded(-200) is False
    # merge не затирает уже добавленных
    pc.add_member(-100, 5)
    pc.merge_members(-100, {7})
    assert pc.is_member(-100, 5) is True
    assert pc.is_member(-100, 7) is True


# --- parse_missing_invitees ---


class _FakeMissingInvitee:
    def __init__(self, user_id: int) -> None:
        self.user_id = user_id


class _FakeResp:
    def __init__(self, missing) -> None:
        self.missing_invitees = missing


def test_missing_invitees_hit_is_skip():
    resp = _FakeResp([_FakeMissingInvitee(7)])
    assert inv.parse_missing_invitees(resp, 7) == ResultCode.privacy_restricted


def test_missing_invitees_empty_is_ok():
    assert inv.parse_missing_invitees(_FakeResp([]), 7) is None


def test_missing_invitees_other_user_is_ok():
    assert inv.parse_missing_invitees(_FakeResp([_FakeMissingInvitee(8)]), 7) is None


def test_missing_invitees_no_attr_is_ok():
    class _Bare:
        pass

    assert inv.parse_missing_invitees(_Bare(), 7) is None


# --- реестр непригодности (§5.3) ---


@pytest.fixture(autouse=True)
def _clear_ineligible():
    inv._ineligible.clear()
    yield
    inv._ineligible.clear()


def test_ineligible_registry_and_quorum():
    assert inv.all_ineligible(5, 2) is False
    inv.mark_ineligible(5, 1)
    assert inv.ineligible_count(5) == 1
    assert inv.all_ineligible(5, 2) is False
    inv.mark_ineligible(5, 2)
    assert inv.ineligible_count(5) == 2
    assert inv.all_ineligible(5, 2) is True   # непригодны все 2 аккаунта
    assert inv.all_ineligible(5, 3) is False  # появился 3-й аккаунт
    # дубликат не увеличивает счётчик
    inv.mark_ineligible(5, 1)
    assert inv.ineligible_count(5) == 2


def test_all_ineligible_zero_accounts_is_false():
    assert inv.all_ineligible(5, 0) is False


def test_reset_campaign():
    inv.mark_ineligible(9, 1)
    assert inv.ineligible_count(9) == 1
    inv.reset_campaign(9)
    assert inv.ineligible_count(9) == 0
