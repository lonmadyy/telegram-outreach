"""Разбор списка групп для /parse (app/utils/group_targets.py)."""

from app.utils.group_targets import (
    GroupToken,
    clean_group_token,
    parse_group_lines,
    token_label,
)


def test_username_forms():
    assert clean_group_token("@python_ru") == GroupToken("username", "python_ru")
    assert clean_group_token("python_ru") == GroupToken("username", "python_ru")
    assert clean_group_token("t.me/python_ru") == GroupToken("username", "python_ru")
    assert clean_group_token("https://t.me/python_ru/") == GroupToken("username", "python_ru")
    assert clean_group_token("telegram.me/Python_Ru") == GroupToken("username", "Python_Ru")


def test_message_link_takes_chat_username():
    # Ссылка на сообщение t.me/name/123 — это та же группа.
    assert clean_group_token("https://t.me/python_ru/12345") == GroupToken("username", "python_ru")


def test_invite_links():
    assert clean_group_token("https://t.me/+AbCd_12-x") == GroupToken("invite", "AbCd_12-x")
    assert clean_group_token("t.me/joinchat/AbCd12") == GroupToken("invite", "AbCd12")


def test_numeric_id():
    assert clean_group_token("-1001234567890") == GroupToken("id", -1001234567890)


def test_title_and_skips():
    assert clean_group_token("Чат города Минск") == GroupToken("name", "Чат города Минск")
    assert clean_group_token("") is None
    assert clean_group_token("   ") is None
    assert clean_group_token("# комментарий") is None


def test_parse_lines_dedup_case_insensitive():
    text = "@Python_RU\nt.me/python_ru\n\n# x\nhttps://t.me/+HASH1\nt.me/+HASH1\nЧат\n"
    tokens, invalid = parse_group_lines(text)
    assert tokens == [
        GroupToken("username", "Python_RU"),
        GroupToken("invite", "HASH1"),
        GroupToken("name", "Чат"),
    ]
    assert invalid == []


def test_parse_lines_invalid():
    tokens, invalid = parse_group_lines("t.me/\n@\nok_group")
    assert tokens == [GroupToken("username", "ok_group")]
    assert invalid == ["t.me/", "@"]


def test_labels():
    assert token_label(GroupToken("username", "abcde")) == "@abcde"
    assert token_label(GroupToken("invite", "H")) == "t.me/+H"
    assert token_label(GroupToken("id", -100)) == "-100"
    assert token_label(GroupToken("name", "Чат")) == "«Чат»"
