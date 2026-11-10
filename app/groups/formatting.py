"""Тексты раздела «Рассылка по группам». ARCHITECTURE.md §22.9.

Чистые функции (tests/test_group_formatting.py). Время в карточках — по часовому поясу
тихих часов (Минск по умолчанию): это то, что видит человек.
"""

from __future__ import annotations

from datetime import datetime, timezone
from html import escape
from zoneinfo import ZoneInfo

from app.bot.formatting import num
from app.groups.planner import RoundEstimate

STATUS = {
    "running": ("▶️", "идёт"),
    "paused": ("⏸", "пауза"),
    "stopped": ("⏹", "остановлена"),
}
STATE = {
    "new": ("🆕", "ждёт вступления"),
    "pending": ("⏳", "заявка отправлена"),
    "active": ("🟢", "активна"),
    "manual": ("🖐", "нужна ручная проверка"),
    "excluded": ("🚫", "исключена"),
}
DISTRIBUTION = {
    "even": "поровну",
    "all": "каждый аккаунт во все группы",
    "manual": "вручную по меткам #id",
}


def minutes_range(lo: int, hi: int) -> str:
    """30–200 мин · 2–4 ч · 90 мин."""
    def one(m: int) -> str:
        return f"{m // 60}" if m % 60 == 0 and m >= 60 else str(m)

    if lo % 60 == 0 and hi % 60 == 0 and lo >= 60:
        return f"{one(lo)} ч" if lo == hi else f"{one(lo)}–{one(hi)} ч"
    return f"{lo} мин" if lo == hi else f"{lo}–{hi} мин"


def gap_range(lo_sec: int, hi_sec: int) -> str:
    lo, hi = round(lo_sec / 60, 1), round(hi_sec / 60, 1)
    f = lambda x: str(int(x)) if x == int(x) else str(x)  # noqa: E731
    return f"{f(lo)} мин" if lo == hi else f"{f(lo)}–{f(hi)} мин"


def window_text(start: str | None, end: str | None) -> str:
    if not start or not end:
        return "весь день, кроме тихих часов"
    return f"{start}–{end}"


def local_time(dt: datetime | None, tz_name: str, *, now: datetime | None = None) -> str:
    if dt is None:
        return "—"
    tz = ZoneInfo(tz_name)
    loc = dt.astimezone(tz)
    today = (now or datetime.now(timezone.utc)).astimezone(tz).date()
    return f"{loc:%H:%M}" if loc.date() == today else f"{loc:%d.%m %H:%M}"


def intervals_block(
    period_min: int, period_max: int, gap_min: int, gap_max: int,
    window_start: str | None, window_end: str | None,
) -> str:
    return (
        f"• Повтор в каждой группе: <b>{minutes_range(period_min, period_max)}</b>\n"
        f"• Пауза между постами аккаунта: <b>{gap_range(gap_min, gap_max)}</b>\n"
        f"• Окно работы: <b>{window_text(window_start, window_end)}</b>"
    )


def states_line(by_state: dict[str, int]) -> str:
    parts = []
    for key in ("active", "new", "pending", "manual", "excluded"):
        n = by_state.get(key, 0)
        if n:
            emoji, label = STATE[key]
            parts.append(f"{emoji} {label}: {num(n)}")
    return " · ".join(parts) if parts else "групп нет"


def campaign_brief(c, stats) -> str:
    """Две строки для списка рассылок."""
    emoji, status = STATUS.get(c.status, ("•", c.status))
    head = f"{emoji} <b>#{c.id} · {escape(c.name)}</b> — {status}"
    tail = (
        f"    групп: {num(stats.total)} · аккаунтов: {stats.accounts} · "
        f"постов за сутки: {num(stats.posts_24h)}"
    )
    if stats.problems:
        tail += f" · ⚠ проблемных: {stats.problems}"
    return f"{head}\n{tail}"


def campaign_card(c, stats, *, template_name: str, tz_name: str, now: datetime | None = None) -> str:
    emoji, status = STATUS.get(c.status, ("•", c.status))
    lines = [
        f"{emoji} <b>Рассылка #{c.id} · {escape(c.name)}</b> — {status}",
        "",
        f"📝 Шаблон: <b>{escape(template_name)}</b>",
        f"👤 Аккаунтов: <b>{stats.accounts}</b> · распределение: {DISTRIBUTION.get(c.distribution, c.distribution)}",
        intervals_block(
            c.period_min, c.period_max, c.gap_min_sec, c.gap_max_sec,
            c.window_start, c.window_end,
        ),
        "",
        f"📋 Групп: <b>{num(stats.total)}</b>",
        f"    {states_line(stats.by_state)}",
    ]
    if stats.unassigned:
        lines.append(f"    ⚠ без живого аккаунта: {stats.unassigned}")
    lines += [
        "",
        f"📨 Постов за сутки: <b>{num(stats.posts_24h)}</b>",
        f"    последний: {local_time(stats.last_post_at, tz_name, now=now)} · "
        f"ближайший: {local_time(stats.next_post_at, tz_name, now=now) if c.status == 'running' else '—'}",
    ]
    return "\n".join(lines)


def target_line(t, *, tz_name: str, now: datetime | None = None) -> str:
    emoji, label = STATE.get(t.state, ("•", t.state))
    name = escape(t.title or ref_label(t.ref_kind, t.ref_value))
    acc = f"#{t.account_id}" if t.account_id else "без аккаунта"
    bits = [f"{emoji} {name}", acc]
    if t.state == "active":
        bits.append(f"постов {t.posts_count}")
        bits.append(f"след. {local_time(t.next_at, tz_name, now=now)}")
    elif t.state_reason:
        bits.append(escape(t.state_reason[:90]))
    else:
        bits.append(label)
    return " · ".join(bits)


def ref_label(kind: str, value: str) -> str:
    if kind == "username":
        return f"@{value}"
    if kind == "invite":
        return f"t.me/+{value}"
    return str(value)


def estimate_block(estimates: list[RoundEstimate], period_min: int) -> str:
    """Оценка круга по аккаунтам (§22.9)."""
    lines = []
    slow = False
    for e in sorted(estimates, key=lambda x: x.account_id):
        mark = " ⚠" if e.slower_than_period else ""
        lines.append(
            f"• #{e.account_id}: {num(e.groups)} групп, круг ≈ {_human_minutes(e.round_minutes)}{mark}"
        )
        slow = slow or e.slower_than_period
    if slow:
        lines.append(
            f"⚠ У отмеченных аккаунтов круг дольше {period_min} мин — в группу они будут "
            "постить реже заданного периода. Добавьте аккаунтов или уменьшите паузу."
        )
    return "\n".join(lines)


def _human_minutes(m: float) -> str:
    if m < 60:
        return f"{int(round(m))} мин"
    h, mm = divmod(int(round(m)), 60)
    return f"{h} ч {mm} мин" if mm else f"{h} ч"


def progress_block(items: list[tuple[object, object]], *, interval_label: str) -> str:
    """Блок «Группы» для сводки §10.5. items — [(рассылка, CampaignStats)]."""
    lines = ["<b>📢 Группы</b>"]
    for c, s in items:
        line = (
            f"«{escape(c.name)}» #{c.id}: постов за {interval_label} — {num(s.posts_since)}, "
            f"за сутки — {num(s.posts_24h)}; активных групп {s.by_state.get('active', 0)}"
        )
        if s.problems:
            line += f", проблемных {s.problems}"
        lines.append(line)
    return "\n".join(lines)
