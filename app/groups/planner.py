"""Распределение и расписание групповой рассылки. ARCHITECTURE.md §22.4, §22.5, §22.9.

Чистые функции (без БД и Telethon), покрыты tests/test_group_planner.py.
"""

from __future__ import annotations

import random
import re
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from app.groups.lines import GroupEntry

DISTRIBUTIONS = ("even", "all", "manual")

# Первый пост после нашего вступления — через 5–20 мин (§22.5).
FIRST_POST_AFTER_JOIN_MIN = (5, 20)
# Режим all: после поста другие аккаунты откладывают эту группу на 10–30 мин.
SIBLING_DEFER_MIN = (10, 30)
# Повтор после временной ошибки (§22.6).
RETRY_AFTER_ERROR_MIN = (15, 30)
# Запас после SlowModeWait.
SLOWMODE_EXTRA_SEC = (60, 180)

_HHMM_RE = re.compile(r"([01]?\d|2[0-3]):([0-5]\d)")
_RANGE_RE = re.compile(r"(\d+)(?:\s*[-–—]\s*(\d+))?")


# --- Распределение ----------------------------------------------------------


@dataclass(frozen=True)
class Assignment:
    entry: GroupEntry
    account_id: int


def assign_targets(
    entries: list[GroupEntry],
    account_ids: list[int],
    mode: str,
    *,
    initial_load: dict[int, int] | None = None,
) -> tuple[list[Assignment], list[str]]:
    """Группы × выбранные аккаунты по режиму §22.4 → (назначения, проблемы).

    even   — поровну по кругу (метки #id игнорируются);
    all    — каждый аккаунт во все группы;
    manual — строка с #id → этому аккаунту (он должен быть среди выбранных),
             остальные — поровну с учётом уже назначенных.
    initial_load — сколько групп у аккаунтов уже есть (добавление в работающую рассылку).
    """
    if not account_ids:
        return [], ["не выбрано ни одного аккаунта"]
    if mode not in DISTRIBUTIONS:
        raise ValueError(f"неизвестный режим распределения: {mode!r}")

    if mode == "all":
        return [Assignment(e, a) for e in entries for a in account_ids], []

    load = {a: (initial_load or {}).get(a, 0) for a in account_ids}
    out: list[Assignment] = []
    problems: list[str] = []
    untagged: list[GroupEntry] = []
    for e in entries:
        if mode == "manual" and e.account_id is not None:
            if e.account_id not in load:
                problems.append(f"{e.label}: аккаунт #{e.account_id} не выбран в рассылке")
                continue
            out.append(Assignment(e, e.account_id))
            load[e.account_id] += 1
        else:
            untagged.append(e)
    for e in untagged:
        acc = least_loaded(load)
        out.append(Assignment(e, acc))
        load[acc] += 1
    return out, problems


def least_loaded(load: dict[int, int]) -> int:
    """Аккаунт с наименьшей нагрузкой; при равенстве — первый по порядку выбора."""
    return min(load, key=lambda a: load[a])


def rebalance_orphans(
    orphan_ids: list[int], load: dict[int, int]
) -> dict[int, int]:
    """Сироты (цели выбывшего аккаунта) → наименее загруженным живым аккаунтам.

    `load` — текущее число целей у каждого живого аккаунта рассылки (мутируется копия).
    Возвращает {target_id: account_id}; пусто, если живых аккаунтов нет.
    """
    if not load:
        return {}
    cur = dict(load)
    out: dict[int, int] = {}
    for tid in orphan_ids:
        acc = least_loaded(cur)
        out[tid] = acc
        cur[acc] += 1
    return out


def take_for_new_account(
    new_targets_by_account: dict[int, list[int]], totals: dict[int, int]
) -> list[int]:
    """Какие ещё не начатые цели (new) отдать новому аккаунту even-рассылки (§22.9).

    totals — число целей у каждого существующего аккаунта. Берём у самых загруженных,
    пока новый аккаунт не догонит среднее; трогаем только цели `new` (вступления ещё
    не было — переносить их бесплатно).
    """
    accounts = len(totals) + 1
    total = sum(totals.values())
    fair = total // accounts
    cur = dict(totals)
    pools = {a: list(ids) for a, ids in new_targets_by_account.items()}
    taken: list[int] = []
    while len(taken) < fair:
        donors = [a for a in cur if pools.get(a)]
        if not donors:
            break
        donor = max(donors, key=lambda a: cur[a])
        if cur[donor] <= len(taken) + 1:
            break
        taken.append(pools[donor].pop())
        cur[donor] -= 1
    return taken


# --- Расписание -----------------------------------------------------------


def effective_period(
    campaign_min: int, campaign_max: int, override: tuple[int | None, int | None]
) -> tuple[int, int]:
    """Период повтора цели: свой период строки или период рассылки (минуты)."""
    lo, hi = override
    if lo is not None and hi is not None:
        return lo, hi
    return campaign_min, campaign_max


def next_post_at(
    now: datetime,
    period: tuple[int, int],
    *,
    slowmode_sec: int | None = None,
    rng: random.Random | None = None,
) -> datetime:
    """Следующий пост в группу: случайно из периода, не раньше slowmode (§22.5)."""
    r = rng or random
    lo, hi = period
    delay = r.uniform(lo * 60, hi * 60)
    if slowmode_sec:
        delay = max(delay, float(slowmode_sec))
    return now + timedelta(seconds=delay)


def first_post_at(
    now: datetime,
    *,
    joined_by_us: bool,
    period: tuple[int, int],
    rng: random.Random | None = None,
) -> datetime:
    """Первый пост: после нашего вступления — через 5–20 мин; если аккаунт уже был в
    группе — случайно в пределах нижней границы периода (чтобы не постить всё залпом)."""
    r = rng or random
    if joined_by_us:
        lo, hi = FIRST_POST_AFTER_JOIN_MIN
        return now + timedelta(minutes=r.uniform(lo, hi))
    return now + timedelta(minutes=r.uniform(0, max(1, period[0])))


def random_delay(now: datetime, minutes: tuple[int, int], rng=None) -> datetime:
    r = rng or random
    return now + timedelta(minutes=r.uniform(*minutes))


def gap_seconds(gap_min_sec: int, gap_max_sec: int, rng=None) -> float:
    """Пауза аккаунта после действия групповой линии (§22.5 п.4)."""
    r = rng or random
    lo, hi = sorted((max(1, gap_min_sec), max(1, gap_max_sec)))
    return r.uniform(lo, hi)


# --- Окно работы ----------------------------------------------------------


def _parse_hhmm(s: str) -> time:
    hh, mm = s.strip().split(":", 1)
    return time(int(hh), int(mm))


def in_window(
    now: datetime, start: str | None, end: str | None, tz_name: str
) -> bool:
    """Открыто ли окно работы рассылки (§22.4). Без окна — всегда открыто.
    Окно может переходить через полночь (22:00–02:00)."""
    if not start or not end:
        return True
    local = now.astimezone(ZoneInfo(tz_name)).time()
    s, e = _parse_hhmm(start), _parse_hhmm(end)
    if s == e:
        return True
    if s < e:
        return s <= local < e
    return local >= s or local < e


# --- Разбор ввода в мастере ------------------------------------------------


def parse_minutes_range(
    text: str, *, lo_limit: int, hi_limit: int
) -> tuple[int, int] | None:
    """`30-200` / `30 – 200` / `45` → (lo, hi) в минутах в пределах [lo_limit, hi_limit]."""
    m = _RANGE_RE.fullmatch((text or "").strip())
    if not m:
        return None
    lo = int(m.group(1))
    hi = int(m.group(2)) if m.group(2) else lo
    if lo > hi or lo < lo_limit or hi > hi_limit:
        return None
    return lo, hi


def parse_window(text: str) -> tuple[str, str] | None | bool:
    """`09:00-22:00` → ('09:00', '22:00'); `нет`/`-`/`0` → None (без окна);
    невалидно → False."""
    s = (text or "").strip().lower()
    if s in ("нет", "-", "0", "no", "none", "без окна", "убрать"):
        return None
    parts = re.split(r"\s*[-–—]\s*", s)
    if len(parts) != 2:
        return False
    a, b = (p.strip() for p in parts)
    if not _HHMM_RE.fullmatch(a) or not _HHMM_RE.fullmatch(b) or a == b:
        return False
    norm = lambda x: f"{int(x.split(':')[0]):02d}:{x.split(':')[1]}"  # noqa: E731
    return norm(a), norm(b)


# --- Оценка круга ---------------------------------------------------------


@dataclass(frozen=True)
class RoundEstimate:
    account_id: int
    groups: int
    round_minutes: float
    slower_than_period: bool


def estimate_rounds(
    groups_per_account: dict[int, int],
    *,
    gap_min_sec: int,
    gap_max_sec: int,
    period_min: int,
) -> list[RoundEstimate]:
    """Сколько займёт полный круг по группам у каждого аккаунта (§22.9).

    Круг = групп × средняя пауза. Если он длиннее нижней границы периода — реальная
    частота постов в группу будет ниже заданной.
    """
    avg_gap_min = (gap_min_sec + gap_max_sec) / 2 / 60
    out = []
    for acc, n in groups_per_account.items():
        minutes = n * avg_gap_min
        out.append(
            RoundEstimate(
                account_id=acc,
                groups=n,
                round_minutes=minutes,
                slower_than_period=minutes > period_min,
            )
        )
    return out


def utcnow() -> datetime:
    return datetime.now(timezone.utc)
