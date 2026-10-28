"""Отсев, дедуп и сборка файлов парсера групп. ARCHITECTURE.md §20.3.

Чистая логика без Telethon/БД: пользователь — любой объект с полями id, username,
usernames, bot, deleted (Telethon User). Покрыто tests/test_parse_collector.py.
"""

from __future__ import annotations

import io
import re
import zipfile
from collections import Counter
from dataclasses import dataclass

_FILENAME_BAD = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_FILENAME_MAX = 60


def primary_username(user) -> str | None:
    """Основной username (lower) или первый активный из коллекционных (Fragment)."""
    name = getattr(user, "username", None)
    if name:
        return name.lower()
    for item in getattr(user, "usernames", None) or []:
        if getattr(item, "active", False) and getattr(item, "username", None):
            return item.username.lower()
    return None


def user_reject_reason(
    user, *, admin_ids: set[int], exclude_user_ids: set[int]
) -> str | None:
    """Почему пользователь не идёт в итог (None — подходит). Порядок важен для счётчиков."""
    if getattr(user, "deleted", False):
        return "deleted"
    if getattr(user, "bot", False):
        return "bot"
    if user.id in exclude_user_ids:
        return "own_account"
    if user.id in admin_ids:
        return "admin"
    if primary_username(user) is None:
        return "no_username"
    return None


class ParseCollector:
    """Копит usernames текущей группы; dropped — счётчики отсева по причинам за задание.

    Повтор одного человека (по user.id) внутри группы не считается ни отсевом, ни
    добавлением. Дедуп между группами делает build_outputs.
    """

    def __init__(self, exclude_user_ids: set[int]) -> None:
        self.exclude_user_ids = set(exclude_user_ids)
        self.dropped: Counter[str] = Counter()
        self._admin_ids: set[int] = set()
        self._seen_ids: set[int] = set()
        self._usernames: set[str] = set()

    def begin_group(self, admin_ids: set[int]) -> None:
        self._admin_ids = set(admin_ids)
        self._seen_ids = set()
        self._usernames = set()

    def add_user(self, user) -> bool:
        """True — пользователь новый для группы и прошёл фильтры."""
        if user.id in self._seen_ids:
            return False
        self._seen_ids.add(user.id)
        reason = user_reject_reason(
            user, admin_ids=self._admin_ids, exclude_user_ids=self.exclude_user_ids
        )
        if reason is not None:
            self.dropped[reason] += 1
            return False
        name = primary_username(user)
        if name in self._usernames:
            return False
        self._usernames.add(name)
        return True

    @property
    def seen_count(self) -> int:
        return len(self._seen_ids)

    def group_usernames(self) -> set[str]:
        return set(self._usernames)


def safe_filename(title: str) -> str:
    name = _FILENAME_BAD.sub("_", (title or "").strip())[:_FILENAME_MAX].strip()
    return name or "group"


def _txt(usernames: list[str]) -> bytes:
    return "".join(f"@{u}\n" for u in usernames).encode("utf-8")


@dataclass
class ParseOutputs:
    all_usernames: list[str]
    all_txt: bytes
    zip_bytes: bytes
    per_group_counts: list[int]
    skipped: int  # уникальных, убранных реестром processed_clients


def build_outputs(
    groups: list[tuple[str, list[str]]], *, skip: set[str]
) -> ParseOutputs:
    """groups: [(название, usernames)] в порядке списка. skip — уже обработанные (lower)."""
    total: set[str] = set()
    per_counts: list[int] = []
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for idx, (title, names) in enumerate(groups, start=1):
            clean = sorted({n for n in names if n not in skip})
            total.update(clean)
            per_counts.append(len(clean))
            zf.writestr(f"{idx:02d}_{safe_filename(title)}.txt", _txt(clean))
    skipped = len({n for _, names in groups for n in names} & skip)
    all_sorted = sorted(total)
    return ParseOutputs(
        all_usernames=all_sorted,
        all_txt=_txt(all_sorted),
        zip_bytes=buf.getvalue(),
        per_group_counts=per_counts,
        skipped=skipped,
    )
