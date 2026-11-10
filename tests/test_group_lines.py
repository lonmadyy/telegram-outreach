"""Разбор списка групп для групповой рассылки (app/groups/lines.py, §22.3)."""

from app.groups.lines import (
    MAX_GROUPS_PER_CAMPAIGN,
    GroupEntry,
    parse_entry,
    parse_group_list,
    parse_period,
    ref_key,
)
from app.utils.group_targets import GroupToken


def test_period_forms():
    assert parse_period("90m") == (90, 90)
    assert parse_period("24h") == (1440, 1440)
    assert parse_period("2-4h") == (120, 240)
    assert parse_period("30-200m") == (30, 200)
    assert parse_period("90мин") == (90, 90)
    assert parse_period("3ч") == (180, 180)
    assert parse_period("2 - 4h") == (120, 240)


def test_period_bounds_and_garbage():
    assert parse_period("4m") is None          # меньше 5 минут
    assert parse_period("169h") is None        # больше 7 суток
    assert parse_period("5-3h") is None        # перевёрнутый диапазон
    assert parse_period("abc") is None
    assert parse_period("") is None
    assert parse_period("90") is None          # без единицы — не период


def test_entry_with_period_and_account_any_order():
    e = parse_entry("@python_ru 24h #12")
    assert e == GroupEntry(GroupToken("username", "python_ru"), (1440, 1440), 12)
    e2 = parse_entry("t.me/+HASH1 #3 2-4h")
    assert e2 == GroupEntry(GroupToken("invite", "HASH1"), (120, 240), 3)


def test_entry_rejects_names_and_bad_tails():
    assert parse_entry("Чат города") is None           # по названию нельзя
    assert parse_entry("@python_ru мусор") is None
    assert parse_entry("@python_ru 24h 12h") is None   # два периода
    assert parse_entry("@python_ru #1 #2") is None     # две метки


def test_numeric_id_supported():
    e = parse_entry("-1001234567890")
    assert e is not None and e.token == GroupToken("id", -1001234567890)


def test_ref_key_case_insensitive_username_only():
    assert ref_key(GroupToken("username", "Python_RU")) == "u:python_ru"
    assert ref_key(GroupToken("invite", "AbC")) == "i:AbC"
    assert ref_key(GroupToken("id", -100)) == "id:-100"


def test_list_dedup_comments_invalid():
    text = (
        "# мой список\n"
        "@Python_RU\n"
        "t.me/python_ru 2h\n"      # дубль по ключу — отбрасывается
        "\n"
        "https://t.me/+HASH1\n"
        "Просто название\n"
        "@ok_group 30m #5\n"
    )
    res = parse_group_list(text)
    assert [e.label for e in res.entries] == ["@Python_RU", "t.me/+HASH1", "@ok_group"]
    assert res.duplicates == 1
    assert res.invalid == ["Просто название"]
    assert res.entries[2].period == (30, 30) and res.entries[2].account_id == 5
    assert res.too_many is False


def test_list_too_many():
    text = "\n".join(f"@group_{i:04d}" for i in range(MAX_GROUPS_PER_CAMPAIGN + 1))
    res = parse_group_list(text)
    assert res.too_many is True
