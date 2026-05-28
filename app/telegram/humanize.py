"""Имитация человеческого поведения. ARCHITECTURE.md §5.2.

Диапазоны зафиксированы в архитектуре:
- pre_action_pause: 1.5–4.0 секунд (открытие чата перед действием).
- typing: 2.5–7.0 секунд (имитация набора).
- inter_task_delay: 300–540 секунд × джиттер ±15%.

inter_task_delay использует текущие значения из таблицы settings, чтобы
лимиты можно было менять без рестарта (§4.9, MVP-3).
"""

from __future__ import annotations

import asyncio
import random

from telethon import TelegramClient


PRE_ACTION_MIN = 1.5
PRE_ACTION_MAX = 4.0

TYPING_MIN = 2.5
TYPING_MAX = 7.0

JITTER_LOW = 0.85
JITTER_HIGH = 1.15


async def pre_action_pause() -> None:
    """Случайная пауза перед действием с peer'ом (имитация открытия чата)."""
    await asyncio.sleep(random.uniform(PRE_ACTION_MIN, PRE_ACTION_MAX))


async def simulate_typing(client: TelegramClient, peer) -> None:
    """Показать «печатает...» в чате с peer'ом 2.5–7.0 секунд.

    Псевдокод §5.2:
        async with client.action(peer, 'typing'):
            await asyncio.sleep(random.uniform(2.5, 7.0))
    """
    duration = random.uniform(TYPING_MIN, TYPING_MAX)
    async with client.action(peer, "typing"):
        await asyncio.sleep(duration)


def inter_task_delay_seconds(min_sec: int, max_sec: int) -> float:
    """Базовый интервал uniform(min, max) × jitter uniform(0.85, 1.15)."""
    base = random.uniform(min_sec, max_sec)
    return base * random.uniform(JITTER_LOW, JITTER_HIGH)


async def sleep_inter_task(min_sec: int, max_sec: int) -> None:
    delay = inter_task_delay_seconds(min_sec, max_sec)
    await asyncio.sleep(delay)
