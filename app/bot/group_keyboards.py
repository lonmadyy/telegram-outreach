"""Клавиатуры раздела «Рассылка по группам». ARCHITECTURE.md §22.9.

Префиксы callback_data: `g:` — рассылки, `gw:` — мастер новой рассылки, `ge:` — правка
рассылки, `gt:` — шаблоны для групп, `role:` — роли аккаунтов.
"""

from __future__ import annotations

from aiogram.types import InlineKeyboardButton as B
from aiogram.types import InlineKeyboardMarkup

CANCEL = B(text="Отмена", callback_data="cancel")


def _kb(rows: list[list[B]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=rows)


def groups_menu_kb() -> InlineKeyboardMarkup:
    return _kb(
        [
            [
                B(text="📋 Мои рассылки", callback_data="g:list"),
                B(text="➕ Новая рассылка", callback_data="g:new"),
            ],
            [
                B(text="📝 Шаблоны для групп", callback_data="gt:list"),
                B(text="👥 Аккаунты и роли", callback_data="g:roles"),
            ],
            [B(text="☰ Меню", callback_data="menu:back")],
        ]
    )


def campaigns_list_kb(campaigns: list) -> InlineKeyboardMarkup:
    rows = [
        [B(text=f"Открыть #{c.id} · {c.name}"[:60], callback_data=f"g:open:{c.id}")]
        for c in campaigns
    ]
    rows.append([B(text="➕ Новая рассылка", callback_data="g:new")])
    rows.append([B(text="⬅ Рассылка по группам", callback_data="menu:groups")])
    return _kb(rows)


def campaign_kb(c) -> InlineKeyboardMarkup:
    cid = c.id
    first: list[B] = []
    if c.status == "running":
        first.append(B(text="⏸ Пауза", callback_data=f"g:pause:{cid}"))
    else:
        first.append(B(text="▶ Продолжить", callback_data=f"g:resume:{cid}"))
    if c.status != "stopped":
        first.append(B(text="⏹ Остановить", callback_data=f"g:stop:{cid}"))
    return _kb(
        [
            first,
            [
                B(text="📋 Группы", callback_data=f"g:tg:{cid}:0"),
                B(text="♻ Вернуть проблемные", callback_data=f"g:retry:{cid}"),
            ],
            [
                B(text="⏱ Интервалы", callback_data=f"g:iv:{cid}"),
                B(text="📝 Шаблон", callback_data=f"g:tpl:{cid}"),
            ],
            [
                B(text="➕ Группы", callback_data=f"g:add:{cid}"),
                B(text="👤 Аккаунты", callback_data=f"g:acc:{cid}"),
            ],
            [
                B(text="📊 CSV", callback_data=f"g:csv:{cid}"),
                B(text="🔄 Обновить", callback_data=f"g:open:{cid}"),
            ],
            [B(text="⬅ Все рассылки", callback_data="g:list")],
        ]
    )


def stop_confirm_kb(cid: int) -> InlineKeyboardMarkup:
    return _kb(
        [
            [
                B(text="⏹ Да, остановить", callback_data=f"g:stopok:{cid}"),
                B(text="Назад", callback_data=f"g:open:{cid}"),
            ]
        ]
    )


def targets_nav_kb(cid: int, page: int, pages: int) -> InlineKeyboardMarkup:
    nav: list[B] = []
    if page > 0:
        nav.append(B(text="◀", callback_data=f"g:tg:{cid}:{page - 1}"))
    nav.append(B(text=f"{page + 1}/{max(pages, 1)}", callback_data=f"g:tg:{cid}:{page}"))
    if page + 1 < pages:
        nav.append(B(text="▶", callback_data=f"g:tg:{cid}:{page + 1}"))
    return _kb([nav, [B(text="⬅ К рассылке", callback_data=f"g:open:{cid}")]])


def back_to_campaign_kb(cid: int) -> InlineKeyboardMarkup:
    return _kb([[B(text="⬅ К рассылке", callback_data=f"g:open:{cid}")]])


def template_picker_kb(templates: list, prefix: str) -> InlineKeyboardMarkup:
    rows = [
        [B(text=f"📄 {t.name}"[:60], callback_data=f"{prefix}{t.id}")] for t in templates
    ]
    rows.append([CANCEL])
    return _kb(rows)


def account_label(acc) -> str:
    name = f"@{acc.username}" if acc.username else acc.phone
    return f"#{acc.id} · {name}"


def accounts_select_kb(accounts: list, selected: set[int]) -> InlineKeyboardMarkup:
    rows = [
        [
            B(
                text=("✅ " if a.id in selected else "⬜ ") + account_label(a),
                callback_data=f"gw:acc:{a.id}",
            )
        ]
        for a in accounts
    ]
    rows.append(
        [
            B(text="Выбрать все", callback_data="gw:accall"),
            B(text="🔄 Обновить", callback_data="gw:accrefresh"),
        ]
    )
    rows.append([B(text="Далее ➡", callback_data="gw:accok"), CANCEL])
    return _kb(rows)


def no_accounts_kb() -> InlineKeyboardMarkup:
    return _kb(
        [
            [B(text="👥 Аккаунты и роли", callback_data="g:roles")],
            [B(text="🔄 Проверить снова", callback_data="gw:accrefresh"), CANCEL],
        ]
    )


def distribution_kb(tagged: int) -> InlineKeyboardMarkup:
    manual = "✍ Вручную по меткам #id" + (f" ({tagged})" if tagged else "")
    return _kb(
        [
            [B(text="⚖ Поровну", callback_data="gw:dist:even")],
            [B(text="🔁 Каждый аккаунт во все группы", callback_data="gw:dist:all")],
            [B(text=manual, callback_data="gw:dist:manual")],
            [CANCEL],
        ]
    )


def intervals_kb(prefix: str, *, done_text: str) -> InlineKeyboardMarkup:
    return _kb(
        [
            [B(text=done_text, callback_data=f"{prefix}:iv:ok")],
            [
                B(text="🔁 Период повтора", callback_data=f"{prefix}:iv:period"),
                B(text="↔ Пауза", callback_data=f"{prefix}:iv:gap"),
            ],
            [B(text="🕘 Окно работы", callback_data=f"{prefix}:iv:window"), CANCEL],
        ]
    )


def start_kb() -> InlineKeyboardMarkup:
    return _kb([[B(text="▶ Запустить", callback_data="gw:start"), CANCEL]])


def campaign_accounts_kb(cid: int, accounts: list, members: set[int]) -> InlineKeyboardMarkup:
    rows = [
        [
            B(
                text=("✅ " if a.id in members else "⬜ ") + account_label(a),
                callback_data=f"g:acct:{cid}:{a.id}",
            )
        ]
        for a in accounts
    ]
    rows.append([B(text="⬅ К рассылке", callback_data=f"g:open:{cid}")])
    return _kb(rows)


def group_templates_kb(templates: list) -> InlineKeyboardMarkup:
    rows = []
    for t in templates:
        rows.append(
            [
                B(text=f"👁 #{t.id} · {t.name}"[:50], callback_data=f"gt:view:{t.id}"),
                B(text="🗑", callback_data=f"gt:del:{t.id}"),
            ]
        )
    rows.append([B(text="➕ Новый шаблон", callback_data="gt:new")])
    rows.append([B(text="⬅ Рассылка по группам", callback_data="menu:groups")])
    return _kb(rows)


def template_delete_kb(tid: int) -> InlineKeyboardMarkup:
    return _kb(
        [
            [
                B(text="🗑 Да, удалить", callback_data=f"gt:delok:{tid}"),
                B(text="Назад", callback_data="gt:list"),
            ]
        ]
    )


def roles_kb(accounts: list) -> InlineKeyboardMarkup:
    rows = []
    for a in accounts:
        rows.append(
            [
                B(
                    text=f"#{a.id} 💬 Личка {'✅' if a.role_dm else '⬜'}",
                    callback_data=f"role:dm:{a.id}",
                ),
                B(
                    text=f"#{a.id} 📢 Группы {'✅' if a.role_groups else '⬜'}",
                    callback_data=f"role:gr:{a.id}",
                ),
            ]
        )
    rows.append([B(text="⬅ Рассылка по группам", callback_data="menu:groups")])
    return _kb(rows)
