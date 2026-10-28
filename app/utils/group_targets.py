"""Разбор списка групп для парсинга (/parse). ARCHITECTURE.md §20.

Одна строка — одна группа. Понимаем @username, t.me/username (в т.ч. ссылку на
сообщение t.me/name/123), приглашения t.me/+HASH и t.me/joinchat/HASH, числовой id
-100… и название группы (ищется среди диалогов аккаунта). Чистые функции — без
Telethon, покрыты tests/test_group_targets.py.
"""

from __future__ import annotations

import re
from typing import NamedTuple

MAX_GROUPS = 100

_LINK_RE = re.compile(r"(?:https?://)?(?:t\.me|telegram\.me)/(.*)$", re.IGNORECASE)
_INVITE_RE = re.compile(r"(?:\+|joinchat/)([A-Za-z0-9_-]+)", re.IGNORECASE)
_USERNAME_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]{3,31}")


class GroupToken(NamedTuple):
    """kind: 'username' | 'invite' | 'id' | 'name'."""

    kind: str
    value: str | int


def clean_group_token(raw: str) -> GroupToken | None:
    """Строка → GroupToken. None — пустая строка, комментарий (#…) или мусор."""
    s = (raw or "").strip()
    if not s or s.startswith("#"):
        return None
    if re.fullmatch(r"-?\d+", s):
        return GroupToken("id", int(s))

    m = _LINK_RE.match(s)
    if m:
        body = m.group(1).strip().strip("/")
        inv = _INVITE_RE.fullmatch(body)
        if inv:
            return GroupToken("invite", inv.group(1))
        # t.me/name/123 (ссылка на сообщение) → name
        head = body.split("/", 1)[0].lstrip("@")
        if _USERNAME_RE.fullmatch(head):
            return GroupToken("username", head)
        return None

    token = s.lstrip("@").strip()
    if not token:
        return None
    if _USERNAME_RE.fullmatch(token):
        return GroupToken("username", token)
    if s.startswith("@"):
        return None  # «@» + не-username — опечатка, а не название
    return GroupToken("name", token)


def _dedup_key(token: GroupToken) -> tuple[str, str]:
    value = token.value
    if token.kind in ("username", "name"):
        value = str(value).casefold()
    return token.kind, str(value)


def parse_group_lines(text: str) -> tuple[list[GroupToken], list[str]]:
    """Текст (по группе в строке) → (уникальные токены в исходном порядке, невалидные строки)."""
    tokens: list[GroupToken] = []
    invalid: list[str] = []
    seen: set[tuple[str, str]] = set()
    for line in (text or "").splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        token = clean_group_token(s)
        if token is None:
            invalid.append(s)
            continue
        key = _dedup_key(token)
        if key in seen:
            continue
        seen.add(key)
        tokens.append(token)
    return tokens, invalid


def token_label(token: GroupToken) -> str:
    """Человекочитаемая метка для статуса и сводки."""
    if token.kind == "username":
        return f"@{token.value}"
    if token.kind == "invite":
        return f"t.me/+{token.value}"
    if token.kind == "id":
        return str(token.value)
    return f"«{token.value}»"
