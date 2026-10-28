"""Отсев/дедуп и сборка файлов парсера (app/parsing/collector.py)."""

import io
import zipfile
from types import SimpleNamespace

from app.parsing.collector import (
    ParseCollector,
    build_outputs,
    primary_username,
    safe_filename,
    user_reject_reason,
)


def _u(uid, username=None, *, bot=False, deleted=False, usernames=None):
    return SimpleNamespace(
        id=uid, username=username, bot=bot, deleted=deleted, usernames=usernames
    )


def _active(name):
    return SimpleNamespace(username=name, active=True)


def test_primary_username_falls_back_to_collectible():
    assert primary_username(_u(1, "Main")) == "main"
    assert primary_username(_u(1, None, usernames=[SimpleNamespace(username="x_old", active=False), _active("Nick_2")])) == "nick_2"
    assert primary_username(_u(1, None)) is None


def test_reject_reasons():
    kw = dict(admin_ids={10}, exclude_user_ids={20})
    assert user_reject_reason(_u(1, None), **kw) == "no_username"
    assert user_reject_reason(_u(2, "botik", bot=True), **kw) == "bot"
    assert user_reject_reason(_u(3, "gone", deleted=True), **kw) == "deleted"
    assert user_reject_reason(_u(10, "admin_x"), **kw) == "admin"
    assert user_reject_reason(_u(20, "our_acc"), **kw) == "own_account"
    assert user_reject_reason(_u(5, "good_user"), **kw) is None


def test_collector_dedups_case_insensitive_and_counts_drops():
    c = ParseCollector(exclude_user_ids={99})
    c.begin_group(admin_ids={7})
    assert c.add_user(_u(1, "Alice")) is True
    assert c.add_user(_u(1, "alice")) is False          # тот же человек повторно
    assert c.add_user(_u(7, "the_admin")) is False
    assert c.add_user(_u(99, "ourbot_acc")) is False
    assert c.add_user(_u(2, "spambot", bot=True)) is False
    assert c.group_usernames() == {"alice"}
    assert c.dropped["admin"] == 1 and c.dropped["own_account"] == 1 and c.dropped["bot"] == 1

    # Вторая группа: admin первой здесь обычный участник — попадает.
    c.begin_group(admin_ids=set())
    assert c.add_user(_u(7, "the_admin")) is True
    assert c.add_user(_u(1, "ALICE")) is True           # новый для этой группы
    assert c.group_usernames() == {"the_admin", "alice"}


def test_build_outputs_applies_skip_everywhere():
    groups = [("Чат: Минск/Авто", ["bob", "alice"]), ("second", ["alice", "carol"])]
    out = build_outputs(groups, skip={"carol"})
    assert out.all_usernames == ["alice", "bob"]
    assert out.all_txt == b"@alice\n@bob\n"
    assert out.skipped == 1
    assert out.per_group_counts == [2, 1]
    names = zipfile.ZipFile(io.BytesIO(out.zip_bytes)).namelist()
    assert names == ["01_Чат_ Минск_Авто.txt", "02_second.txt"]
    second = zipfile.ZipFile(io.BytesIO(out.zip_bytes)).read("02_second.txt")
    assert second == b"@alice\n"


def test_build_outputs_empty():
    out = build_outputs([("g", ["x_user"])], skip={"x_user"})
    assert out.all_usernames == [] and out.all_txt == b""


def test_safe_filename():
    assert safe_filename('a<b>:"/\\|?*c') == "a_b________c"
    assert safe_filename("   ") == "group"
    assert len(safe_filename("я" * 200)) == 60
