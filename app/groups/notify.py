"""Уведомления групповой рассылки: копятся и уходят одним сообщением раз в минуту.

ARCHITECTURE.md §22.7. Если аккаунт забанили и он теряет десяток групп подряд, админ
получает одно сообщение, а не десять. `pack` — чистая функция (tests/test_group_notify.py).
"""

from __future__ import annotations

from loguru import logger

from app.notifications.admin import notify_admin

MESSAGE_LIMIT = 3800
HEADER = "📢 <b>Рассылка по группам</b>"

_buffer: list[str] = []


def queue(line: str) -> None:
    """Поставить строку в очередь уведомлений (без await — можно звать откуда угодно)."""
    _buffer.append(line)


def pack(lines: list[str], *, limit: int = MESSAGE_LIMIT) -> list[str]:
    """Строки → сообщения не длиннее limit, у каждого заголовок."""
    messages: list[str] = []
    cur = HEADER
    for line in lines:
        piece = "\n• " + line
        if len(cur) + len(piece) > limit and cur != HEADER:
            messages.append(cur)
            cur = HEADER
        cur += piece[: limit - len(HEADER)]
    if cur != HEADER:
        messages.append(cur)
    return messages


async def flush() -> None:
    """Отправить накопленное (scheduler-джоба раз в минуту)."""
    if not _buffer:
        return
    lines = list(_buffer)
    _buffer.clear()
    for text in pack(lines):
        try:
            await notify_admin(text)
        except Exception:
            logger.exception("group notify flush failed")
