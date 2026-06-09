"""APScheduler-задачи. ARCHITECTURE.md §3 «Scheduler», §5.3, §7.1, §7.5.

Три задачи:
  1. spamcheck_job — раз в spamcheck_interval_sec для каждого активного аккаунта.
     Запуск каждого аккаунта смещён на (account_id % 240) сек чтобы не палить
     SpamBot залпом (§7.5).
  2. daily_limit_reset_job — каждый день 00:00 UTC обнуляет daily_sent/invited.
     §5.1.
  3. quiet_hours_check_job — раз в минуту, при входе в окно ставит active
     аккаунты в pause со spam_unlock_at = next_07_00 + uniform(0, 900). §5.3.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Callable

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from loguru import logger

from app.campaigns import progress as progress_mod
from app.db.models import AccountStatus
from app.db.repositories import accounts as accounts_repo
from app.db.repositories import campaigns as campaigns_repo
from app.db.repositories import logs as logs_repo
from app.db.repositories.settings import settings_cache
from app.db.session import session_scope
from app.notifications.admin import notify_admin
from app.telegram import antiflood
from app.telegram.spam_checker import (
    get_interval_multiplier,
    spam_check,
)
from app.utils.time import is_in_quiet_hours, next_quiet_end_at

DEFAULT_SPAMCHECK_INTERVAL_SEC = 240
DEFAULT_PROGRESS_NOTIFY_SEC = 1800
# Базовый тик progress-задачи; фактический интервал берётся из настроек каждый
# раз, чтобы /set progress_notify_interval_sec действовал без рестарта (§4.9).
PROGRESS_TICK_SECONDS = 60


class SchedulerService:
    """Связка AsyncIOScheduler + хелперы для добавления per-account задач.

    `client_provider` — функция account_id → TelegramClient | None (от WorkerPool).
    """

    def __init__(self, client_provider: Callable[[int], object | None]) -> None:
        self._scheduler = AsyncIOScheduler(timezone="UTC")
        self._client_provider = client_provider
        self._tracked_accounts: set[int] = set()
        self._last_quiet_state: bool | None = None
        self._last_progress_at: datetime | None = None
        self._last_global_flood: bool = False

    # ------------------ Lifecycle ------------------

    def start(self) -> None:
        # Постоянные задачи (не привязаны к аккаунтам).
        self._scheduler.add_job(
            self._daily_limit_reset_job,
            CronTrigger(hour=0, minute=0, timezone="UTC"),
            id="daily_limit_reset",
            replace_existing=True,
        )
        self._scheduler.add_job(
            self._quiet_hours_check_job,
            IntervalTrigger(minutes=1),
            id="quiet_hours_check",
            replace_existing=True,
        )
        self._scheduler.add_job(
            self._progress_notify_job,
            IntervalTrigger(seconds=PROGRESS_TICK_SECONDS),
            id="progress_notify",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
        self._scheduler.add_job(
            self._antiflood_check_job,
            IntervalTrigger(minutes=1),
            id="antiflood_check",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
        self._scheduler.start()
        logger.info("Scheduler started")

    async def stop(self) -> None:
        try:
            self._scheduler.shutdown(wait=False)
        except Exception:
            pass

    # ------------------ Per-account job management ------------------

    def add_spamcheck_for_account(self, account_id: int) -> None:
        """Добавить spamcheck-задачу для аккаунта с offset = account_id % 240."""
        if account_id in self._tracked_accounts:
            return
        base_interval = settings_cache.get_int(
            "spamcheck_interval_sec", DEFAULT_SPAMCHECK_INTERVAL_SEC
        )
        offset = account_id % base_interval  # §7.5 разнесение во времени

        async def _job(aid: int = account_id) -> None:
            await self._run_spamcheck(aid, base_interval)

        self._scheduler.add_job(
            _job,
            IntervalTrigger(seconds=base_interval, start_date=_offset_now(offset)),
            id=f"spamcheck_{account_id}",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
        self._tracked_accounts.add(account_id)
        logger.info(
            "Scheduler: spamcheck job added for account_id={} (interval={}s, offset={}s)",
            account_id, base_interval, offset,
        )

    def remove_spamcheck_for_account(self, account_id: int) -> None:
        try:
            self._scheduler.remove_job(f"spamcheck_{account_id}")
        except Exception:
            pass
        self._tracked_accounts.discard(account_id)

    # ------------------ Job implementations ------------------

    async def _run_spamcheck(self, account_id: int, base_interval: int) -> None:
        """Один опрос SpamBot. §7.5: если для этого аккаунта стоит множитель
        (после собственного FloodWait на запросах) — пропустим лишние тики."""
        mult = get_interval_multiplier(account_id)
        if mult > 1.0:
            # Грубая дросселяция: вероятность срабатывания 1/mult.
            import random
            if random.random() > 1.0 / mult:
                return

        client = self._client_provider(account_id)
        if client is None:
            return
        try:
            await spam_check(account_id=account_id, client=client)  # type: ignore[arg-type]
        except Exception:
            logger.exception("spam_check job failed for account_id={}", account_id)

    async def _daily_limit_reset_job(self) -> None:
        """Cron 00:00 UTC: обнулить счётчики у всех аккаунтов. §5.1."""
        async with session_scope() as session:
            n = await accounts_repo.reset_daily_counters(session)
            await logs_repo.log_event(
                session,
                level="info",
                event_type="daily_limits_reset",
                message=f"Сброс дневных счётчиков для {n} аккаунтов",
            )
        logger.info("Daily counters reset: {} accounts", n)

    async def _progress_notify_job(self) -> None:
        """§10.5: сводка по running-кампаниям раз в progress_notify_interval_sec.

        Тихо в простое: если активных кампаний нет — ничего не шлём и не двигаем
        отметку времени. Интервал читается из настроек каждый тик (hot-reload)."""
        interval = settings_cache.get_int(
            "progress_notify_interval_sec", DEFAULT_PROGRESS_NOTIFY_SEC
        )
        now = datetime.now(timezone.utc)
        if (
            self._last_progress_at is not None
            and (now - self._last_progress_at).total_seconds() < interval
        ):
            return
        async with session_scope() as session:
            text = await progress_mod.build_summary(session)
        if not text:
            return  # активных кампаний нет — молчим
        self._last_progress_at = now
        try:
            await notify_admin("<b>Сводка по кампаниям</b>\n\n" + text)
        except Exception:
            logger.exception("progress_notify send failed")

    async def _quiet_hours_check_job(self) -> None:
        """Каждую минуту проверяет, в окне ли мы. §5.3."""
        quiet_start = settings_cache.get_str("quiet_hours_start", "01:00")
        quiet_end = settings_cache.get_str("quiet_hours_end", "07:00")
        tz_name = settings_cache.get_str("quiet_hours_timezone", "Europe/Minsk")

        in_quiet = is_in_quiet_hours(
            quiet_start=quiet_start, quiet_end=quiet_end, tz_name=tz_name
        )

        # Реагируем только на ПЕРЕХОД (вход в окно).
        if self._last_quiet_state is False and in_quiet:
            await self._enter_quiet_hours(quiet_start, quiet_end, tz_name)
        # Выход из окна обрабатывают сами воркеры (они спят до next_quiet_end).
        self._last_quiet_state = in_quiet

    async def _enter_quiet_hours(
        self, quiet_start: str, quiet_end: str, tz_name: str
    ) -> None:
        """Переводим все active аккаунты в pause со spam_unlock_at = next_07_00 ± jitter."""
        async with session_scope() as session:
            active = await accounts_repo.list_active_during_quiet_start(session)
            if not active:
                await logs_repo.log_event(
                    session,
                    level="info",
                    event_type="quiet_hours_enter",
                    message="Вошли в quiet hours (нет active аккаунтов)",
                )
                return
            for account in active:
                wake_at = next_quiet_end_at(
                    quiet_start=quiet_start,
                    quiet_end=quiet_end,
                    tz_name=tz_name,
                )
                await accounts_repo.set_pause(
                    session, account_id=account.id, unlock_at=wake_at, reason="quiet_hours"
                )
            await logs_repo.log_event(
                session,
                level="info",
                event_type="quiet_hours_enter",
                message=f"Вошли в quiet hours: {len(active)} аккаунтов на паузу",
                payload={"accounts": [a.id for a in active]},
            )
        logger.info(
            "Quiet hours entered: {} accounts paused until end+jitter", len(active)
        )

    async def _antiflood_check_job(self) -> None:
        """§5.3: глобальная пауза при массовом флуде, адаптивный интервал, авто-resume.

        Единый оркестратор. Источник окна флуда — таблица `logs` (БД). Раз в минуту:
          • продлевает горячий флаг адаптивного интервала при недавнем флуде;
          • при кворуме (все рабочие аккаунты зафлудили) — паузит running-кампании;
          • при снятии флуда и появлении восстановленного аккаунта — возобновляет их.
        """
        now = datetime.now(timezone.utc)
        quorum_window = settings_cache.get_int("flood_window_quorum_sec", 1800)
        adaptive_window = settings_cache.get_int("flood_window_adaptive_sec", 3600)
        adaptive_hold = settings_cache.get_int("flood_adaptive_hold_sec", 3600)

        paused_ids: list[int] = []
        resumed_ids: list[int] = []
        park_size = 0
        async with session_scope() as session:
            park = {
                a.id for a in await accounts_repo.list_for_worker_pool(session)
            }
            park_size = len(park)
            flooded_quorum = await logs_repo.flooded_account_ids_since(
                session, since=now - timedelta(seconds=quorum_window)
            )
            adaptive_flood = await logs_repo.exists_peer_flood_since(
                session, since=now - timedelta(seconds=adaptive_window)
            )

            # 1. Адаптивный интервал: при недавнем флуде держим замедление (§5.3).
            if adaptive_flood:
                antiflood.set_adaptive_until(now + timedelta(seconds=adaptive_hold))

            # 2. Глобальная пауза: все рабочие аккаунты зафлудили за окно (§5.3).
            global_flood = antiflood.is_global_flood(flooded_quorum, park)
            if global_flood and not self._last_global_flood:
                paused_ids = await campaigns_repo.pause_running_with_reason(
                    session, reason="global_flood"
                )
                if paused_ids:
                    await logs_repo.log_event(
                        session,
                        level="error",
                        event_type="campaign_paused_global_flood",
                        message=(
                            f"Массовый PeerFlood ({park_size} акк.): "
                            f"пауза кампаний {paused_ids}"
                        ),
                        payload={"campaigns": paused_ids, "park": park_size},
                    )
            self._last_global_flood = global_flood

            # 3. Авто-возобновление: флуд снят и есть восстановленный аккаунт (§5.3).
            if not global_flood:
                flood_paused = await campaigns_repo.list_paused_by_reason(
                    session, reason="global_flood"
                )
                recovered = await accounts_repo.has_recovered_account(session)
                if antiflood.should_auto_resume(
                    has_recovered_account=recovered,
                    has_flood_paused_campaigns=bool(flood_paused),
                ):
                    resumed_ids = await campaigns_repo.resume_paused_by_reason(
                        session, reason="global_flood"
                    )
                    if resumed_ids:
                        await logs_repo.log_event(
                            session,
                            level="info",
                            event_type="campaign_resumed_global_flood",
                            message=f"Флуд снят, возобновлены кампании {resumed_ids}",
                            payload={"campaigns": resumed_ids},
                        )

        # Уведомления админу — вне транзакции (best-effort).
        if paused_ids:
            try:
                await notify_admin(
                    f"⛔ Массовый PeerFlood: все {park_size} рабочих аккаунтов "
                    f"под ограничением. Кампании {paused_ids} на паузе — "
                    f"возобновятся автоматически при снятии."
                )
            except Exception:
                logger.exception("antiflood pause notify failed")
        if resumed_ids:
            try:
                await notify_admin(
                    f"✓ Ограничения сняты — кампании {resumed_ids} возобновлены."
                )
            except Exception:
                logger.exception("antiflood resume notify failed")


def _offset_now(seconds: float) -> datetime:
    from datetime import timedelta
    return datetime.now(timezone.utc) + timedelta(seconds=seconds)


# ---------------------------------------------------------------------------
# Глобальный аксессор единственного SchedulerService (как worker_pool) — чтобы
# bot-хендлеры могли регистрировать spamcheck-задачу для аккаунта, добавленного
# в рантайме, без рестарта (MVP-5). Один event loop → блокировка не нужна.
# ---------------------------------------------------------------------------

_scheduler_instance: SchedulerService | None = None


def set_scheduler(service: SchedulerService) -> None:
    global _scheduler_instance
    _scheduler_instance = service


def get_scheduler() -> SchedulerService | None:
    return _scheduler_instance
