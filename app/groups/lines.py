"""Разбор списка групп для групповой рассылки. ARCHITECTURE.md §22.3.

Строка: `<группа> [период] [#аккаунт]`, хвостовые части в любом порядке.
Группа — как в /parse (`app/utils/group_targets.clean_group_token`), но без поиска по
названию: @name, t.me/name, t.me/+HASH, t.me/joinchat/HASH, -100…
Период: `90m`, `24h`, `2-4h`, `30-200m` (`м`/`мин`/`ч`) — от 5 минут до 7 суток.
Чистые функции, покрыты tests/test_group_lines.py.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.utils.group_targets import GroupToken, clean_group_token, token_label

MAX_GROUPS_PER_CAMPAIGN = 500
PERIOD_MIN_MINUTES = 5
PERIOD_MAX_MINUTES = 7 * 24 * 60
SUPPORTED_KINDS = ("username", "invite", "id")

_PERIOD_RE = re.compile(
    r"(\d+)(?:\s*-\s*(\d+))?\s*(m|min|м|мин|h|ч)", re.IGNORECASE
)
_ACCOUNT_RE = re.compile(r"#(\d+)")


@dataclass(frozen=True)
class GroupEntry:
    """Строка списка: группа + свой период (минуты) + метка аккаунта."""

    token: GroupToken
    period: tuple[int, int] | None = None
    account_id: int | None = None

    @property
    def key(self) -> str:
        return ref_key(self.token)

    @property
    def label(self) -> str:
        return token_label(self.token)


@dataclass
class ParsedGroups:
    entries: list[GroupEntry] = field(default_factory=list)
    invalid: list[str] = field(default_factory=list)
    duplicates: int = 0
    too_many: bool = False


def ref_key(token: GroupToken) -> str:
    """Ключ дедупа группы: username без учёта регистра, hash приглашения как есть."""
    if token.kind == "username":
        return f"u:{str(token.value).casefold()}"
    if token.kind == "invite":
        return f"i:{token.value}"
    return f"id:{int(token.value)}"


def parse_period(text: str) -> tuple[int, int] | None:
    """`90m` → (90, 90); `2-4h` → (120, 240). None — не период или вне 5 мин–7 суток."""
    m = _PERIOD_RE.fullmatch((text or "").strip())
    if not m:
        return None
    lo = int(m.group(1))
    hi = int(m.group(2)) if m.group(2) else lo
    mult = 60 if m.group(3).lower() in ("h", "ч") else 1
    lo, hi = lo * mult, hi * mult
    if lo > hi:
        return None
    if lo < PERIOD_MIN_MINUTES or hi > PERIOD_MAX_MINUTES:
        return None
    return lo, hi


def parse_entry(line: str) -> GroupEntry | None:
    """Строка → GroupEntry. None — невалидная строка (но не пустая/комментарий)."""
    parts = line.split()
    if not parts:
        return None
    token = clean_group_token(parts[0])
    if token is None or token.kind not in SUPPORTED_KINDS:
        return None
    period: tuple[int, int] | None = None
    account_id: int | None = None
    for extra in parts[1:]:
        acc = _ACCOUNT_RE.fullmatch(extra)
        if acc and account_id is None:
            account_id = int(acc.group(1))
            continue
        per = parse_period(extra)
        if per is not None and period is None:
            period = per
            continue
        return None
    return GroupEntry(token=token, period=period, account_id=account_id)


def parse_group_list(text: str) -> ParsedGroups:
    """Текст (группа на строку) → уникальные строки в исходном порядке + отчёт."""
    out = ParsedGroups()
    seen: set[str] = set()
    for raw in (text or "").splitlines():
        s = raw.strip()
        if not s or s.startswith("#"):
            continue
        entry = parse_entry(s)
        if entry is None:
            out.invalid.append(s)
            continue
        if entry.key in seen:
            out.duplicates += 1
            continue
        seen.add(entry.key)
        out.entries.append(entry)
    if len(out.entries) > MAX_GROUPS_PER_CAMPAIGN:
        out.too_many = True
    return out
