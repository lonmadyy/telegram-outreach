"""Worker для userbot-аккаунта. ARCHITECTURE.md §6.1.

В MVP-3 — один WorkerAccount на один Telethon-клиент. Цикл работает
непрерывно: берёт следующую задачу из ЛЮБОЙ running кампании через
SKIP LOCKED (§6.2), выполняет, обрабатывает FloodWait (§6.3) и PeerFlood
(§6.4) ровно по архитектуре, спит между действиями с джиттером (§5.2).

В отличие от MVP-2 (когда воркер привязан к одной кампании и завершается
по её окончании), MVP-3 воркер — долгоживущий. Он стартует с приложением
через WorkerPool и останавливается только при shutdown/dead.
"""

from __future__ import annotations

import asyncio
import random
from datetime import datetime, timedelta, timezone

from loguru import logger
from telethon import TelegramClient
from telethon.errors import FloodWaitError, PeerFloodError

from app.campaigns.template_engine import render_template
from app.db.models import (
    Account,
    AccountStatus,
    CampaignType,
    ResultCode,
)
from app.db.repositories import accounts as accounts_repo
from app.db.repositories import campaigns as campaigns_repo
from app.db.repositories import logs as logs_repo
from app.db.repositories import participants as participants_repo
from app.db.repositories import processed as processed_repo
from app.db.repositories import tasks as tasks_repo
from app.db.repositories import templates as templates_repo
from app.db.repositories.settings import settings_cache
from app.db.session import session_scope
from app.notifications.admin import notify_admin
from app.telegram import antiflood
from app.telegram import invite as invite_mod
from app.telegram import spam_checker
from app.telegram import warmup as warmup_mod
from app.telegram.errors import (
    ACCOUNT_SCOPED_INVITE_ERRORS,
    SESSION_DEAD_ERRORS,
    RetryableError,
    TaskOutcome,
    classify_error,
)
from app.telegram.humanize import (
    pre_action_pause,
    simulate_typing,
)
from app.telegram.peer_cache import peer_cache
from app.utils.time import (
    is_in_quiet_hours,
    next_quiet_end_at,
    seconds_until_midnight_utc,
)


# Дефолты на случай отсутствия настроек в кэше (один раз при старте).
DEFAULT_INTERVAL_MIN = 300
DEFAULT_INTERVAL_MAX = 540
# Адаптивный (замедленный) интервал при недавнем PeerFlood в системе (§5.3).
DEFAULT_FLOOD_INTERVAL_MIN = 450
DEFAULT_FLOOD_INTERVAL_MAX = 720
DEFAULT_DM_WARM = 40
DEFAULT_INVITE_WARM = 100
DEFAULT_DM_FRESH = 10
DEFAULT_INVITE_FRESH = 5
DEFAULT_REDUCTION_RATIO = 0.75

# Стартовый offset чтобы аккаунты не били залпом (§5.3 «Старт со смещением»).
STARTUP_OFFSET_MAX_SECONDS = 300

# Пауза между проверками статуса когда нет работы или ждём разблокировки.
IDLE_POLL_SECONDS = 30

# Warmup (§5.1, MVP-5): как часто аккаунт «оживает» во время прогрева и пауза
# между warmup-тиками в нулевой период лимитов (когда outreach ещё запрещён).
WARMUP_PRESENCE_PROB = 0.3
WARMUP_SAVED_PROB = 0.15
WARMUP_IDLE_SECONDS = 180


def _interval_bounds() -> tuple[int, int]:
    """Интервал между действиями (§5.2 п.7). При недавнем флуде в системе —
    адаптивный замедленный диапазон (§5.3), иначе обычный. Всё из кэша настроек;
    адаптивный флаг читается синхронно из antiflood (его держит scheduler-джоба)."""
    normal = (
        settings_cache.get_int("interval_min_sec", DEFAULT_INTERVAL_MIN),
        settings_cache.get_int("interval_max_sec", DEFAULT_INTERVAL_MAX),
    )
    flood = (
        settings_cache.get_int("flood_interval_min_sec", DEFAULT_FLOOD_INTERVAL_MIN),
        settings_cache.get_int("flood_interval_max_sec", DEFAULT_FLOOD_INTERVAL_MAX),
    )
    return antiflood.pick_interval(
        adaptive=antiflood.is_adaptive_active(datetime.now(timezone.utc)),
        normal=normal,
        flood=flood,
    )


def _limit_settings() -> tuple[int, int, int, int, float]:
    return (
        settings_cache.get_int("daily_dm_limit_warm", DEFAULT_DM_WARM),
        settings_cache.get_int("daily_invite_limit_warm", DEFAULT_INVITE_WARM),
        settings_cache.get_int("daily_dm_limit_fresh", DEFAULT_DM_FRESH),
        settings_cache.get_int("daily_invite_limit_fresh", DEFAULT_INVITE_FRESH),
        settings_cache.get_float("peerflood_limit_ratio", DEFAULT_REDUCTION_RATIO),
    )


def _quiet_settings() -> tuple[str, str, str]:
    return (
        settings_cache.get_str("quiet_hours_start", "01:00"),
        settings_cache.get_str("quiet_hours_end", "07:00"),
        settings_cache.get_str("quiet_hours_timezone", "Europe/Minsk"),
    )


class WorkerAccount:
    """Цикл воркера для одного аккаунта. §6.1."""

    def __init__(self, account_id: int, client: TelegramClient) -> None:
        self.account_id = account_id
        self.client = client
        self._stop_event = asyncio.Event()
        self._task: asyncio.Task | None = None
        # Кампании, которые этот аккаунт не может обрабатывать (MVP-4 §5.3:
        # invite-кампании, в чат которых он не может приглашать). Передаётся в
        # claim_next_task как exclude_campaign_ids, чтобы не зацикливаться.
        self._ineligible_campaigns: set[int] = set()

    # ------------------ Lifecycle ------------------

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(
                self.run(), name=f"worker-{self.account_id}"
            )

    async def stop(self) -> None:
        self._stop_event.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=30)
            except asyncio.TimeoutError:
                self._task.cancel()
            except Exception:
                pass

    @property
    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    # ------------------ Main loop ------------------

    async def run(self) -> None:
        logger.info("worker[{}] start", self.account_id)
        try:
            await self._ensure_connected()

            # Стартовый offset (§5.3): чтобы воркеры не били синхронно.
            offset = random.uniform(0, STARTUP_OFFSET_MAX_SECONDS)
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=offset)
                return  # stop пришёл во время offset
            except asyncio.TimeoutError:
                pass

            while not self._stop_event.is_set():
                try:
                    if not await self._ensure_alive():
                        await self._sleep(IDLE_POLL_SECONDS)
                        continue
                    await self._tick()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("worker[{}] tick failed", self.account_id)
                    await self._sleep(IDLE_POLL_SECONDS)
        finally:
            try:
                if self.client.is_connected():
                    await self.client.disconnect()
            except Exception:
                pass
            logger.info("worker[{}] stop", self.account_id)

    # ------------------ Internal helpers ------------------

    async def _ensure_connected(self) -> None:
        if not self.client.is_connected():
            await self.client.connect()
        if not await self.client.is_user_authorized():
            logger.error(
                "worker[{}]: session not authorized, marking dead",
                self.account_id,
            )
            async with session_scope() as session:
                await accounts_repo.set_dead(session, account_id=self.account_id)
                await logs_repo.log_event(
                    session,
                    level="error",
                    event_type="account_dead",
                    account_id=self.account_id,
                    message="Сессия невалидна, требуется реавторизация",
                )
            await notify_admin(
                f"❌ Worker {self.account_id}: сессия невалидна, "
                f"требуется реавторизация через /add_account."
            )
            self._stop_event.set()

    async def _ensure_alive(self) -> bool:
        """Лёгкая проверка живости соединения перед каждым тиком (§16).

        Telethon при сетевом обрыве делает `connection_retries` попыток и,
        исчерпав их, остаётся disconnected. Тогда любой запрос в _tick падает с
        `ConnectionError: Cannot send requests while disconnected`, и воркер
        бесконечно крутит ошибку (наблюдалось на проде: тысячи ERROR в логах,
        нулевая полезная работа). Здесь переподключаемся, если соединение
        отвалилось в рантайме.

        Без auth-RPC: валидность сессии уже проверена при старте в
        `_ensure_connected`, а мёртвую сессию (401) ловит обработка в _tick →
        `_mark_session_dead`. Возвращает True, если соединение живо/восстановлено,
        иначе False (воркер просто поспит и попробует на следующем тике — без
        шумного трейсбека на каждой итерации)."""
        if self.client.is_connected():
            return True
        logger.warning(
            "worker[{}] соединение потеряно — переподключаюсь", self.account_id
        )
        try:
            await self.client.connect()
            return True
        except Exception as exc:
            logger.warning(
                "worker[{}] reconnect не удался: {}", self.account_id, exc
            )
            return False

    async def _sleep(self, seconds: float) -> None:
        """Прерываемый sleep — выходит сразу при stop()."""
        if seconds <= 0:
            return
        try:
            await asyncio.wait_for(self._stop_event.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass

    async def _load_account(self) -> Account | None:
        async with session_scope() as session:
            return await accounts_repo.get_by_id(session, self.account_id)

    async def _maybe_warmup_presence(self, account: Account) -> None:
        """Вероятностная имитация присутствия во время warmup (§5.1). Best-effort —
        любая ошибка не должна влиять на основной цикл воркера."""
        try:
            now = datetime.now(timezone.utc)
            hours = (
                (now - account.created_at).total_seconds() / 3600
                if account.created_at is not None
                else float("inf")
            )
            plan = warmup_mod.warmup_actions_for_age(hours)
            if not plan.allow_presence:
                return
            if random.random() < WARMUP_PRESENCE_PROB:
                await warmup_mod.simulate_presence(self.client)
                if plan.allow_saved and random.random() < WARMUP_SAVED_PROB:
                    await warmup_mod.maybe_saved_message(self.client)
        except Exception:
            logger.debug("worker[{}] warmup presence failed", self.account_id)

    async def _mark_session_dead(self, reason: str) -> None:
        """Сессия/аккаунт недействительны (auth-ошибка 401 в рантайме) — переводим
        аккаунт в dead, уведомляем админа и останавливаем воркер (§5.1 dead, §16).
        Без этого воркер «висел» бы на мёртвой сессии, а spamcheck шумел бы вечно."""
        logger.error("worker[{}]: session dead — {}", self.account_id, reason)
        try:
            async with session_scope() as session:
                await accounts_repo.set_dead(session, account_id=self.account_id)
                await logs_repo.log_event(
                    session,
                    level="error",
                    event_type="account_dead",
                    account_id=self.account_id,
                    message=f"Сессия невалидна ({reason[:120]}), требуется реавторизация",
                )
        except Exception:
            logger.exception("worker[{}] set_dead failed", self.account_id)
        try:
            await notify_admin(
                f"❌ Аккаунт #{self.account_id}: сессия недействительна "
                f"({reason[:120]}). Переведён в dead — нужна реавторизация через "
                f"/add_account."
            )
        except Exception:
            pass
        self._stop_event.set()

    async def _tick(self) -> None:
        """Один шаг цикла §6.1. Возвращает после одного действия или паузы."""
        # 1. Проверка статуса.
        account = await self._load_account()
        if account is None:
            self._stop_event.set()
            return
        if account.status == AccountStatus.disabled:
            self._stop_event.set()
            return
        if account.status == AccountStatus.dead:
            self._stop_event.set()
            return

        # 1b. Авто-переход warmup → active по истечении warmup_until (§5.1 диаграмма).
        if account.status == AccountStatus.warmup and not accounts_repo.is_in_warmup(
            account
        ):
            async with session_scope() as session:
                await accounts_repo.set_active(session, account_id=self.account_id)
                await logs_repo.log_event(
                    session,
                    level="info",
                    event_type="account_activated",
                    account_id=self.account_id,
                    message="Warmup завершён, аккаунт переведён в active",
                )
            account.status = AccountStatus.active  # локально, чтобы не перечитывать

        # 1c. Истёкшая пауза (quiet-hours / FloodWait) → active (§5.3, §6.3, §6.5).
        # Возвращаем статус, когда пауза прошла. spam_blocked и активные паузы
        # (FloodWait со spam_unlock в будущем) НЕ трогаем — их снимает таймер/SpamBot.
        if accounts_repo.is_pause_expired(account):
            async with session_scope() as session:
                await accounts_repo.set_active(session, account_id=self.account_id)
                await logs_repo.log_event(
                    session,
                    level="info",
                    event_type="account_reactivated",
                    account_id=self.account_id,
                    message="Пауза истекла, аккаунт возвращён в active",
                )
            account.status = AccountStatus.active  # локально
            account.spam_unlock_at = None

        # 2. Quiet hours.
        quiet_start, quiet_end, tz_name = _quiet_settings()
        if is_in_quiet_hours(
            quiet_start=quiet_start, quiet_end=quiet_end, tz_name=tz_name
        ):
            wake = next_quiet_end_at(
                quiet_start=quiet_start, quiet_end=quiet_end, tz_name=tz_name
            )
            sleep_seconds = max(
                0.0, (wake - datetime.now(timezone.utc)).total_seconds()
            )
            await self._sleep(min(sleep_seconds, 6 * 3600))  # max 6h за один sleep
            return

        # 3. spam_unlock_at.
        if (
            account.spam_unlock_at is not None
            and account.spam_unlock_at > datetime.now(timezone.utc)
        ):
            # Просыпаемся каждые 30 сек — вдруг SpamBot снимет ограничение раньше.
            await self._sleep(IDLE_POLL_SECONDS)
            return

        # 4. Дневной лимит ПО ТИПАМ. Строим allowed_types для захвата — аккаунт
        # физически не возьмёт задачу типа, на который лимит исчерпан (§5.1: warmup
        # invite=0 → только message). Это и есть «реальная проверка по типу».
        dm_warm, invite_warm, dm_fresh, invite_fresh, ratio = _limit_settings()
        can_dm = accounts_repo.can_send_today(
            account,
            action_type=CampaignType.message,
            dm_warm=dm_warm, invite_warm=invite_warm,
            dm_fresh=dm_fresh, invite_fresh=invite_fresh,
            reduction_ratio=ratio,
        )
        can_invite = accounts_repo.can_send_today(
            account,
            action_type=CampaignType.invite,
            dm_warm=dm_warm, invite_warm=invite_warm,
            dm_fresh=dm_fresh, invite_fresh=invite_fresh,
            reduction_ratio=ratio,
        )

        in_warmup = accounts_repo.is_in_warmup(account)

        if not can_dm and not can_invite:
            if in_warmup:
                # Нулевой период лимитов (<12ч): не спим до полуночи — «греем»
                # аккаунт (онлайн + чтение канала) и проверяемся снова скоро.
                await warmup_mod.simulate_presence(self.client)
                await self._sleep(WARMUP_IDLE_SECONDS)
            else:
                # Спим до полуночи UTC (счётчики сбросит daily_limit_reset_job).
                await self._sleep(min(seconds_until_midnight_utc(), 6 * 3600))
            return

        allowed_types: list[str] = []
        if can_dm:
            allowed_types.append(CampaignType.message.value)
        if can_invite:
            allowed_types.append(CampaignType.invite.value)

        # Лёгкое присутствие во время warmup и в периоды с ненулевым лимитом (§5.1).
        if in_warmup:
            await self._maybe_warmup_presence(account)

        # 5. Захват задачи (SKIP LOCKED). Исключаем кампании, для которых этот
        # аккаунт признан непригодным (invite без прав/членства, §5.3), и
        # ограничиваем типами, доступными по дневному лимиту (§5.1).
        async with session_scope() as session:
            claimed = await tasks_repo.claim_next_task(
                session,
                account_id=self.account_id,
                exclude_campaign_ids=list(self._ineligible_campaigns),
                allowed_types=allowed_types,
            )
        if claimed is None:
            await self._sleep(IDLE_POLL_SECONDS)
            return

        # 6. Выполнение.
        await self._execute_claimed(
            task_id=claimed["id"],
            campaign_id=claimed["campaign_id"],
            username=claimed["username"],
            account=account,
        )

        # 7. Пауза с джиттером между действиями (§5.2 п.7).
        min_sec, max_sec = _interval_bounds()
        base = random.uniform(min_sec, max_sec)
        delay = base * random.uniform(0.85, 1.15)
        await self._sleep(delay)

    async def _execute_claimed(
        self,
        *,
        task_id: int,
        campaign_id: int,
        username: str,
        account: Account,
    ) -> None:
        """Диспетчер по типу кампании. message → §5.2 DM-путь, invite → §5.2 invite-путь."""
        async with session_scope() as session:
            campaign = await campaigns_repo.get_by_id(session, campaign_id)
            template = None
            if campaign and campaign.template_id is not None:
                template = await templates_repo.get_by_id(
                    session, campaign.template_id
                )

        if campaign is None:
            await self._record_failure(
                task_id=task_id,
                campaign_id=campaign_id,
                username=username,
                error_message="Кампания не найдена",
            )
            return

        if campaign.type == CampaignType.invite:
            await self._execute_invite(
                task_id=task_id,
                campaign_id=campaign_id,
                username=username,
                account=account,
                campaign=campaign,
            )
            return

        # message
        if template is None:
            await self._record_failure(
                task_id=task_id,
                campaign_id=campaign_id,
                username=username,
                error_message="Campaign has no template",
            )
            return
        await self._execute_message(
            task_id=task_id,
            campaign_id=campaign_id,
            username=username,
            account=account,
            template=template,
        )

    async def _execute_message(
        self,
        *,
        task_id: int,
        campaign_id: int,
        username: str,
        account: Account,
        template,
    ) -> None:
        """DM-путь §5.2 «Перед отправкой DM»."""
        # 1. Резолв peer.
        peer = await peer_cache.get_or_resolve(self.client, username)
        if peer is None:
            await self._record_skip(
                task_id=task_id,
                campaign_id=campaign_id,
                username=username,
                result_code=ResultCode.not_found,
                error_message="Username не найден или невалиден",
            )
            return

        # 2-4. pre_action_pause + typing.
        await pre_action_pause()
        try:
            await simulate_typing(self.client, peer.to_input_peer())
        except Exception:
            pass  # не критично

        # 5. Рендер шаблона.
        vars_for_render = {
            "username": peer.username or "",
            "first_name": peer.first_name or "",
            "last_name": peer.last_name or "",
            "full_name": (
                ((peer.first_name or "") + " " + (peer.last_name or "")).strip()
            ),
        }
        text = render_template(template.body, vars_for_render)

        # 6. Отправка.
        try:
            await self.client.send_message(peer.to_input_peer(), text)
        except FloodWaitError as e:
            await self._handle_flood_wait(
                task_id=task_id, campaign_id=campaign_id, seconds=e.seconds,
                username=username,
            )
            return
        except PeerFloodError:
            await self._handle_peer_flood(
                task_id=task_id,
                campaign_id=campaign_id,
                phone=account.phone,
                username=username,
            )
            return
        except SESSION_DEAD_ERRORS as exc:
            # Сессия аннулирована (разлогин/бан) — возвращаем задачу и в dead.
            async with session_scope() as session:
                await tasks_repo.requeue_with_delay(
                    session, task_id=task_id, delay_seconds=0
                )
            await self._mark_session_dead(str(exc))
            return
        except Exception as exc:
            mapped = classify_error(exc)
            if mapped is not None:
                rc, severity = mapped
                await self._record_skip(
                    task_id=task_id,
                    campaign_id=campaign_id,
                    username=username,
                    result_code=rc,
                    error_message=str(exc),
                )
                if severity == "fatal":
                    await self._pause_campaign_due_to_fatal(
                        campaign_id, str(exc)
                    )
                return
            logger.exception("worker[{}] send_message failed", self.account_id)
            await self._record_failure(
                task_id=task_id,
                campaign_id=campaign_id,
                username=username,
                error_message=str(exc),
            )
            return

        # Успех.
        async with session_scope() as session:
            await tasks_repo.mark_done(
                session, task_id=task_id, result_code=ResultCode.ok
            )
            await campaigns_repo.update_counts(
                session, campaign_id=campaign_id, sent_delta=1
            )
            await processed_repo.register_processed(
                session,
                username=username,
                last_action="message",
                last_result_code=ResultCode.ok,
                account_id=self.account_id,
                campaign_id=campaign_id,
            )
            await accounts_repo.increment_counter(
                session,
                account_id=self.account_id,
                action_type=CampaignType.message,
            )
            await logs_repo.log_event(
                session,
                level="info",
                event_type="message_sent",
                message=f"Отправлено {username}",
                account_id=self.account_id,
                campaign_id=campaign_id,
                task_id=task_id,
                payload={"username": username},
            )

    async def _execute_invite(
        self,
        *,
        task_id: int,
        campaign_id: int,
        username: str,
        account: Account,
        campaign,
    ) -> None:
        """Invite-путь §5.2 «Перед инвайтом» (без typing) + §1.3 InviteToChannelRequest."""
        if not campaign.target_chat or campaign.target_chat_id is None:
            await self._record_failure(
                task_id=task_id,
                campaign_id=campaign_id,
                username=username,
                error_message="Invite-кампания без целевого чата (target не разрешён)",
            )
            return

        chat_id = campaign.target_chat_id
        norm = invite_mod.normalize_target_input(campaign.target_chat)
        target_key = norm if norm is not None else campaign.target_chat

        # 1. Резолв целевого чата ЭТИМ аккаунтом. Если не участник/нет доступа —
        # помечаем аккаунт непригодным для кампании (§5.3), задачу вернём в очередь.
        try:
            target_input = await self.client.get_input_entity(target_key)
        except SESSION_DEAD_ERRORS as exc:
            async with session_scope() as session:
                await tasks_repo.requeue_with_delay(
                    session, task_id=task_id, delay_seconds=0
                )
            await self._mark_session_dead(str(exc))
            return
        except ACCOUNT_SCOPED_INVITE_ERRORS as exc:
            await self._mark_account_ineligible(
                task_id=task_id, campaign_id=campaign_id,
                username=username, reason=str(exc),
            )
            return
        except (ValueError, TypeError) as exc:
            await self._mark_account_ineligible(
                task_id=task_id, campaign_id=campaign_id,
                username=username, reason=f"resolve failed: {exc}",
            )
            return

        # 2. Префетч участников (§5.2). FloodWait — пауза аккаунта, задача в очередь.
        try:
            await invite_mod.participants_cache.ensure_loaded(
                self.client, chat_id=chat_id, target_input=target_input
            )
        except FloodWaitError as e:
            await self._handle_flood_wait(
                task_id=task_id, campaign_id=campaign_id,
                seconds=e.seconds, username=username,
            )
            return

        # §19 #11: один раз на чат подгружаем наших ранее приглашённых из БД в кэш,
        # чтобы после рестарта не пытаться пригласить их повторно (инкрементальный снимок).
        if not invite_mod.participants_cache.db_loaded(chat_id):
            async with session_scope() as session:
                invited_ids = await participants_repo.list_invited_user_ids(
                    session, chat_id=chat_id
                )
            invite_mod.participants_cache.merge_members(chat_id, invited_ids)
            invite_mod.participants_cache.mark_db_loaded(chat_id)

        # 3. Резолв получателя.
        peer = await peer_cache.get_or_resolve(self.client, username)
        if peer is None:
            await self._record_skip(
                task_id=task_id,
                campaign_id=campaign_id,
                username=username,
                result_code=ResultCode.not_found,
                error_message="Username не найден или невалиден",
            )
            return

        # 4. Уже участник? — пропускаем без запроса.
        if invite_mod.participants_cache.is_member(chat_id, peer.user_id):
            await self._record_skip(
                task_id=task_id,
                campaign_id=campaign_id,
                username=username,
                result_code=ResultCode.already_member,
                error_message="Уже состоит в целевом чате",
            )
            return

        # 5. Пауза перед действием. Для invite typing НЕ имитируем (§5.2).
        await pre_action_pause()

        # 6. Инвайт.
        try:
            outcome = await invite_mod.do_invite(
                self.client,
                target_input=target_input,
                user_input=peer.to_input_peer(),
                user_id=peer.user_id,
            )
        except FloodWaitError as e:
            await self._handle_flood_wait(
                task_id=task_id, campaign_id=campaign_id,
                seconds=e.seconds, username=username,
            )
            return
        except PeerFloodError:
            await self._handle_peer_flood(
                task_id=task_id,
                campaign_id=campaign_id,
                phone=account.phone,
                username=username,
            )
            return
        except ACCOUNT_SCOPED_INVITE_ERRORS as exc:
            # Право/членство этого аккаунта — не валим кампанию (§5.3).
            await self._mark_account_ineligible(
                task_id=task_id, campaign_id=campaign_id,
                username=username, reason=str(exc),
            )
            return
        except SESSION_DEAD_ERRORS as exc:
            async with session_scope() as session:
                await tasks_repo.requeue_with_delay(
                    session, task_id=task_id, delay_seconds=0
                )
            await self._mark_session_dead(str(exc))
            return
        except Exception as exc:
            mapped = classify_error(exc)
            if mapped is not None:
                rc, severity = mapped
                await self._record_skip(
                    task_id=task_id,
                    campaign_id=campaign_id,
                    username=username,
                    result_code=rc,
                    error_message=str(exc),
                )
                if severity == "fatal":
                    await self._pause_campaign_due_to_fatal(campaign_id, str(exc))
                return
            logger.exception("worker[{}] invite failed", self.account_id)
            await self._record_failure(
                task_id=task_id,
                campaign_id=campaign_id,
                username=username,
                error_message=str(exc),
            )
            return

        # 7. Telegram молча не добавил (missing_invitees) — skip, НЕ пишем в processed.
        if not outcome.ok:
            await self._record_skip(
                task_id=task_id,
                campaign_id=campaign_id,
                username=username,
                result_code=outcome.result_code or ResultCode.privacy_restricted,
                error_message="Не добавлен (missing_invitees)",
            )
            return

        # 8. Успех.
        invite_mod.participants_cache.add_member(chat_id, peer.user_id)
        async with session_scope() as session:
            await tasks_repo.mark_done(
                session, task_id=task_id, result_code=ResultCode.ok
            )
            await participants_repo.add_invited(
                session,
                chat_id=chat_id,
                user_id=peer.user_id,
                campaign_id=campaign_id,
            )
            await campaigns_repo.update_counts(
                session, campaign_id=campaign_id, sent_delta=1
            )
            await processed_repo.register_processed(
                session,
                username=username,
                last_action="invite",
                last_result_code=ResultCode.ok,
                account_id=self.account_id,
                campaign_id=campaign_id,
            )
            await accounts_repo.increment_counter(
                session,
                account_id=self.account_id,
                action_type=CampaignType.invite,
            )
            await logs_repo.log_event(
                session,
                level="info",
                event_type="invite_sent",
                message=f"Приглашён {username} в {campaign.target_chat}",
                account_id=self.account_id,
                campaign_id=campaign_id,
                task_id=task_id,
                payload={"username": username, "target_chat_id": chat_id},
            )

    async def _mark_account_ineligible(
        self,
        *,
        task_id: int,
        campaign_id: int,
        username: str,
        reason: str,
    ) -> None:
        """§5.3: этот аккаунт не может приглашать в чат кампании. Помечаем его
        непригодным (in-memory), возвращаем задачу в очередь для другого аккаунта.
        Если непригодны ВСЕ рабочие аккаунты — паузим кампанию (§16.1 fatal-аналог)."""
        self._ineligible_campaigns.add(campaign_id)
        invite_mod.mark_ineligible(campaign_id, self.account_id)
        async with session_scope() as session:
            await tasks_repo.requeue_with_delay(
                session, task_id=task_id, delay_seconds=60
            )
            total = len(await accounts_repo.list_for_worker_pool(session))
            await logs_repo.log_event(
                session,
                level="warning",
                event_type="account_invite_ineligible",
                account_id=self.account_id,
                campaign_id=campaign_id,
                task_id=task_id,
                message=(
                    f"Аккаунт не может приглашать в чат кампании #{campaign_id}: "
                    f"{reason[:200]}"
                ),
                payload={"username": username, "reason": reason[:500]},
            )
        if invite_mod.all_ineligible(campaign_id, total):
            await self._pause_campaign_due_to_fatal(
                campaign_id,
                "ни один рабочий аккаунт не может приглашать в целевой чат",
            )

    # ------------------ Failure paths ------------------

    async def _handle_flood_wait(
        self,
        *,
        task_id: int,
        campaign_id: int,
        seconds: int,
        username: str,
    ) -> None:
        """ARCHITECTURE.md §6.3."""
        unlock_at = datetime.now(timezone.utc) + timedelta(seconds=seconds)
        async with session_scope() as session:
            await tasks_repo.requeue_with_delay(
                session, task_id=task_id, delay_seconds=seconds
            )
            await accounts_repo.set_pause(
                session, account_id=self.account_id, unlock_at=unlock_at
            )
            await logs_repo.log_event(
                session,
                level="warning",
                event_type="flood_wait",
                account_id=self.account_id,
                campaign_id=campaign_id,
                task_id=task_id,
                message=f"FloodWait {seconds}s, аккаунт на паузе до {unlock_at:%Y-%m-%d %H:%M UTC}",
                payload={"seconds": seconds, "username": username},
            )

    async def _handle_peer_flood(
        self,
        *,
        task_id: int,
        campaign_id: int,
        phone: str,
        username: str,
    ) -> None:
        """ARCHITECTURE.md §6.4."""
        unlock_at = datetime.now(timezone.utc) + timedelta(hours=12)
        limit_reduce_until = datetime.now(timezone.utc) + timedelta(days=7)
        async with session_scope() as session:
            await tasks_repo.requeue_with_delay(
                session, task_id=task_id, delay_seconds=12 * 3600
            )
            await accounts_repo.set_spam_blocked(
                session,
                account_id=self.account_id,
                unlock_at=unlock_at,
                limit_reduce_until=limit_reduce_until,
            )
            await logs_repo.log_event(
                session,
                level="error",
                event_type="peer_flood",
                account_id=self.account_id,
                campaign_id=campaign_id,
                task_id=task_id,
                message="PeerFlood: 12ч карантин, лимит снижен до 75% на 7 дней",
                payload={"username": username},
            )
        await notify_admin(
            f"⚠ Аккаунт {phone} получил PeerFlood. 12ч карантин, лимит 75% на 7д.\n"
            f"SpamBot опросит и автоматически снимет если ограничение снимется раньше."
        )
        # §6.4: немедленный SpamBot-чек — не ждём планового (раз в ~4 мин), чтобы
        # сразу уточнить характер ограничения (и снять блок, если он мягче). Best-
        # effort: ошибка чека не должна влиять на обработку PeerFlood.
        try:
            await spam_checker.spam_check(
                account_id=self.account_id, client=self.client
            )
        except Exception:
            logger.debug(
                "worker[{}] immediate spam_check after PeerFlood failed",
                self.account_id,
            )

    async def _record_skip(
        self,
        *,
        task_id: int,
        campaign_id: int,
        username: str,
        result_code: ResultCode,
        error_message: str | None,
    ) -> None:
        async with session_scope() as session:
            await tasks_repo.mark_skipped(
                session,
                task_id=task_id,
                result_code=result_code,
                error_message=error_message,
            )
            await campaigns_repo.update_counts(
                session, campaign_id=campaign_id, skipped_delta=1
            )
            await logs_repo.log_event(
                session,
                level="info",
                event_type="task_skipped",
                message=f"Skip {username}: {result_code.value}",
                account_id=self.account_id,
                campaign_id=campaign_id,
                task_id=task_id,
                payload={
                    "username": username,
                    "result_code": result_code.value,
                    "error": error_message,
                },
            )

    async def _record_failure(
        self,
        *,
        task_id: int,
        campaign_id: int,
        username: str,
        error_message: str,
    ) -> None:
        async with session_scope() as session:
            await tasks_repo.mark_failed(
                session,
                task_id=task_id,
                result_code=ResultCode.other_error,
                error_message=error_message,
            )
            await campaigns_repo.update_counts(
                session, campaign_id=campaign_id, failed_delta=1
            )
            await logs_repo.log_event(
                session,
                level="error",
                event_type="task_failed",
                message=f"Fail {username}: {error_message[:200]}",
                account_id=self.account_id,
                campaign_id=campaign_id,
                task_id=task_id,
                payload={"username": username, "error": error_message[:500]},
            )

    async def _pause_campaign_due_to_fatal(
        self, campaign_id: int, reason: str
    ) -> None:
        from app.db.models import CampaignStatus

        async with session_scope() as session:
            await campaigns_repo.set_status(
                session,
                campaign_id=campaign_id,
                status=CampaignStatus.paused,
            )
            await logs_repo.log_event(
                session,
                level="error",
                event_type="campaign_paused_fatal",
                campaign_id=campaign_id,
                message=f"Кампания паузится из-за fatal-ошибки: {reason[:200]}",
            )
        await notify_admin(
            f"⚠ Кампания #{campaign_id} поставлена на паузу: {reason[:300]}"
        )
