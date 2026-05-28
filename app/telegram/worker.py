"""Упрощённый MVP-2 воркер: один аккаунт обрабатывает одну активную рассылку.

ARCHITECTURE.md §5.2 «Перед отправкой DM», §6.1 (упрощённая версия).

Полноценный цикл с обработкой FloodWait/PeerFlood, quiet hours, лимитов,
SKIP LOCKED для пула воркеров — в MVP-3.
"""

from __future__ import annotations

import asyncio
from typing import Any

from loguru import logger
from telethon import TelegramClient
from telethon.errors import FloodWaitError, PeerFloodError

from app.config import settings
from app.db.models import (
    Account,
    Campaign,
    CampaignStatus,
    CampaignType,
    ResultCode,
    Template,
)
from app.db.repositories import accounts as accounts_repo
from app.db.repositories import campaigns as campaigns_repo
from app.db.repositories import logs as logs_repo
from app.db.repositories import processed as processed_repo
from app.db.repositories import settings as settings_repo
from app.db.repositories import tasks as tasks_repo
from app.db.repositories import templates as templates_repo
from app.db.session import session_scope
from app.campaigns.template_engine import render_template
from app.telegram.client_factory import create_client
from app.telegram.errors import classify_error
from app.telegram.humanize import (
    pre_action_pause,
    simulate_typing,
    sleep_inter_task,
)
from app.telegram.peer_cache import peer_cache


async def _get_interval_bounds() -> tuple[int, int]:
    """Берём текущие interval_min_sec / interval_max_sec из таблицы settings."""
    async with session_scope() as session:
        min_sec = await settings_repo.get(session, "interval_min_sec")
        max_sec = await settings_repo.get(session, "interval_max_sec")
    return (
        int(min_sec) if min_sec is not None else settings.interval_min_sec,
        int(max_sec) if max_sec is not None else settings.interval_max_sec,
    )


def _peer_to_vars(peer) -> dict[str, str]:
    """CachedPeer → словарь переменных для рендера шаблона."""
    first = peer.first_name or ""
    last = peer.last_name or ""
    full = (first + " " + last).strip()
    return {
        "username": peer.username or "",
        "first_name": first,
        "last_name": last,
        "full_name": full,
    }


async def _load_campaign_template(
    campaign_id: int,
) -> tuple[Campaign | None, Template | None]:
    async with session_scope() as session:
        campaign = await campaigns_repo.get_by_id(session, campaign_id)
        if campaign is None:
            return None, None
        template: Template | None = None
        if campaign.template_id is not None:
            template = await templates_repo.get_by_id(session, campaign.template_id)
        return campaign, template


async def _load_account(account_id: int) -> Account | None:
    async with session_scope() as session:
        return await accounts_repo.get_by_id(session, account_id)


async def _log(
    *,
    level: str,
    event_type: str,
    message: str,
    account_id: int | None = None,
    campaign_id: int | None = None,
    task_id: int | None = None,
    payload: dict[str, Any] | None = None,
) -> None:
    async with session_scope() as session:
        await logs_repo.log_event(
            session,
            level=level,
            event_type=event_type,
            message=message,
            account_id=account_id,
            campaign_id=campaign_id,
            task_id=task_id,
            payload=payload,
        )


async def run_campaign_worker(*, campaign_id: int, account_id: int) -> None:
    """Точка входа: один аккаунт прогоняет одну рассылочную кампанию.

    Создаёт свой Telethon-клиент, проходит queued-задачи по очереди до
    исчерпания или паузы/остановки кампании. При FloodWait/PeerFlood — ставит
    кампанию на паузу и завершает воркер (MVP-3 будет ждать разблокировки).
    """
    logger.info(
        "worker start: campaign_id={}, account_id={}", campaign_id, account_id
    )

    campaign, template = await _load_campaign_template(campaign_id)
    if campaign is None:
        logger.error("campaign not found: {}", campaign_id)
        return
    if campaign.type != CampaignType.message:
        logger.error("MVP-2 supports only 'message' campaigns, got {}", campaign.type)
        return
    if template is None:
        logger.error("campaign #{} has no template", campaign_id)
        return

    account = await _load_account(account_id)
    if account is None:
        logger.error("account not found: {}", account_id)
        return

    client = create_client(phone=account.phone, proxy_url=account.proxy_url)
    try:
        await client.connect()
        if not await client.is_user_authorized():
            await _log(
                level="error",
                event_type="account_dead",
                account_id=account_id,
                message=f"Аккаунт {account.phone} не авторизован, нужна реавторизация",
            )
            return

        await _run_loop(
            client=client,
            campaign_id=campaign_id,
            account_id=account_id,
            template_body=template.body,
        )

    finally:
        try:
            if client.is_connected():
                await client.disconnect()
        except Exception:
            pass
        logger.info(
            "worker stop: campaign_id={}, account_id={}", campaign_id, account_id
        )


async def _run_loop(
    *,
    client: TelegramClient,
    campaign_id: int,
    account_id: int,
    template_body: str,
) -> None:
    min_sec, max_sec = await _get_interval_bounds()

    while True:
        # Проверяем что кампания всё ещё running.
        async with session_scope() as session:
            campaign = await campaigns_repo.get_by_id(session, campaign_id)
            if campaign is None or campaign.status != CampaignStatus.running:
                logger.info(
                    "worker: campaign #{} status={} — выходим",
                    campaign_id,
                    campaign.status.value if campaign else "missing",
                )
                return

            task = await tasks_repo.get_next_queued(session, campaign_id=campaign_id)
            if task is None:
                # очередь пуста — завершаем
                await _finalize_campaign_if_done(session, campaign_id)
                return

            task_id = task.id
            username = task.username
            await tasks_repo.mark_in_progress(
                session, task_id=task_id, account_id=account_id
            )

        # Выполняем действие (вне транзакции, чтобы не держать lock на запись).
        outcome = await _process_one_task(
            client=client,
            campaign_id=campaign_id,
            account_id=account_id,
            task_id=task_id,
            username=username,
            template_body=template_body,
        )

        if outcome == "flood":
            # Ставим кампанию на паузу — MVP-3 разблокирует.
            async with session_scope() as session:
                await campaigns_repo.set_status(
                    session, campaign_id=campaign_id, status=CampaignStatus.paused
                )
            return

        # Пауза между действиями (если очередь не пуста — мы вернёмся в цикл).
        await sleep_inter_task(min_sec, max_sec)


async def _process_one_task(
    *,
    client: TelegramClient,
    campaign_id: int,
    account_id: int,
    task_id: int,
    username: str,
    template_body: str,
) -> str:
    """Выполняет один send_message. Возвращает строку-исход для управляющей логики.

    Возможные исходы: 'ok', 'skip', 'flood', 'error'.
    """
    # 1. Резолв peer (с кэшированием).
    peer = await peer_cache.get_or_resolve(client, username)
    if peer is None:
        await _record_skip(
            task_id=task_id,
            campaign_id=campaign_id,
            account_id=account_id,
            username=username,
            result_code=ResultCode.not_found,
            error_message="Username не найден или невалиден",
        )
        return "skip"

    # 2. Пауза перед действием + 3. (read_receipt в MVP-2 пропускаем).
    await pre_action_pause()

    # 4. Имитация набора.
    input_peer = peer.to_input_peer()
    try:
        await simulate_typing(client, input_peer)
    except Exception as e:
        # Если не смогли поставить typing — не фатал, едем дальше.
        logger.debug("typing failed for {}: {}", username, e)

    # 5. Рендер.
    text = render_template(template_body, _peer_to_vars(peer))

    # 6. Отправка.
    try:
        await client.send_message(input_peer, text)
    except FloodWaitError as e:
        await _log(
            level="warning",
            event_type="flood_wait",
            account_id=account_id,
            campaign_id=campaign_id,
            task_id=task_id,
            message=f"FloodWait {e.seconds}s при отправке {username}",
            payload={"seconds": e.seconds},
        )
        async with session_scope() as session:
            await tasks_repo.requeue_with_delay(
                session, task_id=task_id, delay_seconds=e.seconds
            )
        return "flood"
    except PeerFloodError:
        await _log(
            level="error",
            event_type="peer_flood",
            account_id=account_id,
            campaign_id=campaign_id,
            task_id=task_id,
            message=f"PeerFlood при отправке {username} (MVP-2: пауза кампании)",
        )
        async with session_scope() as session:
            await tasks_repo.requeue_with_delay(
                session, task_id=task_id, delay_seconds=12 * 3600
            )
        return "flood"
    except Exception as exc:
        classified = classify_error(exc)
        if classified is not None:
            result_code, _severity = classified
            await _record_skip(
                task_id=task_id,
                campaign_id=campaign_id,
                account_id=account_id,
                username=username,
                result_code=result_code,
                error_message=str(exc),
            )
            return "skip"
        # Неизвестная ошибка — task_failed.
        logger.exception("send_message failed for {}: {}", username, exc)
        await _record_failure(
            task_id=task_id,
            campaign_id=campaign_id,
            account_id=account_id,
            username=username,
            error_message=str(exc),
        )
        return "error"

    # 7. Успех.
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
            account_id=account_id,
            campaign_id=campaign_id,
        )
        await logs_repo.log_event(
            session,
            level="info",
            event_type="message_sent",
            message=f"Отправлено {username}",
            account_id=account_id,
            campaign_id=campaign_id,
            task_id=task_id,
            payload={"username": username},
        )
    return "ok"


async def _record_skip(
    *,
    task_id: int,
    campaign_id: int,
    account_id: int,
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
            account_id=account_id,
            campaign_id=campaign_id,
            task_id=task_id,
            payload={
                "username": username,
                "result_code": result_code.value,
                "error": error_message,
            },
        )


async def _record_failure(
    *,
    task_id: int,
    campaign_id: int,
    account_id: int,
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
            account_id=account_id,
            campaign_id=campaign_id,
            task_id=task_id,
            payload={"username": username, "error": error_message[:500]},
        )


async def _finalize_campaign_if_done(session, campaign_id: int) -> None:
    """Если очередь пуста и нет in_progress — переводим кампанию в done."""
    remaining = await tasks_repo.count_remaining_queued(session, campaign_id=campaign_id)
    if remaining == 0:
        await campaigns_repo.set_status(
            session,
            campaign_id=campaign_id,
            status=CampaignStatus.done,
            mark_finished=True,
        )
        await logs_repo.log_event(
            session,
            level="info",
            event_type="campaign_finished",
            message=f"Кампания #{campaign_id} завершена",
            campaign_id=campaign_id,
        )
