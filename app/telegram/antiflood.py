"""Антифлуд уровня кампании. ARCHITECTURE.md §5.3.

Горячее in-memory состояние адаптивного интервала + чистые функции решений.
Источник правды — таблица `logs` (события `peer_flood`); scheduler-джоба
`_antiflood_check_job` синхронизирует сюда состояние раз в минуту. Воркер читает
`is_adaptive_active()` синхронно (без обращения к БД из горячего цикла).
"""

from __future__ import annotations

from datetime import datetime

# Горячее состояние: до какого момента держим адаптивный (замедленный) интервал.
# None = адаптивный режим выключен. Обновляется только scheduler-джобой.
_adaptive_until: datetime | None = None


def set_adaptive_until(until: datetime | None) -> None:
    """Установить/снять время действия адаптивного интервала (зовёт джоба)."""
    global _adaptive_until
    _adaptive_until = until


def get_adaptive_until() -> datetime | None:
    return _adaptive_until


def is_adaptive_active(now: datetime) -> bool:
    """Активен ли сейчас адаптивный (замедленный) интервал. Синхронно, без БД."""
    return _adaptive_until is not None and _adaptive_until > now


# ---------------------------------------------------------------------------
# Чистые функции решений (тестируются без БД/Telethon).
# ---------------------------------------------------------------------------


def is_global_flood(flooded_ids: set[int], park_ids: set[int]) -> bool:
    """True, если ВСЕ рабочие аккаунты (park) зафлудили за окно (§5.3).

    `park_ids` — рабочий парк (notin disabled/dead). `flooded_ids` — те, кто
    получил PeerFlood за окно. Кворум = парк непуст и весь покрыт зафлудившими.
    """
    if not park_ids:
        return False
    return park_ids.issubset(flooded_ids)


def should_auto_resume(
    *, has_recovered_account: bool, has_flood_paused_campaigns: bool
) -> bool:
    """Возобновлять ли флуд-паузленные кампании (§5.3): есть что возобновлять и
    появился хотя бы один восстановленный рабочий аккаунт (первый no_limits)."""
    return has_flood_paused_campaigns and has_recovered_account


def pick_interval(
    *, adaptive: bool, normal: tuple[int, int], flood: tuple[int, int]
) -> tuple[int, int]:
    """Выбор диапазона интервала: адаптивный (замедленный) либо обычный."""
    return flood if adaptive else normal


def reset() -> None:
    """Сброс горячего состояния (для тестов и рестарта)."""
    global _adaptive_until
    _adaptive_until = None
