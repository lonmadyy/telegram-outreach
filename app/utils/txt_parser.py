"""Парсинг TXT-списка username для кампании. ARCHITECTURE.md §9.1 п.1."""

from __future__ import annotations

import re

# Правила Telegram-username (2025): 5–32 символа, латиница/цифры/подчёркивания,
# первая — буква. Tolerant к нижнему регистру: мы уже к моменту валидации привели lower().
_USERNAME_RE = re.compile(r"^[a-z][a-z0-9_]{4,31}$")


def parse_txt_usernames(content: str) -> tuple[list[str], list[str]]:
    """Разбирает текст файла на список валидных username и список «мусора».

    Шаги (§9.1 п.1):
    - strip()
    - lstrip('@')
    - lower()
    - dedup с сохранением порядка
    - валидация по правилам Telegram-username (4–32 символа, a-z 0-9 _).

    Возвращает (valid, invalid). Пустые строки и комментарии (# ...) игнорируются.
    """
    valid: list[str] = []
    invalid: list[str] = []
    seen: set[str] = set()

    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        username = line.lstrip("@").strip().lower()
        if not username:
            continue
        if not _USERNAME_RE.match(username):
            invalid.append(raw_line.strip())
            continue
        if username in seen:
            continue
        seen.add(username)
        valid.append(username)

    return valid, invalid
