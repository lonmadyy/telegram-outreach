"""Inline-клавиатуры для бота. ARCHITECTURE.md §10.6."""

from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


def main_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Аккаунты", callback_data="menu:accounts")],
            [InlineKeyboardButton(text="Шаблоны", callback_data="menu:templates")],
            [InlineKeyboardButton(text="Новая кампания", callback_data="menu:new_campaign")],
            [InlineKeyboardButton(text="Статус", callback_data="menu:status")],
            [InlineKeyboardButton(text="Настройки", callback_data="menu:settings")],
        ]
    )


def cancel_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="Отмена", callback_data="cancel")]]
    )


def confirm_kb(yes_data: str, no_data: str = "cancel") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✓ Подтвердить", callback_data=yes_data),
                InlineKeyboardButton(text="Отмена", callback_data=no_data),
            ]
        ]
    )


def campaign_type_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Рассылка (DM)", callback_data="ctype:message")],
            [
                InlineKeyboardButton(
                    text="Инвайт в чат (доступно в MVP-4)",
                    callback_data="ctype:invite_disabled",
                )
            ],
            [InlineKeyboardButton(text="Отмена", callback_data="cancel")],
        ]
    )


def resend_decision_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Продолжить", callback_data="resend:no"),
            ],
            [
                InlineKeyboardButton(
                    text="Переотправить тем, кому >180 дней",
                    callback_data="resend:yes",
                ),
            ],
            [InlineKeyboardButton(text="Отмена", callback_data="cancel")],
        ]
    )


def templates_picker_kb(templates: list) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=t.name, callback_data=f"pick_tpl:{t.id}")]
        for t in templates
    ]
    rows.append([InlineKeyboardButton(text="Отмена", callback_data="cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)
