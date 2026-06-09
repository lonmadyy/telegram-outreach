"""Единый визуальный язык сообщений бота. ARCHITECTURE.md §10.2.

Чистые функции форматирования: карточки, статусы по-русски с эмодзи, числа с
разделителем тысяч. Переиспользуются всеми списками (accounts/campaigns/status/
templates), чтобы формат был согласованным и читаемым.

НЕ меняет логику и не трогает БД — только человеческое представление. Английские
значения статусов остаются в модели/БД; здесь лишь их отображение.
"""

from __future__ import annotations

from datetime import datetime, timezone

from app.db.models import (
    Account,
    AccountStatus,
    Campaign,
    CampaignStatus,
    CampaignType,
)
from app.db.repositories import accounts as accounts_repo


# --- Числа и даты ---------------------------------------------------------


def num(n: int) -> str:
    """Разделитель тысяч пробелом: 120219 → '120 219'."""
    return f"{n:,}".replace(",", " ")


def fmt_dt(dt: datetime | None, *, now: datetime | None = None) -> str:
    """Компактное время (UTC, как хранится): 'HH:MM' если сегодня, иначе 'DD.MM HH:MM'.

    TZ намеренно не конвертируем — это задача про формат, а не про семантику времени.
    """
    if dt is None:
        return "—"
    now = now or datetime.now(timezone.utc)
    if dt.date() == now.date():
        return f"{dt:%H:%M}"
    return f"{dt:%d.%m %H:%M}"


# --- Статусы аккаунта -----------------------------------------------------

_ACCOUNT_STATUS: dict[AccountStatus, tuple[str, str]] = {
    AccountStatus.warmup: ("🔥", "прогрев"),
    AccountStatus.active: ("🟢", "активен"),
    AccountStatus.spam_blocked: ("🚫", "спам-блок"),
    AccountStatus.dead: ("⚰️", "нужна реавторизация"),
    AccountStatus.disabled: ("⛔", "отключён"),
    AccountStatus.pause: ("⏸", "пауза"),  # уточняется по pause_reason ниже
}


def account_status(acc: Account, *, now: datetime | None = None) -> tuple[str, str]:
    """(эмодзи, человеческий статус) аккаунта. Для паузы различает причину —
    FloodWait / тихие часы / прочее — и показывает время до снятия."""
    now = now or datetime.now(timezone.utc)
    if acc.status == AccountStatus.pause:
        until = f" до {fmt_dt(acc.spam_unlock_at, now=now)}" if acc.spam_unlock_at else ""
        if accounts_repo.is_flood_waiting(acc, now=now):
            return "⏳", f"FloodWait{until}"
        if acc.pause_reason == "quiet_hours":
            return "🌙", f"тихие часы{until}"
        return "⏸", f"пауза{until}"
    emoji, text = _ACCOUNT_STATUS.get(acc.status, ("•", acc.status.value))
    if acc.status == AccountStatus.spam_blocked and acc.spam_unlock_at:
        text += f" до {fmt_dt(acc.spam_unlock_at, now=now)}"
    return emoji, text


def account_card(acc: Account, *, now: datetime | None = None) -> str:
    """Двухстрочная карточка аккаунта для /accounts."""
    emoji, status = account_status(acc, now=now)
    now = now or datetime.now(timezone.utc)
    head = f"{emoji} #{acc.id} · <b>{acc.phone}</b>"
    bits = [status, f"сегодня: {acc.daily_invited} инв · {acc.daily_sent} DM"]
    if acc.limit_reduced_until is not None and acc.limit_reduced_until > now:
        bits.append("лимит 75%")
    return head + "\n    " + " · ".join(bits)


# --- Статусы и типы кампании ----------------------------------------------

_CAMPAIGN_STATUS: dict[CampaignStatus, tuple[str, str]] = {
    CampaignStatus.pending: ("🕐", "ожидает"),
    CampaignStatus.running: ("▶️", "идёт"),
    CampaignStatus.paused: ("⏸", "пауза"),
    CampaignStatus.done: ("✅", "завершена"),
    CampaignStatus.failed: ("⚠️", "сбой"),
    CampaignStatus.cancelled: ("⛔", "отменена"),
}

_CAMPAIGN_TYPE: dict[CampaignType, str] = {
    CampaignType.message: "рассылка",
    CampaignType.invite: "инвайт",
}


def campaign_type_ru(c: Campaign) -> str:
    return _CAMPAIGN_TYPE.get(c.type, c.type.value)


def sent_label(c: Campaign) -> str:
    """Подпись успешного счётчика по типу: инвайт → «Приглашено», иначе «Отправлено»."""
    return "Приглашено" if c.type == CampaignType.invite else "Отправлено"


def campaign_card(c: Campaign) -> str:
    """Трёхстрочная карточка кампании для /campaigns и /status."""
    emoji, status = _CAMPAIGN_STATUS.get(c.status, ("•", c.status.value))
    pct = int(c.sent_count / c.total_count * 100) if c.total_count else 0
    head = f"{emoji} <b>Кампания #{c.id}</b> · {campaign_type_ru(c)} · {status}"
    line2 = f"    {sent_label(c)}: {num(c.sent_count)} из {num(c.total_count)} ({pct}%)"
    line3 = f"    Пропущено: {num(c.skipped_count)} · Ошибки: {num(c.failed_count)}"
    return f"{head}\n{line2}\n{line3}"


# --- Заголовок секции -----------------------------------------------------


def section_header(emoji: str, title: str, subtitle: str | None = None) -> str:
    """Единый заголовок: '👤 <b>Аккаунты</b> — 4 активных'."""
    head = f"{emoji} <b>{title}</b>"
    if subtitle:
        head += f" — {subtitle}"
    return head
