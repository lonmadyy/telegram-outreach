"""Quiet hours и связанные time-утилиты. ARCHITECTURE.md §5.3.

Окно тишины задаётся настройками quiet_hours_start/end/timezone в таблице
settings (можно менять без рестарта). Дефолты — 01:00..07:00 Europe/Minsk.

Воркер использует is_in_quiet_hours() / next_quiet_end_at() для решения
когда спать. quiet_hours_check_job (scheduler) использует те же функции
для массового перевода аккаунтов в pause при входе в окно.
"""

from __future__ import annotations

import random
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

DEFAULT_TZ = "Europe/Minsk"
DEFAULT_START = "01:00"
DEFAULT_END = "07:00"

# Максимальный случайный сдвиг при возобновлении после quiet hours (§5.3).
RESUME_JITTER_MAX_SECONDS = 15 * 60


def _parse_hhmm(s: str) -> time:
    """`01:00` -> time(1, 0). Не валидирует строго; падает только если совсем кривое."""
    hh, mm = s.strip().split(":", 1)
    return time(int(hh), int(mm))


def _get_tz(tz_name: str | None) -> ZoneInfo:
    return ZoneInfo(tz_name or DEFAULT_TZ)


def is_in_quiet_hours(
    *,
    now: datetime | None = None,
    quiet_start: str = DEFAULT_START,
    quiet_end: str = DEFAULT_END,
    tz_name: str = DEFAULT_TZ,
) -> bool:
    """В окне тишины ли мы СЕЙЧАС (с учётом перехода через полночь)."""
    tz = _get_tz(tz_name)
    now_local = (now or datetime.now(timezone.utc)).astimezone(tz)

    start_t = _parse_hhmm(quiet_start)
    end_t = _parse_hhmm(quiet_end)
    cur = now_local.time()

    if start_t < end_t:
        # Простое окно внутри одного дня (наш дефолт 01:00-07:00).
        return start_t <= cur < end_t
    # Окно через полночь (например 23:00-06:00): в окне если cur >= start ИЛИ cur < end.
    return cur >= start_t or cur < end_t


def next_quiet_end_at(
    *,
    now: datetime | None = None,
    quiet_start: str = DEFAULT_START,
    quiet_end: str = DEFAULT_END,
    tz_name: str = DEFAULT_TZ,
    jitter_seconds: int | None = None,
) -> datetime:
    """Ближайший конец окна тишины (UTC).

    Если сейчас в окне → возвращает ближайшее end_t.
    Если сейчас НЕ в окне → возвращает следующее end_t (после следующего входа).
    К результату добавляется случайный jitter 0..RESUME_JITTER_MAX_SECONDS,
    если jitter_seconds не задан (для «размазывания» аккаунтов).
    """
    tz = _get_tz(tz_name)
    now_local = (now or datetime.now(timezone.utc)).astimezone(tz)

    end_t = _parse_hhmm(quiet_end)
    start_t = _parse_hhmm(quiet_start)
    today: date = now_local.date()

    candidate = datetime.combine(today, end_t, tzinfo=tz)

    if start_t < end_t:
        # Дневное окно: end относится к тому же дню что и start.
        if now_local >= candidate:
            candidate = candidate + timedelta(days=1)
    else:
        # Окно через полночь: end относится к УТРУ следующего дня после start.
        # Если сейчас уже после end сегодня — следующий end будет завтра утром.
        if now_local >= candidate:
            candidate = candidate + timedelta(days=1)

    jitter = (
        jitter_seconds
        if jitter_seconds is not None
        else random.randint(0, RESUME_JITTER_MAX_SECONDS)
    )
    return (candidate + timedelta(seconds=jitter)).astimezone(timezone.utc)


def seconds_until_midnight_utc(now: datetime | None = None) -> float:
    """Сколько секунд до следующих 00:00 UTC (для daily_limit_reset и §6.1 шаг 4)."""
    now_utc = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    tomorrow = (now_utc + timedelta(days=1)).date()
    midnight = datetime.combine(tomorrow, time(0, 0), tzinfo=timezone.utc)
    return max(0.0, (midnight - now_utc).total_seconds())
