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
