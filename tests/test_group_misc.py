"""Тексты, отчёт, шаблоны, план целей и помощники движка групповой рассылки (§22)."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from telethon.tl.types import (
    Channel,
    Chat,
    ChatPhotoEmpty,
    InputPeerChannel,
    InputPeerChat,
    Username,
)

from app.groups import notify
from app.groups.engine import entity_keys, input_peer, peer_from_entity
from app.groups.formatting import (
    campaign_brief,
    estimate_block,
    gap_range,
    minutes_range,
    progress_block,
    states_line,
    target_line,
    window_text,
)
from app.groups.lines import GroupEntry
from app.groups.planner import RoundEstimate
from app.groups.reporting import HEADER, build_targets_csv
from app.groups.service import plan_targets
from app.groups.templates import check_group_template, unique_name
from app.utils.group_targets import GroupToken

NOW = datetime(2026, 11, 11, 12, 0, tzinfo=timezone.utc)
TZ = "Europe/Minsk"


# --- notify.pack ---


def test_pack_single_and_split():
    msgs = notify.pack(["a", "b"])
    assert len(msgs) == 1 and msgs[0].startswith(notify.HEADER) and "• a" in msgs[0]
    long = ["x" * 100 for _ in range(10)]
    parts = notify.pack(long, limit=350)
    assert len(parts) > 1
    assert all(len(p) <= 350 for p in parts)
    assert sum(p.count("• ") for p in parts) == 10
    assert notify.pack([]) == []


# --- шаблоны ---


def test_check_group_template():
    assert check_group_template("{Привет|Здравствуйте}, это пост") is None
    err = check_group_template("Привет, {first_name}!")
    assert err and "{first_name}" in err
    assert check_group_template("{оборванный|скобки") is not None
    assert check_group_template("") is not None


def test_unique_name():
    assert unique_name("Пиар", set()) == "Пиар"
    assert unique_name("Пиар", {"Пиар"}) == "Пиар (2)"
    assert unique_name("Пиар", {"Пиар", "Пиар (2)"}) == "Пиар (3)"
    assert len(unique_name("x" * 200, {"x" * 128})) <= 128


# --- форматирование ---


def test_ranges_and_window():
    assert minutes_range(30, 200) == "30–200 мин"
    assert minutes_range(120, 240) == "2–4 ч"
    assert minutes_range(90, 90) == "90 мин"
    assert minutes_range(60, 60) == "1 ч"
    assert gap_range(120, 600) == "2–10 мин"
    assert gap_range(90, 90) == "1.5 мин"
    assert window_text(None, None).startswith("весь день")
    assert window_text("09:00", "22:00") == "09:00–22:00"


def _stats(**kw):
    base = dict(
        by_state={"active": 3, "manual": 1}, accounts=2, posts_24h=12, posts_since=2,
        last_post_at=None, next_post_at=None, unassigned=0,
    )
    base.update(kw)
    s = SimpleNamespace(**base)
    s.total = sum(s.by_state.values())
    s.problems = s.by_state.get("manual", 0) + s.by_state.get("excluded", 0)
    return s


def test_states_line_and_brief():
    assert "активна: 3" in states_line({"active": 3})
    assert states_line({}) == "групп нет"
    c = SimpleNamespace(id=7, name="Пиар <IT>", status="running")
    brief = campaign_brief(c, _stats())
    assert "#7" in brief and "&lt;IT&gt;" in brief and "проблемных: 1" in brief


def test_target_line_active_and_problem():
    active = SimpleNamespace(
        state="active", title="Чат", ref_kind="username", ref_value="chat", account_id=3,
        posts_count=5, next_at=NOW + timedelta(hours=1), state_reason=None,
    )
    line = target_line(active, tz_name=TZ, now=NOW)
    assert "Чат" in line and "#3" in line and "постов 5" in line and "16:00" in line
    manual = SimpleNamespace(
        state="manual", title=None, ref_kind="invite", ref_value="H", account_id=None,
        posts_count=0, next_at=None, state_reason="капча",
    )
    line = target_line(manual, tz_name=TZ, now=NOW)
    assert "t.me/+H" in line and "без аккаунта" in line and "капча" in line


def test_estimate_and_progress_blocks():
    block = estimate_block(
        [RoundEstimate(1, 10, 60.0, True), RoundEstimate(2, 3, 18.0, False)], period_min=30
    )
    assert "#1: 10 групп, круг ≈ 1 ч ⚠" in block
    assert "реже заданного" in block
    c = SimpleNamespace(id=1, name="A")
    text = progress_block([(c, _stats())], interval_label="30 мин")
    assert "постов за 30 мин — 2" in text and "проблемных 1" in text


# --- CSV ---


def test_targets_csv():
    t = SimpleNamespace(
        id=1, ref_kind="username", ref_value="chat", title="Чат", account_id=2,
        state="excluded", state_reason="бан", posts_count=4, last_post_at=NOW,
        next_at=None, period_min=60, period_max=120, slowmode_sec=None,
    )
    data = build_targets_csv([t]).decode("utf-8")
    assert data.startswith("﻿")
    rows = data.lstrip("﻿").strip().splitlines()
    assert rows[0].split(";") == HEADER
    assert rows[1].split(";")[:7] == ["1", "@chat", "Чат", "2", "исключена", "бан", "4"]
    assert "60-120" in rows[1]


# --- план целей ---


def _e(name, period=None, acc=None):
    return GroupEntry(GroupToken("username", name), period, acc)


def test_plan_targets_even_all_manual():
    entries = [_e("a", (60, 90)), _e("b"), _e("c", acc=2)]
    even = plan_targets(entries, [1, 2], "even")
    assert even.per_account == {1: 2, 2: 1}
    row = next(r for r in even.rows if r.ref_key == "u:a")
    assert (row.period_min, row.period_max) == (60, 90)
    assert plan_targets(entries, [1, 2], "all").per_account == {1: 3, 2: 3}
    manual = plan_targets(entries, [1, 2], "manual")
    assert next(r for r in manual.rows if r.ref_key == "u:c").account_id == 2


def test_plan_targets_initial_load():
    plan = plan_targets([_e("x"), _e("y")], [1, 2], "even", initial_load={1: 10, 2: 0})
    assert plan.per_account == {2: 2}


# --- помощники движка ---


def test_entity_keys_and_peer():
    mega = Channel(
        id=12345, title="Mega", photo=ChatPhotoEmpty(), date=None, megagroup=True,
        access_hash=777, username="MegaChat",
        usernames=[Username(username="Extra", active=True), Username(username="Old")],
    )
    keys = entity_keys(mega)
    assert "id:-1000000012345" in keys and "u:megachat" in keys and "u:extra" in keys
    assert "u:old" not in keys
    info = peer_from_entity(mega)
    assert info.peer_type == "channel" and info.access_hash == 777

    chat = Chat(id=55, title="Small", photo=ChatPhotoEmpty(), participants_count=3, date=None, version=1)
    assert "id:-55" in entity_keys(chat)
    assert peer_from_entity(chat).peer_type == "chat"


def test_input_peer():
    t = SimpleNamespace(peer_type="channel", chat_id=10, access_hash=99)
    assert input_peer(t) == InputPeerChannel(10, 99)
    t = SimpleNamespace(peer_type="chat", chat_id=11, access_hash=None)
    assert input_peer(t) == InputPeerChat(11)
