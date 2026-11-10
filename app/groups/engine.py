"""Групповая линия воркера: вступление в группы и посты. ARCHITECTURE.md §22.5, §22.6.

Работает на клиенте воркера аккаунта (одна .session не подключается дважды). Один вызов
`step()` = не больше одного «смыслового» действия в Telegram (резолв+вступление или
пост). FloodWait/PeerFlood на посте и 401 пробрасываются воркеру — это уровень
аккаунта (§6.3, §6.4, §16). FloodWait на резолве/вступлении блокирует только
вступления на свой срок.
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from loguru import logger
from telethon import TelegramClient
from telethon.errors import FloodWaitError, PeerFloodError
from telethon.tl.functions.channels import JoinChannelRequest
from telethon.tl.functions.contacts import ResolveUsernameRequest
from telethon.tl.functions.messages import CheckChatInviteRequest, ImportChatInviteRequest
from telethon.tl.types import (
    Channel,
    Chat,
    ChatInvite,
    ChatInviteAlready,
    ChatInvitePeek,
    InputPeerChannel,
    InputPeerChat,
)

from app.campaigns.template_engine import render_template
from app.db.models import Account, GroupTarget
from app.db.repositories import group_campaigns as repo
from app.db.repositories import logs as logs_repo
from app.db.repositories.settings import settings_cache
from app.db.session import session_scope
from app.groups import notify
from app.groups.errors import classify_join_error, classify_post_error, entity_kind_problem
from app.groups.planner import (
    RETRY_AFTER_ERROR_MIN,
    SIBLING_DEFER_MIN,
    SLOWMODE_EXTRA_SEC,
    effective_period,
    first_post_at,
    gap_seconds,
    in_window,
    next_post_at,
    random_delay,
)
from app.telegram.errors import SESSION_DEAD_ERRORS
from app.utils.time import seconds_until_midnight_utc

DIALOGS_TTL_SEC = 6 * 3600
BAN_CHECK_EVERY_SEC = 3600
# Пауза групповой линии после резолва без вступления, если у рассылки её не взять.
DEFAULT_GAP_SEC = (120, 600)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class PeerInfo:
    chat_id: int
    access_hash: int | None
    peer_type: str  # channel | chat
    title: str | None


def peer_from_entity(entity) -> PeerInfo | None:
    if isinstance(entity, Channel):
        return PeerInfo(entity.id, entity.access_hash, "channel", entity.title)
    if isinstance(entity, Chat):
        return PeerInfo(entity.id, None, "chat", entity.title)
    return None


def input_peer(target: GroupTarget):
    if target.peer_type == "chat":
        return InputPeerChat(int(target.chat_id))
    return InputPeerChannel(int(target.chat_id), int(target.access_hash or 0))


def entity_keys(entity) -> list[str]:
    """Ключи ref_key (§22.3), по которым цель узнаёт группу среди диалогов аккаунта."""
    keys: list[str] = []
    if isinstance(entity, Channel):
        keys.append(f"id:{-1000000000000 - entity.id}")  # -100… как в t.me/c и боте
        keys.append(f"id:{entity.id}")
        names = [entity.username] if entity.username else []
        for u in getattr(entity, "usernames", None) or []:
            if getattr(u, "active", False) and u.username:
                names.append(u.username)
        keys.extend(f"u:{n.casefold()}" for n in names)
    elif isinstance(entity, Chat):
        keys.append(f"id:{-entity.id}")
        keys.append(f"id:{entity.id}")
    return keys


def _setting(key: str, default: int) -> int:
    return settings_cache.get_int(key, default)


class GroupLine:
    """Групповая линия одного аккаунта (§22.5)."""

    def __init__(self, account_id: int, client: TelegramClient) -> None:
        self.account_id = account_id
        self.client = client
        self._dialogs: dict[str, PeerInfo] | None = None
        self._dialogs_at = 0.0
        self._next_join_at: float | None = None
        self._joins_blocked_until = 0.0
        self._last_ban_check = 0.0
        # new-цели, уже сверенные с известными peer и диалогами (не гоняем повторно
        # на каждом тике). Сбрасывается при перечитывании диалогов.
        self._checked_new: set[int] = set()

    # ------------------ Публичное ------------------

    async def step(self, account: Account) -> float | None:
        """Одно действие групповой линии. Возвращает паузу (сек) до следующего шага,
        если был запрос к Telegram; None — делать нечего."""
        now = _utcnow()
        async with session_scope() as session:
            new = await repo.list_new_targets(session, self.account_id)
            pending = await repo.list_pending_due(session, self.account_id, now=now)
        if new:
            new = await self._activate_known(new, now)

        gap = await self._maybe_join(account, new, pending, now)
        if gap is not None:
            return gap
        return await self._maybe_post(account, now)

    # ------------------ Сверка без запросов к группе ------------------

    async def _activate_known(self, targets: list[GroupTarget], now: datetime) -> list[GroupTarget]:
        """Цели, чей peer уже известен (другая рассылка) или которые есть среди диалогов
        аккаунта, сразу становятся active. Возвращает оставшиеся (им нужен резолв)."""
        if self._dialogs is not None and time.monotonic() - self._dialogs_at >= DIALOGS_TTL_SEC:
            self._checked_new.clear()  # диалоги перечитаем — сверим всё заново
        fresh = [t for t in targets if t.id not in self._checked_new]
        checked = [t for t in targets if t.id in self._checked_new]
        if not fresh:
            return checked
        remaining: list[GroupTarget] = []
        campaigns: dict[int, object] = {}
        async with session_scope() as session:
            for t in fresh:
                known = await repo.find_known_peer(session, self.account_id, t.ref_key)
                if known is None:
                    remaining.append(t)
                    continue
                c = campaigns.get(t.campaign_id) or await repo.get_campaign(session, t.campaign_id)
                campaigns[t.campaign_id] = c
                period = effective_period(c.period_min, c.period_max, (t.period_min, t.period_max))
                await repo.activate_target(
                    session, t.id,
                    chat_id=known.chat_id, access_hash=known.access_hash,
                    peer_type=known.peer_type or "channel", title=known.title,
                    joined_by_us=False,
                    next_at=first_post_at(now, joined_by_us=False, period=period),
                    slowmode_sec=known.slowmode_sec,
                )
        if not remaining:
            return checked

        dialogs = await self._load_dialogs()
        if dialogs is None:
            return checked + remaining  # диалоги не прочитались — сверим на следующем тике
        still: list[GroupTarget] = []
        async with session_scope() as session:
            for t in remaining:
                info = dialogs.get(t.ref_key)
                if info is None:
                    if t.ref_kind == "id":
                        await self._to_problem(
                            session, t, "manual",
                            "аккаунт не состоит в группе с таким id — укажите @username "
                            "или ссылку-приглашение",
                        )
                    else:
                        still.append(t)
                    continue
                c = campaigns.get(t.campaign_id) or await repo.get_campaign(session, t.campaign_id)
                campaigns[t.campaign_id] = c
                period = effective_period(c.period_min, c.period_max, (t.period_min, t.period_max))
                await repo.activate_target(
                    session, t.id,
                    chat_id=info.chat_id, access_hash=info.access_hash,
                    peer_type=info.peer_type, title=info.title, joined_by_us=False,
                    next_at=first_post_at(now, joined_by_us=False, period=period),
                )
        self._checked_new.update(t.id for t in still)
        return checked + still

    async def _load_dialogs(self) -> dict[str, PeerInfo] | None:
        mono = time.monotonic()
        if self._dialogs is not None and mono - self._dialogs_at < DIALOGS_TTL_SEC:
            return self._dialogs
        if mono < self._joins_blocked_until:
            return self._dialogs
        found: dict[str, PeerInfo] = {}
        try:
            async for d in self.client.iter_dialogs():
                ent = d.entity
                if entity_kind_problem(ent) is not None:
                    continue
                info = peer_from_entity(ent)
                if info is None:
                    continue
                for k in entity_keys(ent):
                    found[k] = info
        except FloodWaitError as e:
            self._block_joins(e.seconds, "GetDialogs")
            return self._dialogs
        except SESSION_DEAD_ERRORS:
            raise
        except Exception:
            logger.exception("group[{}] iter_dialogs failed", self.account_id)
            return self._dialogs
        self._dialogs = found
        self._dialogs_at = mono
        return found

    # ------------------ Вступление ------------------

    def _join_slot_open(self, account: Account) -> bool:
        mono = time.monotonic()
        if self._next_join_at is None:
            # После рестарта уважаем паузу от последнего вступления из БД.
            gap_min = _setting("group_join_gap_min_sec", 900)
            wait = 0.0
            if account.last_group_join_at is not None:
                elapsed = (_utcnow() - account.last_group_join_at).total_seconds()
                wait = max(0.0, gap_min - elapsed)
            self._next_join_at = mono + wait
        return mono >= self._next_join_at and mono >= self._joins_blocked_until

    def _joins_left(self, account: Account) -> bool:
        return (account.daily_group_joins or 0) < _setting("group_join_daily_limit", 10)

    def _block_joins(self, seconds: float, request: str) -> None:
        self._joins_blocked_until = time.monotonic() + max(60.0, float(seconds))
        logger.warning(
            "group[{}] FloodWait {}s на {} — вступления на паузе", self.account_id, seconds, request
        )

    async def _maybe_join(
        self,
        account: Account,
        new: list[GroupTarget],
        pending: list[GroupTarget],
        now: datetime,
    ) -> float | None:
        # -100… резолвится только через диалоги (_activate_known), не вступлением.
        joinable = [t for t in new if t.ref_kind in ("username", "invite")]
        if not joinable and not pending:
            return None
        if not self._join_slot_open(account):
            return None
        if pending:
            return await self._recheck_pending(pending[0], now)
        if not joinable or not self._joins_left(account):
            return None
        return await self._join(joinable[0], now)

    async def _resolve(self, target: GroupTarget):
        """Свежая сущность группы и признак «аккаунт уже участник» (без кэша Telethon)."""
        if target.ref_kind == "username":
            res = await self.client(ResolveUsernameRequest(target.ref_value))
            entity = res.chats[0] if res.chats else (res.users[0] if res.users else None)
            if entity is None:
                raise ValueError("не найдено")
            member = isinstance(entity, Channel) and not getattr(entity, "left", True)
            return entity, member, None
        info = await self.client(CheckChatInviteRequest(target.ref_value))
        if isinstance(info, ChatInviteAlready):
            return info.chat, True, None
        if isinstance(info, ChatInvitePeek):
            return info.chat, False, None
        if isinstance(info, ChatInvite):
            return None, False, info
        return None, False, None

    async def _join(self, target: GroupTarget, now: datetime) -> float:
        gap = await self._campaign_gap(target)
        try:
            entity, member, invite = await self._resolve(target)
        except FloodWaitError as e:
            self._block_joins(e.seconds, "Resolve/CheckChatInvite")
            return gap
        except SESSION_DEAD_ERRORS:
            raise
        except Exception as e:  # noqa: BLE001
            await self._apply_join_error(target, e, now)
            return gap

        if invite is not None and getattr(invite, "broadcast", False) and not getattr(
            invite, "megagroup", False
        ):
            async with session_scope() as session:
                await self._to_problem(
                    session, target, "excluded", "это канал, а не группа — писать в него нельзя",
                    title=invite.title,
                )
            return gap
        if entity is not None:
            problem = entity_kind_problem(entity)
            if problem:
                async with session_scope() as session:
                    await self._to_problem(
                        session, target, "excluded", problem, title=getattr(entity, "title", None)
                    )
                return gap
            if member:
                await self._activate(target, entity, joined_by_us=False, now=now)
                return gap

        # Нужно вступать.
        try:
            if target.ref_kind == "username":
                upd = await self.client(JoinChannelRequest(entity))
            else:
                upd = await self.client(ImportChatInviteRequest(target.ref_value))
        except FloodWaitError as e:
            self._block_joins(e.seconds, "Join")
            return gap
        except SESSION_DEAD_ERRORS:
            raise
        except Exception as e:  # noqa: BLE001
            await self._apply_join_error(target, e, now, entity=entity)
            return gap

        self._next_join_at = time.monotonic() + random.uniform(
            _setting("group_join_gap_min_sec", 900), _setting("group_join_gap_max_sec", 2400)
        )
        joined = entity
        chats = getattr(upd, "chats", None) or []
        if chats:
            joined = chats[0]
        async with session_scope() as session:
            await repo.inc_group_join(session, self.account_id)
        if joined is None:
            # Вступили, но чат не вернулся — перепроверим как заявку.
            await self._set_pending(target, now, joined_at=now)
            return gap
        await self._activate(target, joined, joined_by_us=True, now=now)
        if self._dialogs is not None:
            info = peer_from_entity(joined)
            if info is not None:
                for k in entity_keys(joined):
                    self._dialogs[k] = info
        return gap

    async def _recheck_pending(self, target: GroupTarget, now: datetime) -> float:
        gap = await self._campaign_gap(target)
        max_hours = _setting("group_pending_max_hours", 72)
        try:
            entity, member, _invite = await self._resolve(target)
        except FloodWaitError as e:
            self._block_joins(e.seconds, "Resolve/CheckChatInvite")
            return gap
        except SESSION_DEAD_ERRORS:
            raise
        except Exception as e:  # noqa: BLE001
            await self._apply_join_error(target, e, now)
            return gap
        if member and entity is not None:
            await self._activate(target, entity, joined_by_us=True, now=now)
            return gap
        started = target.joined_at or target.updated_at or now
        if (now - started).total_seconds() > max_hours * 3600:
            async with session_scope() as session:
                await self._to_problem(
                    session, target, "manual",
                    f"заявку на вступление не одобрили за {max_hours} ч",
                )
            return gap
        await self._set_pending(target, now, joined_at=None)
        return gap

    async def _apply_join_error(
        self, target: GroupTarget, exc: Exception, now: datetime, *, entity=None
    ) -> None:
        out = classify_join_error(exc)
        if out.action == "member":
            if entity is not None:
                await self._activate(target, entity, joined_by_us=False, now=now)
            else:
                await self._set_pending(target, now, joined_at=None, soon=True)
            return
        if out.action == "pending":
            async with session_scope() as session:
                await repo.inc_group_join(session, self.account_id)
            self._next_join_at = time.monotonic() + random.uniform(
                _setting("group_join_gap_min_sec", 900), _setting("group_join_gap_max_sec", 2400)
            )
            await self._set_pending(target, now, joined_at=now)
            await self._log("info", "group_join_request", target, "Заявка на вступление отправлена")
            return
        async with session_scope() as session:
            if out.action == "too_many":
                self._joins_blocked_until = time.monotonic() + seconds_until_midnight_utc()
                await self._to_problem(session, target, "manual", out.reason)
            elif out.action == "exclude":
                await self._to_problem(session, target, "excluded", out.reason)
            else:  # retry
                streak = await repo.bump_fail(
                    session, target.id, reason=out.reason,
                    next_at=random_delay(now, RETRY_AFTER_ERROR_MIN),
                )
                if streak >= _setting("group_fail_streak_limit", 3):
                    await self._to_problem(
                        session, target, "excluded", f"не удаётся вступить: {out.reason}"
                    )

    async def _set_pending(
        self, target: GroupTarget, now: datetime, *, joined_at: datetime | None, soon: bool = False
    ) -> None:
        hours = _setting("group_pending_recheck_hours", 4)
        nxt = now + (timedelta(minutes=10) if soon else timedelta(hours=hours * random.uniform(1.0, 1.5)))
        async with session_scope() as session:
            await repo.set_target_state(
                session, target.id, "pending",
                reason="заявка на вступление ждёт одобрения", next_at=nxt,
            )
            if joined_at is not None:
                # joined_at у pending = момент подачи заявки (для таймаута §22.5).
                await repo.mark_join_requested(session, target.id, at=joined_at)

    async def _activate(self, target: GroupTarget, entity, *, joined_by_us: bool, now: datetime) -> None:
        info = peer_from_entity(entity)
        if info is None:
            async with session_scope() as session:
                await self._to_problem(session, target, "excluded", "это не группа")
            return
        async with session_scope() as session:
            c = await repo.get_campaign(session, target.campaign_id)
            period = effective_period(
                c.period_min, c.period_max, (target.period_min, target.period_max)
            )
            await repo.activate_target(
                session, target.id,
                chat_id=info.chat_id, access_hash=info.access_hash, peer_type=info.peer_type,
                title=info.title, joined_by_us=joined_by_us,
                next_at=first_post_at(now, joined_by_us=joined_by_us, period=period),
            )
        if joined_by_us:
            await self._log("info", "group_joined", target, f"Вступил в {info.title or target.ref_value}")

    # ------------------ Пост ------------------

    async def _maybe_post(self, account: Account, now: datetime) -> float | None:
        limit = _setting("group_posts_daily_limit", 0)
        if limit > 0 and (account.daily_group_posts or 0) >= limit:
            return None
        async with session_scope() as session:
            due = await repo.list_due_active(session, self.account_id, now=now)
        tz = settings_cache.get_str("quiet_hours_timezone", "Europe/Minsk")
        for d in due:
            if in_window(now, d.campaign.window_start, d.campaign.window_end, tz):
                return await self._post(d, now)
        return None

    async def _post(self, d: repo.DueTarget, now: datetime) -> float:
        t, c = d.target, d.campaign
        gap = gap_seconds(c.gap_min_sec, c.gap_max_sec)
        text = render_template(d.template_body, {})
        try:
            msg = await self.client.send_message(input_peer(t), text)
        except (FloodWaitError, PeerFloodError):
            raise
        except SESSION_DEAD_ERRORS:
            raise
        except Exception as e:  # noqa: BLE001
            await self._apply_post_error(t, e, now)
            return gap

        posted = _utcnow()
        period = effective_period(c.period_min, c.period_max, (t.period_min, t.period_max))
        async with session_scope() as session:
            await repo.mark_posted(
                session, t, now=posted,
                next_at=next_post_at(posted, period, slowmode_sec=t.slowmode_sec),
                message_id=getattr(msg, "id", None),
            )
            await repo.inc_group_post(session, self.account_id)
            if c.distribution == "all":
                await repo.defer_siblings(
                    session, t, not_before=random_delay(posted, SIBLING_DEFER_MIN)
                )
            await logs_repo.log_event(
                session,
                level="info",
                event_type="group_post_sent",
                account_id=self.account_id,
                message=f"Пост в {t.title or t.ref_value} (рассылка «{c.name}»)",
                payload={"group_campaign_id": c.id, "target_id": t.id, "ref": t.ref_key},
            )
        return gap

    async def _apply_post_error(self, t: GroupTarget, exc: Exception, now: datetime) -> None:
        out = classify_post_error(exc, posts_count=t.posts_count or 0)
        async with session_scope() as session:
            if out.action == "slowmode":
                wait = max(out.wait_sec, 1)
                await repo.reschedule(
                    session, t.id,
                    next_at=now + timedelta(seconds=wait + random.uniform(*SLOWMODE_EXTRA_SEC)),
                    slowmode_sec=max(t.slowmode_sec or 0, wait),
                )
                return
            if out.action in ("manual", "exclude"):
                await self._to_problem(
                    session, t, "manual" if out.action == "manual" else "excluded", out.reason
                )
                return
            streak = await repo.bump_fail(
                session, t.id, reason=out.reason, next_at=random_delay(now, RETRY_AFTER_ERROR_MIN)
            )
            if streak >= _setting("group_fail_streak_limit", 3):
                await self._to_problem(session, t, "excluded", f"{out.reason} ({streak} раза подряд)")
        if out.action == "ban_check":
            await self._maybe_ban_check()

    async def _maybe_ban_check(self) -> None:
        """USER_BANNED_IN_CHANNEL бывает и при спам-ограничении всего аккаунта (§22.6):
        внеочередной SpamBot-чек, не чаще раза в час. Ограничение → аккаунт встанет."""
        mono = time.monotonic()
        if mono - self._last_ban_check < BAN_CHECK_EVERY_SEC:
            return
        self._last_ban_check = mono
        try:
            from app.telegram import spam_checker

            await spam_checker.spam_check(account_id=self.account_id, client=self.client)
        except Exception:
            logger.debug("group[{}] ban spam_check failed", self.account_id)

    # ------------------ Служебное ------------------

    async def _campaign_gap(self, target: GroupTarget) -> float:
        async with session_scope() as session:
            c = await repo.get_campaign(session, target.campaign_id)
        if c is None:
            return gap_seconds(*DEFAULT_GAP_SEC)
        return gap_seconds(c.gap_min_sec, c.gap_max_sec)

    async def _to_problem(
        self, session, target: GroupTarget, state: str, reason: str, *, title: str | None = None
    ) -> None:
        await repo.set_target_state(session, target.id, state, reason=reason, title=title)
        c = await repo.get_campaign(session, target.campaign_id)
        name = c.name if c else f"#{target.campaign_id}"
        label = title or target.title or _label(target)
        verb = "нужна ручная проверка" if state == "manual" else "группа исключена"
        await logs_repo.log_event(
            session,
            level="warning",
            event_type=f"group_target_{state}",
            account_id=self.account_id,
            message=f"{label}: {verb} — {reason}",
            payload={"group_campaign_id": target.campaign_id, "target_id": target.id},
        )
        icon = "🖐" if state == "manual" else "🚫"
        notify.queue(f"{icon} «{name}», {label} (аккаунт #{self.account_id}): {verb} — {reason}")

    async def _log(self, level: str, event: str, target: GroupTarget, message: str) -> None:
        try:
            async with session_scope() as session:
                await logs_repo.log_event(
                    session, level=level, event_type=event, account_id=self.account_id,
                    message=message,
                    payload={"group_campaign_id": target.campaign_id, "target_id": target.id},
                )
        except Exception:
            logger.debug("group[{}] log failed", self.account_id)


def _label(target: GroupTarget) -> str:
    if target.ref_kind == "username":
        return f"@{target.ref_value}"
    if target.ref_kind == "invite":
        return f"t.me/+{target.ref_value}"
    return str(target.ref_value)
