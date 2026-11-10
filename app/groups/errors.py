"""Классификация ошибок групповой линии. ARCHITECTURE.md §22.5, §22.6.

FloodWait, PeerFlood и 401 (сессия) сюда не попадают — их обрабатывает воркер на
уровне аккаунта. Здесь — то, что касается одной группы. Покрыто
tests/test_group_errors.py.
"""

from __future__ import annotations

from dataclasses import dataclass

from telethon.errors import (
    ChannelInvalidError,
    ChannelPrivateError,
    ChannelPublicGroupNaError,
    ChannelsTooMuchError,
    ChatAdminRequiredError,
    ChatForbiddenError,
    ChatGuestSendForbiddenError,
    ChatIdInvalidError,
    ChatRestrictedError,
    ChatSendPlainForbiddenError,
    ChatWriteForbiddenError,
    InviteHashExpiredError,
    InviteHashInvalidError,
    InviteRequestSentError,
    PeerIdInvalidError,
    SlowModeWaitError,
    UserAlreadyParticipantError,
    UserBannedInChannelError,
    UsernameInvalidError,
    UsernameNotOccupiedError,
)

# Писать нельзя: до первого поста — скорее капча/подписка/«только админы» (manual),
# после постов — запретили писать (excluded).
_WRITE_FORBIDDEN = (
    ChatWriteForbiddenError,
    ChatRestrictedError,
    ChatSendPlainForbiddenError,
    ChatGuestSendForbiddenError,
    ChatAdminRequiredError,
)
# Группа недоступна или удалена.
_GONE = (
    ChannelPrivateError,
    ChannelInvalidError,
    ChatIdInvalidError,
    PeerIdInvalidError,
    ChatForbiddenError,
    ChannelPublicGroupNaError,
)


@dataclass(frozen=True)
class PostOutcome:
    """action: slowmode | manual | exclude | ban_check | retry."""

    action: str
    reason: str
    wait_sec: int = 0


def classify_post_error(exc: Exception, *, posts_count: int) -> PostOutcome:
    if isinstance(exc, SlowModeWaitError):
        return PostOutcome("slowmode", "slowmode", wait_sec=int(exc.seconds or 0))
    if isinstance(exc, UserBannedInChannelError):
        # Так Telegram отвечает и на бан в чате, и на спам-ограничение всего аккаунта:
        # не исключаем сразу, считаем ошибкой подряд + внеочередной SpamBot-чек.
        return PostOutcome("ban_check", "аккаунту запрещено писать в группах")
    if isinstance(exc, _WRITE_FORBIDDEN):
        if posts_count <= 0:
            return PostOutcome(
                "manual",
                "нельзя писать: капча, подписка на канал или пишут только админы",
            )
        return PostOutcome("exclude", "аккаунту запретили писать в группе")
    if isinstance(exc, _GONE):
        return PostOutcome("exclude", "группа недоступна или удалена")
    return PostOutcome("retry", f"{type(exc).__name__}: {str(exc)[:150]}")


@dataclass(frozen=True)
class JoinOutcome:
    """action: member | pending | manual | exclude | too_many | retry."""

    action: str
    reason: str


def classify_join_error(exc: Exception) -> JoinOutcome:
    if isinstance(exc, UserAlreadyParticipantError):
        return JoinOutcome("member", "")
    if isinstance(exc, InviteRequestSentError):
        return JoinOutcome("pending", "заявка на вступление отправлена")
    if isinstance(exc, ChannelsTooMuchError):
        return JoinOutcome("too_many", "аккаунт состоит в максимуме групп и каналов")
    if isinstance(exc, (InviteHashExpiredError, InviteHashInvalidError)):
        return JoinOutcome("exclude", "ссылка-приглашение недействительна или истекла")
    if isinstance(exc, (UsernameNotOccupiedError, UsernameInvalidError)):
        return JoinOutcome("exclude", "группа не найдена")
    if isinstance(exc, UserBannedInChannelError):
        return JoinOutcome("exclude", "аккаунт забанен в этой группе")
    if isinstance(exc, _GONE):
        return JoinOutcome("exclude", "группа приватная или удалена")
    if isinstance(exc, ValueError):
        # Telethon get_entity: «No user has … as username» / не найдено.
        return JoinOutcome("exclude", "группа не найдена")
    return JoinOutcome("retry", f"{type(exc).__name__}: {str(exc)[:150]}")


def entity_kind_problem(entity) -> str | None:
    """Почему в эту сущность нельзя постить как в группу. None — это группа.

    Обычный чат (Chat) и супергруппа (Channel.megagroup) — группы. Канал-витрина
    (Channel.broadcast без megagroup) и пользователь/бот — нет.
    """
    from telethon.tl.types import Channel, Chat

    if isinstance(entity, Chat):
        return None
    if isinstance(entity, Channel):
        if getattr(entity, "megagroup", False) or getattr(entity, "gigagroup", False):
            return None
        return "это канал, а не группа — писать в него нельзя"
    return "это не группа"
