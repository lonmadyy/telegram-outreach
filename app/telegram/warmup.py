"""Warmup-сценарий для новых аккаунтов. ARCHITECTURE.md §5.1.

Гибридный подход (MVP-5):
  • Подписка на WARMUP_CHANNELS — разово при добавлении аккаунта (пока клиент
    авторизован), см. bot/handlers/accounts.py.
  • Имитация присутствия (online + чтение случайного канала) — периодически в
    воркере, пока аккаунт в warmup-периоде (warmup_until > now).
  • Отправка в Saved Messages — опционально, в периоде 12–24ч.

Все действия best-effort: warmup НИКОГДА не должен ронять воркер или отмену
добавления аккаунта. Любая ошибка ловится и логируется в debug.

Outreach-лимиты во время warmup считаются отдельно (accounts.effective_daily_limits
по таблице §5.1) — этот модуль отвечает только за «оживление» аккаунта.
"""

from __future__ import annotations

import random
from dataclasses import dataclass

from loguru import logger
from telethon import TelegramClient
from telethon.tl.functions.account import UpdateStatusRequest
from telethon.tl.functions.channels import JoinChannelRequest

from app.constants import (
    WARMUP_CHANNEL_SUBSCRIBE_AFTER_HOURS,
    WARMUP_CHANNELS,
    WARMUP_SAVED_MESSAGES_AFTER_HOURS,
)

# Нейтральные заметки для Saved Messages (имитация личных записей).
_SAVED_NOTES: tuple[str, ...] = (
    "Напоминание",
    "Заметка на потом",
    "todo",
    "Проверить позже",
    "Идея",
)

# Полный warmup длится 48ч (совпадает с warmup_duration_hours / §5.1).
WARMUP_TOTAL_HOURS = 48


@dataclass(frozen=True)
class WarmupPlan:
    """Что разрешено делать аккаунту данного возраста (часы с момента создания)."""

    in_warmup: bool       # ещё в периоде прогрева (< 48ч)
    allow_subscribe: bool # можно подписываться на каналы (>= 2ч по §5.1)
    allow_presence: bool  # можно «быть онлайн» + читать (весь период warmup)
    allow_saved: bool     # можно писать в Saved Messages (>= 12ч)


def warmup_actions_for_age(hours: float) -> WarmupPlan:
    """Чистая функция: warmup-действия, разрешённые для аккаунта возраста `hours`.

    Пороги — из таблицы §5.1 (через константы WARMUP_*).
    """
    if hours >= WARMUP_TOTAL_HOURS:
        return WarmupPlan(
            in_warmup=False,
            allow_subscribe=False,
            allow_presence=False,
            allow_saved=False,
        )
    return WarmupPlan(
        in_warmup=True,
        allow_subscribe=hours >= WARMUP_CHANNEL_SUBSCRIBE_AFTER_HOURS,
        allow_presence=True,
        allow_saved=hours >= WARMUP_SAVED_MESSAGES_AFTER_HOURS,
    )


async def subscribe_to_warmup_channels(client: TelegramClient) -> int:
    """Подписывает аккаунт на WARMUP_CHANNELS. Возвращает число успешных подписок.

    Best-effort: уже-подписан / приватный / FloodWait / любой сбой по конкретному
    каналу не прерывают остальные. Вызывается один раз при добавлении аккаунта.
    """
    joined = 0
    for channel in WARMUP_CHANNELS:
        try:
            await client(JoinChannelRequest(channel))
            joined += 1
        except Exception as e:  # noqa: BLE001 — best-effort warmup
            logger.debug("warmup: join {!r} failed: {}", channel, e)
    return joined


async def simulate_presence(client: TelegramClient) -> None:
    """Имитация живого присутствия: online-статус + чтение случайного канала.

    Best-effort, ничего не возвращает. §5.1 «онлайн, чтение каналов».
    """
    try:
        await client(UpdateStatusRequest(offline=False))
    except Exception as e:  # noqa: BLE001
        logger.debug("warmup: UpdateStatus failed: {}", e)

    if not WARMUP_CHANNELS:
        return
    channel = random.choice(WARMUP_CHANNELS)
    try:
        await client.get_messages(channel, limit=random.randint(1, 5))
        await client.send_read_acknowledge(channel)
    except Exception as e:  # noqa: BLE001
        logger.debug("warmup: read {!r} failed: {}", channel, e)


async def maybe_saved_message(client: TelegramClient) -> None:
    """Отправка нейтральной заметки в Saved Messages (§5.1, период 12–24ч). Best-effort."""
    try:
        await client.send_message("me", random.choice(_SAVED_NOTES))
    except Exception as e:  # noqa: BLE001
        logger.debug("warmup: saved message failed: {}", e)
