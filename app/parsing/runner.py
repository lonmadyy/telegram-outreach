"""Раннер парсинга групп через клиент воркера. ARCHITECTURE.md §20.

Один аккаунт — одно задание. На время задания воркер аккаунта на hold (не берёт
задачи рассылки), клиент Telethon — тот же, что у воркера (одну сессию нельзя
подключить дважды). Результаты пишутся в parse_results после каждой группы, так
что остановка/рестарт не теряют уже собранное.
"""

from __future__ import annotations

import asyncio
import random
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable

from loguru import logger
from telethon import TelegramClient
from telethon.errors import (
    AuthKeyUnregisteredError,
    ChatAdminRequiredError,
    FloodWaitError,
    InviteHashExpiredError,
    InviteHashInvalidError,
    UserAlreadyParticipantError,
    UserDeactivatedBanError,
    UsernameInvalidError,
    UsernameNotOccupiedError,
)
from telethon.tl.functions.messages import (
    CheckChatInviteRequest,
    ImportChatInviteRequest,
)
from telethon.tl.types import (
    Channel,
    ChannelParticipantsAdmins,
    Chat,
    ChatInviteAlready,
    ChatInvitePeek,
    ChatParticipantAdmin,
    ChatParticipantCreator,
    MessageService,
    User,
)

from app.db.models import Account
from app.db.repositories import accounts as accounts_repo
from app.db.repositories import parses as parses_repo
from app.db.session import session_scope
from app.parsing.collector import ParseCollector
from app.telegram.worker_pool import worker_pool
from app.utils.group_targets import GroupToken, token_label

FLOOD_WAIT_MAX = 300           # FloodWait длиннее — группу пропускаем
BETWEEN_GROUPS_SEC = (3.0, 8.0)
CHUNK_PAUSE_SEC = (0.5, 1.5)   # пауза каждые 200 участников (одна порция)
MESSAGES_WAIT_TIME = 0.7       # пауза Telethon между пачками истории по 100
BIG_GROUP_THRESHOLD = 9000     # больше — добираем поиском по буквам
PROGRESS_EVERY_SEC = 15.0
SEARCH_QUERIES = list("abcdefghijklmnopqrstuvwxyz") + list(
    "абвгдеёжзийклмнопрстуфхцчшэюя"
) + list("0123456789")

# Аккаунт, с которого можно парсить: живой, есть подключённый воркер.
PARSE_ALLOWED_STATUSES = ("warmup", "active", "pause", "spam_blocked")


@dataclass
class ParseOptions:
    members: bool = True
    senders: bool = True
    days: int | None = 30          # None → без ограничения по дате
    msg_limit: int | None = None   # N последних сообщений (взаимоисключимо с days)
    join_private: bool = False

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "ParseOptions":
        return cls(**{k: data[k] for k in cls.__dataclass_fields__ if k in data})


@dataclass
class Progress:
    job_id: int
    group_no: int
    groups_total: int
    label: str
    stage: str
    seen: int
    collected: int  # уникальных по всем пройденным группам (до отсева processed)
    note: str = ""


@dataclass
class _Entry:
    label: str
    title: str | None = None
    ok: bool = False
    members: int = 0
    senders: int = 0
    added: int = 0
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "label": self.label,
            "title": self.title,
            "ok": self.ok,
            "members": self.members,
            "senders": self.senders,
            "added": self.added,
            "note": "; ".join(self.notes),
        }


class _GroupSkip(Exception):
    """Группу пропускаем с причиной (текст — для сводки)."""


class _Abort(Exception):
    """Аккаунт больше не может работать — задание завершается ошибкой."""


ProgressCallback = Callable[[Progress], Awaitable[None]]
DoneCallback = Callable[[int, str], Awaitable[None]]  # (job_id, status)


class ParseStartError(Exception):
    """Задание нельзя запустить (текст — для пользователя)."""


class ParseRun:
    def __init__(
        self,
        *,
        job_id: int,
        account_id: int,
        client: TelegramClient,
        tokens: list[GroupToken],
        options: ParseOptions,
        exclude_user_ids: set[int],
        on_progress: ProgressCallback,
        on_done: DoneCallback,
    ) -> None:
        self.job_id = job_id
        self.account_id = account_id
        self.client = client
        self.tokens = tokens
        self.options = options
        self.on_progress = on_progress
        self.on_done = on_done
        self.stop_event = asyncio.Event()
        self.collector = ParseCollector(exclude_user_ids)
        self.report: list[dict] = []
        self._collected: set[str] = set()
        self._dialogs: list[tuple[str, object]] | None = None
        self._last_progress = 0.0
        self._group_no = 0
        self._label = ""
        self.task: asyncio.Task | None = None

    # ------------------ Запуск/итог ------------------

    async def run(self) -> None:
        status = parses_repo.STATUS_DONE
        error: str | None = None
        try:
            for no, token in enumerate(self.tokens, start=1):
                if self.stop_event.is_set():
                    break
                self._group_no, self._label = no, token_label(token)
                entry = _Entry(label=self._label)
                usernames: set[str] = set()
                try:
                    usernames = await self._parse_group(token, entry)
                    entry.ok = True
                except _GroupSkip as e:
                    entry.notes.append(str(e))
                    usernames = self.collector.group_usernames()
                except _Abort:
                    raise
                except (AuthKeyUnregisteredError, UserDeactivatedBanError) as e:
                    raise _Abort(f"сессия аккаунта недействительна ({type(e).__name__})")
                except Exception as e:  # noqa: BLE001 — одна группа не валит задание
                    logger.warning("parse[{}] group {} failed: {!r}", self.job_id, no, e)
                    self._ensure_connected()
                    entry.notes.append(f"ошибка: {type(e).__name__}")
                    usernames = self.collector.group_usernames()
                entry.added = len(usernames - self._collected)
                self._collected |= usernames
                self.report.append(entry.as_dict())
                async with session_scope() as session:
                    await parses_repo.save_group(
                        session,
                        job_id=self.job_id,
                        group_idx=no,
                        usernames=usernames,
                        report=self.report,
                        dropped=dict(self.collector.dropped),
                    )
                await self._progress("готово", force=True)
                if no < len(self.tokens):
                    await self._pause(*BETWEEN_GROUPS_SEC)
            if self.stop_event.is_set():
                status = parses_repo.STATUS_STOPPED
        except _Abort as e:
            status, error = parses_repo.STATUS_FAILED, str(e)
        except Exception as e:  # noqa: BLE001 — задание не должно ронять процесс
            logger.exception("parse[{}] failed", self.job_id)
            status, error = parses_repo.STATUS_FAILED, f"{type(e).__name__}: {e}"
        finally:
            worker_pool.set_hold(self.account_id, False)
            _runs.pop(self.job_id, None)
            try:
                async with session_scope() as session:
                    await parses_repo.finish_job(
                        session,
                        job_id=self.job_id,
                        status=status,
                        report=self.report,
                        dropped=dict(self.collector.dropped),
                        error=error,
                    )
            except Exception:
                logger.exception("parse[{}] finish_job failed", self.job_id)
        logger.info("parse[{}] {} ({} usernames)", self.job_id, status, len(self._collected))
        try:
            await self.on_done(self.job_id, status)
        except Exception:
            logger.exception("parse[{}] on_done failed", self.job_id)

    # ------------------ Группа ------------------

    async def _parse_group(self, token: GroupToken, entry: _Entry) -> set[str]:
        self.collector.begin_group(set())  # пропуск на резолве не должен унести прошлую группу
        self._ensure_connected()
        entity = await self._call(lambda: self._resolve(token))
        if isinstance(entity, Channel) and not entity.megagroup:
            raise _GroupSkip("это канал, а не группа: участников и авторов Telegram не отдаёт")
        if not isinstance(entity, (Channel, Chat)):
            raise _GroupSkip("это не группа")
        entry.title = getattr(entity, "title", None) or self._label
        self._label = entry.title

        admin_ids = await self._call(lambda: self._admin_ids(entity))
        self.collector.begin_group(admin_ids)

        if self.options.members:
            try:
                entry.members = await self._call(
                    lambda: self._collect_members(entity, entry)
                )
            except _GroupSkip as e:
                entry.notes.append(str(e))
            except (_Abort, AuthKeyUnregisteredError, UserDeactivatedBanError):
                raise
            except Exception as e:  # noqa: BLE001 — этап не валит группу
                self._ensure_connected()
                entry.notes.append(f"участники недоступны: {type(e).__name__}")
        if self.options.senders and not self.stop_event.is_set():
            try:
                entry.senders = await self._collect_senders(entity)
            except _GroupSkip as e:
                entry.notes.append(str(e))
            except (_Abort, AuthKeyUnregisteredError, UserDeactivatedBanError):
                raise
            except Exception as e:  # noqa: BLE001
                self._ensure_connected()
                entry.notes.append(f"сообщения недоступны: {type(e).__name__}")
        return self.collector.group_usernames()

    async def _resolve(self, token: GroupToken):
        kind, value = token
        try:
            if kind == "id":
                return await self.client.get_entity(value)
            if kind == "invite":
                return await self._resolve_invite(str(value))
            if kind == "username":
                try:
                    return await self.client.get_entity(value)
                except (UsernameNotOccupiedError, UsernameInvalidError, ValueError):
                    found = await self._find_dialog(str(value))
                    if found is None:
                        raise _GroupSkip("не найдено: нет такой публичной группы")
                    return found
            found = await self._find_dialog(str(value))
            if found is None:
                raise _GroupSkip(
                    "не найдено среди групп аккаунта (по названию ищем только там, где он состоит)"
                )
            return found
        except (_GroupSkip, FloodWaitError, _Abort):
            raise
        except ValueError:
            raise _GroupSkip("не найдено (аккаунт должен состоять в этой группе)")
        except (AuthKeyUnregisteredError, UserDeactivatedBanError) as e:
            raise _Abort(f"сессия аккаунта недействительна ({type(e).__name__})")
        except Exception as e:  # noqa: BLE001
            raise _GroupSkip(f"не открылась: {type(e).__name__}")

    async def _resolve_invite(self, hash_: str):
        try:
            info = await self.client(CheckChatInviteRequest(hash_))
        except (InviteHashInvalidError, InviteHashExpiredError):
            raise _GroupSkip("ссылка-приглашение недействительна или истекла")
        if isinstance(info, (ChatInviteAlready, ChatInvitePeek)):
            return info.chat
        if not self.options.join_private:
            raise _GroupSkip(
                "закрытая группа, аккаунт не участник (включите «вступать по ссылке»)"
            )
        try:
            upd = await self.client(ImportChatInviteRequest(hash_))
        except UserAlreadyParticipantError:
            info = await self.client(CheckChatInviteRequest(hash_))
            return getattr(info, "chat", None)
        except FloodWaitError:
            raise
        except Exception as e:  # noqa: BLE001 — в т.ч. заявка на вступление
            raise _GroupSkip(f"не удалось вступить: {type(e).__name__}")
        chats = getattr(upd, "chats", None) or []
        if not chats:
            raise _GroupSkip("вступление не вернуло чат")
        return chats[0]

    async def _find_dialog(self, name: str):
        if self._dialogs is None:
            self._dialogs = []
            async for d in self.client.iter_dialogs():
                if d.is_group and (d.title or "").strip():
                    self._dialogs.append((d.title.strip(), d.entity))
        key = name.casefold()
        for title, ent in self._dialogs:
            if title.casefold() == key:
                return ent
        matches = [ent for title, ent in self._dialogs if key in title.casefold()]
        return matches[0] if matches else None

    async def _admin_ids(self, entity) -> set[int]:
        ids: set[int] = set()
        try:
            if isinstance(entity, Channel):
                async for u in self.client.iter_participants(
                    entity, filter=ChannelParticipantsAdmins()
                ):
                    ids.add(u.id)
            else:
                async for u in self.client.iter_participants(entity):
                    p = getattr(u, "participant", None)
                    if isinstance(p, (ChatParticipantAdmin, ChatParticipantCreator)):
                        ids.add(u.id)
        except FloodWaitError:
            raise
        except Exception:  # noqa: BLE001 — нет доступа к списку админов: не страшно
            pass
        return ids

    async def _collect_members(self, entity, entry: _Entry) -> int:
        seen: set[int] = set()
        try:
            total = (await self.client.get_participants(entity, limit=0)).total or 0
        except ChatAdminRequiredError:
            raise _GroupSkip("список участников скрыт админом, собраны только писавшие")
        await self._iter_members(entity, "", seen)
        if total > BIG_GROUP_THRESHOLD:
            for q in SEARCH_QUERIES:
                if self.stop_event.is_set():
                    break
                await self._progress(f"участники, поиск «{q}»", seen=len(seen))
                await self._iter_members(entity, q, seen)
        if total > 50 and len(seen) < total * 0.5:
            entry.notes.append(
                f"Telegram отдал {len(seen)} участников из {total} "
                "(часть списка скрыта, остальных добираем по сообщениям)"
            )
        return len(seen)

    async def _iter_members(self, entity, search: str, seen: set[int]) -> None:
        n = 0
        try:
            async for u in self.client.iter_participants(entity, search=search):
                if self.stop_event.is_set():
                    return
                n += 1
                seen.add(u.id)
                if isinstance(u, User):
                    self.collector.add_user(u)
                if n % 200 == 0:
                    await self._progress("участники", seen=len(seen))
                    await self._pause(*CHUNK_PAUSE_SEC)
        except ChatAdminRequiredError:
            if not search:
                raise _GroupSkip("список участников скрыт админом, собраны только писавшие")

    async def _collect_senders(self, entity) -> int:
        cutoff = None
        if self.options.days and not self.options.msg_limit:
            cutoff = datetime.now(timezone.utc) - timedelta(days=self.options.days)
        limit = self.options.msg_limit
        authors: set[int] = set()
        scanned = 0
        offset_id = 0
        for _attempt in range(5):
            try:
                async for msg in self.client.iter_messages(
                    entity,
                    limit=None if limit is None else max(limit - scanned, 0),
                    offset_id=offset_id,
                    wait_time=MESSAGES_WAIT_TIME,
                ):
                    if self.stop_event.is_set():
                        return len(authors)
                    offset_id = msg.id
                    if cutoff is not None and msg.date < cutoff:
                        return len(authors)
                    scanned += 1
                    if not isinstance(msg, MessageService):
                        sender = msg.sender
                        if isinstance(sender, User):
                            authors.add(sender.id)
                            self.collector.add_user(sender)
                    if scanned % 500 == 0:
                        await self._progress(
                            f"сообщения ({scanned})", seen=len(authors)
                        )
                return len(authors)
            except FloodWaitError as e:
                # Продолжаем с того же места (offset_id), а не с начала истории.
                await self._flood_or_skip(e)
            except ChatAdminRequiredError:
                raise _GroupSkip("история сообщений недоступна")
        raise _GroupSkip("слишком много FloodWait при чтении истории")

    # ------------------ Служебное ------------------

    async def _call(self, factory: Callable[[], Awaitable]):
        """Выполняет шаг; FloodWait ≤ FLOOD_WAIT_MAX — ждём и повторяем шаг один раз."""
        for _attempt in range(2):
            try:
                return await factory()
            except FloodWaitError as e:
                await self._flood_or_skip(e)
        raise _GroupSkip("Telegram повторно просит подождать, шаг пропущен")

    async def _flood_or_skip(self, e: FloodWaitError) -> None:
        if e.seconds > FLOOD_WAIT_MAX:
            raise _GroupSkip(f"Telegram просит подождать {e.seconds} с, группа пропущена")
        await self._progress(f"FloodWait, ждём {e.seconds} с", force=True)
        await self._pause(e.seconds + 1, e.seconds + 2)

    def _ensure_connected(self) -> None:
        if not self.client.is_connected():
            raise _Abort("аккаунт отключился от Telegram")

    async def _pause(self, lo: float, hi: float) -> None:
        try:
            await asyncio.wait_for(self.stop_event.wait(), timeout=random.uniform(lo, hi))
        except asyncio.TimeoutError:
            pass

    async def _progress(self, stage: str, *, seen: int = 0, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self._last_progress < PROGRESS_EVERY_SEC:
            return
        self._last_progress = now
        snapshot = Progress(
            job_id=self.job_id,
            group_no=self._group_no,
            groups_total=len(self.tokens),
            label=self._label,
            stage=stage,
            seen=seen,
            collected=len(self._collected | self.collector.group_usernames()),
        )
        try:
            await self.on_progress(snapshot)
        except Exception:
            logger.debug("parse[{}] progress callback failed", self.job_id)


# ------------------ Реестр заданий ------------------

_runs: dict[int, ParseRun] = {}


def busy_account_ids() -> set[int]:
    return {r.account_id for r in _runs.values()}


def is_running(job_id: int) -> bool:
    return job_id in _runs


def stop_parse(job_id: int) -> bool:
    run = _runs.get(job_id)
    if run is None:
        return False
    run.stop_event.set()
    return True


def parse_candidates(accounts: list[Account]) -> list[Account]:
    """Аккаунты, которыми можно парсить: живой статус и подключённый воркер."""
    return [
        a
        for a in accounts
        if a.status.value in PARSE_ALLOWED_STATUSES
        and worker_pool.get_client(a.id) is not None
    ]


async def start_parse(
    *,
    account_id: int,
    chat_id: int,
    tokens: list[GroupToken],
    options: ParseOptions,
    on_progress: ProgressCallback,
    on_done: DoneCallback,
) -> int:
    """Создаёт задание и запускает его в фоне. Возвращает job_id."""
    if account_id in busy_account_ids():
        raise ParseStartError("Этот аккаунт уже парсит. Дождитесь окончания или остановите.")
    client = worker_pool.get_client(account_id)
    if client is None or not client.is_connected():
        raise ParseStartError("Аккаунт сейчас не подключён к Telegram, выберите другой.")

    async with session_scope() as session:
        accounts = await accounts_repo.list_accounts(session)
        exclude = {a.tg_user_id for a in accounts if a.tg_user_id}
        job_id = await parses_repo.create_job(
            session,
            account_id=account_id,
            chat_id=chat_id,
            groups=[[t.kind, t.value] for t in tokens],
            options=options.to_dict(),
        )

    run = ParseRun(
        job_id=job_id,
        account_id=account_id,
        client=client,
        tokens=tokens,
        options=options,
        exclude_user_ids=exclude,
        on_progress=on_progress,
        on_done=on_done,
    )
    _runs[job_id] = run
    worker_pool.set_hold(account_id, True)
    run.task = asyncio.create_task(run.run(), name=f"parse-{job_id}")
    logger.info(
        "parse[{}] started: account_id={} groups={}", job_id, account_id, len(tokens)
    )
    return job_id


async def recover_parse_jobs() -> None:
    """Старт приложения: задания, оборванные рестартом, → interrupted (§18.4 шаг 4)."""
    async with session_scope() as session:
        n = await parses_repo.mark_running_interrupted(session)
    if n:
        logger.info("Parse jobs interrupted by restart: {}", n)
