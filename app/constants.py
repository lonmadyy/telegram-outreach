"""Константы проекта. Расширяется в MVP-5 (WARMUP_CHANNELS и пр.)."""

from __future__ import annotations

# Каналы для warmup-подписки (MVP-5).
# Public-каналы, безопасны для подписки, типичны для нормальных пользователей.
WARMUP_CHANNELS: tuple[str, ...] = (
    "@durov",
    "@telegram",
    "@TelegramTips",
)

WARMUP_INITIAL_QUIET_HOURS = 2
WARMUP_CHANNEL_SUBSCRIBE_AFTER_HOURS = 2
WARMUP_SAVED_MESSAGES_AFTER_HOURS = 12
WARMUP_HALF_LIMIT_AFTER_HOURS = 24
