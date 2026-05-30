"""Invite-логика: резолв целевого чата, проверка прав, кэш участников,
выполнение InviteToChannelRequest. ARCHITECTURE.md §1.3, §5.2, §10.4, §16.1.

Ключевые свойства (MVP-4):
  • Инвайт только через InviteToChannelRequest от аккаунта-участника с правом
    приглашать (§1.3). По ссылке не приглашаем.
  • Кэш членства (participants) — in-memory на процесс, префетч при первом
    обращении к чату; после успешного инвайта user_id добавляется в кэш.
    DB-снимок (§5.2) отложен — см. §19.
  • Подтверждение инвайта: помимо исключений инспектируем `missing_invitees`
    в ответе — если Telegram молча не добавил юзера (приватность), считаем skip,
    НЕ пишем в processed_clients и НЕ инкрементим счётчик (§4.6).
  • Per-account устойчивость: если конкретный аккаунт не может инвайтить в этот
    чат (не участник / нет прав), он помечается непригодным для кампании, а не
    валит её целиком. Решение о паузе принимает worker, когда непригодны все
    аккаунты (§5.3). Реестр непригодности живёт здесь, чтобы не импортировать
    worker_pool (циклический импорт).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from loguru import logger
from telethon import TelegramClient, utils
from telethon.errors import (
    ChannelPrivateError,
    ChatAdminRequiredError,
    FloodWaitError,
    UsernameInvalidError,
    UsernameNotOccupiedError,
)
from telethon.tl.functions.channels import InviteToChannelRequest
from telethon.tl.types import Channel

from app.db.models import ResultCode

# Сколько участников максимум тянем в кэш членства при префетче.
DEFAULT_PARTICIPANTS_CAP = 50_000


# ---------------------------------------------------------------------------
# Нормализация ввода целевого чата. Чистая функция (тестируется).
# ---------------------------------------------------------------------------


def normalize_target_input(raw: str) -> str | int | None:
    """`@name` / `https://t.me/name` / `t.me/name` / `-100123` / `123` → форма
    для get_entity (username без `@` либо int id). None если ввод пустой/мусор.
    """
    if raw is None:
        return None
    s = raw.strip()
    if not s:
        return None

    # Числовой id (в т.ч. -100…): отдаём как int.
    if s.lstrip("-").isdigit():
        try:
            return int(s)
        except ValueError:
            return None

    # Срезаем схему/домен t.me.
    for prefix in ("https://", "http://"):
        if s.lower().startswith(prefix):
            s = s[len(prefix):]
            break
    for dom in ("t.me/", "telegram.me/"):
        if s.lower().startswith(dom):
            s = s[len(dom):]
            break

    s = s.lstrip("@").strip("/")
    if not s:
        return None
    # Приглашения по ссылке (+hash / joinchat) в scope MVP-4 не поддерживаем (§1.3).
    if s.startswith("+") or s.lower().startswith("joinchat"):
        return None
    # Username Telegram: буквы/цифры/подчёркивание.
    if all(ch.isalnum() or ch == "_" for ch in s):
        return s
    return None


# ---------------------------------------------------------------------------
# Резолв целевого чата и проверка прав на инвайт.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ResolvedTarget:
    chat_id: int  # marked id (-100…), ключ кэша членства
    title: str


async def resolve_target(client: TelegramClient, raw: str) -> ResolvedTarget | None:
    """Резолвит целевой чат конкретным клиентом. None если этот аккаунт не может
    его увидеть/чат не существует/это не канал-супергруппа.

    Доступ-ошибки (ChannelPrivate/ChatAdminRequired) тоже дают None: для FSM это
    означает «попробуем другой аккаунт», для worker'а резолв делается иначе
    (get_input_entity + InviteToChannelRequest сам бросит account-scoped ошибку).
    """
    norm = normalize_target_input(raw)
    if norm is None:
        return None
    try:
        entity = await client.get_entity(norm)
    except (
        UsernameInvalidError,
        UsernameNotOccupiedError,
        ChannelPrivateError,
        ChatAdminRequiredError,
        ValueError,
        TypeError,
    ):
        return None

    if not isinstance(entity, Channel):
        # Только супергруппы/каналы (InviteToChannelRequest). Обычные chat — вне scope.
        return None

    return ResolvedTarget(
        chat_id=utils.get_peer_id(entity),
        title=getattr(entity, "title", "") or "",
    )


async def check_invite_permission(client: TelegramClient, raw: str) -> bool:
    """True если ЭТОТ аккаунт состоит в чате и имеет право приглашать (invite_users)."""
    norm = normalize_target_input(raw)
    if norm is None:
        return False
    try:
        perms = await client.get_permissions(norm, "me")
    except Exception:
        # Не участник / нет доступа / любой сбой → не может инвайтить.
        return False
    return bool(getattr(perms, "invite_users", False))


# ---------------------------------------------------------------------------
# Кэш членства (in-memory, §5.2).
# ---------------------------------------------------------------------------


class ParticipantsCache:
    def __init__(self) -> None:
        self._members: dict[int, set[int]] = {}
        self._loaded: set[int] = set()
        self._locks: dict[int, asyncio.Lock] = {}
        self._global_lock = asyncio.Lock()

    async def _chat_lock(self, chat_id: int) -> asyncio.Lock:
        async with self._global_lock:
            lock = self._locks.get(chat_id)
            if lock is None:
                lock = asyncio.Lock()
                self._locks[chat_id] = lock
            return lock

    async def ensure_loaded(
        self,
        client: TelegramClient,
        *,
        chat_id: int,
        target_input,
        cap: int = DEFAULT_PARTICIPANTS_CAP,
    ) -> None:
        """Идемпотентный префетч участников чата в set[user_id].

        При FloodWait уже полученные id остаются в кэше, но chat_id НЕ помечается
        загруженным — повторим в следующий раз.
        """
        if chat_id in self._loaded:
            return
        lock = await self._chat_lock(chat_id)
        async with lock:
            if chat_id in self._loaded:
                return
            members = self._members.setdefault(chat_id, set())
            try:
                async for user in client.iter_participants(target_input, limit=cap):
                    uid = getattr(user, "id", None)
                    if uid is not None:
                        members.add(uid)
            except FloodWaitError as e:
                logger.warning(
                    "participants prefetch FloodWait {}s for chat_id={} "
                    "(partial {} cached)",
                    e.seconds, chat_id, len(members),
                )
                return
            except Exception:
                logger.exception(
                    "participants prefetch failed for chat_id={}", chat_id
                )
                return
            self._loaded.add(chat_id)
            logger.info(
                "participants cache loaded: chat_id={} members={}",
                chat_id, len(members),
            )

    def is_member(self, chat_id: int, user_id: int) -> bool:
        return user_id in self._members.get(chat_id, frozenset())

    def add_member(self, chat_id: int, user_id: int) -> None:
        self._members.setdefault(chat_id, set()).add(user_id)


participants_cache = ParticipantsCache()


# ---------------------------------------------------------------------------
# Выполнение инвайта + разбор missing_invitees.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class InviteOutcome:
    ok: bool
    result_code: ResultCode | None = None


def parse_missing_invitees(result, user_id: int) -> ResultCode | None:
    """Чистая функция. Возвращает ResultCode если юзер попал в missing_invitees
    (Telegram молча не добавил его — приватность/премиум), иначе None (успех).

    `result` — ответ InviteToChannelRequest: либо Updates (старый layer), либо
    messages.InvitedUsers (новый) с полем `missing_invitees`.
    """
    missing = getattr(result, "missing_invitees", None)
    if not missing:
        return None
    for mi in missing:
        if getattr(mi, "user_id", None) == user_id:
            # Нет специального кода под «premium required» — трактуем как privacy.
            return ResultCode.privacy_restricted
    return None


async def do_invite(
    client: TelegramClient, *, target_input, user_input, user_id: int
) -> InviteOutcome:
    """Один InviteToChannelRequest. Исключения (FloodWait/PeerFlood/account-scoped/
    skip/unknown) пробрасываются наружу — их обрабатывает worker (как в message-пути).

    Возвращает InviteOutcome только для пути «без исключения»:
      • ok=True — юзер добавлен;
      • ok=False + result_code — юзер в missing_invitees (skip).
    """
    result = await client(
        InviteToChannelRequest(channel=target_input, users=[user_input])
    )
    rc = parse_missing_invitees(result, user_id)
    if rc is not None:
        return InviteOutcome(ok=False, result_code=rc)
    return InviteOutcome(ok=True)


# ---------------------------------------------------------------------------
# Реестр непригодности аккаунтов для инвайт-кампаний (§5.3).
# ---------------------------------------------------------------------------

_ineligible: dict[int, set[int]] = {}  # campaign_id -> set[account_id]


def mark_ineligible(campaign_id: int, account_id: int) -> None:
    _ineligible.setdefault(campaign_id, set()).add(account_id)


def ineligible_count(campaign_id: int) -> int:
    return len(_ineligible.get(campaign_id, ()))


def all_ineligible(campaign_id: int, total_accounts: int) -> bool:
    """True если непригодны ВСЕ рабочие аккаунты (повод паузить кампанию)."""
    if total_accounts <= 0:
        return False
    return len(_ineligible.get(campaign_id, ())) >= total_accounts


def reset_campaign(campaign_id: int) -> None:
    _ineligible.pop(campaign_id, None)
