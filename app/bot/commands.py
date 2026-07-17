"""Команды бота для нативной кнопки «Меню». ARCHITECTURE.md §10.2, §10.6.

set_my_commands наполняет выпадающий список у синей кнопки «Меню» слева от поля
ввода — список доступных команд с описаниями. Роутинг команд не меняется.
"""

from __future__ import annotations

from aiogram import Bot
from aiogram.types import BotCommand

# Порядок = как показывается в меню. Только пользовательские команды (§10.2).
BOT_COMMANDS: list[BotCommand] = [
    BotCommand(command="start", description="Главное меню"),
    BotCommand(command="status", description="Статус кампаний и аккаунтов"),
    BotCommand(command="accounts", description="Список аккаунтов"),
    BotCommand(command="add_account", description="Добавить userbot-аккаунт"),
    BotCommand(command="remove_account", description="Удалить аккаунт (phone|id)"),
    BotCommand(command="templates", description="Шаблоны сообщений"),
    BotCommand(command="new_template", description="Создать шаблон"),
    BotCommand(command="new_campaign", description="Новая кампания (рассылка/инвайт)"),
    BotCommand(command="campaigns", description="Список кампаний"),
    BotCommand(command="pause", description="Пауза кампании (id)"),
    BotCommand(command="resume", description="Возобновить кампанию (id)"),
    BotCommand(command="stop", description="Остановить кампанию (id)"),
    BotCommand(command="reactivate", description="Вернуть отменённую кампанию (id)"),
    BotCommand(command="set_proxy", description="Задать/сменить прокси аккаунта"),
    BotCommand(command="spamcheck", description="Проверка через SpamBot"),
    BotCommand(command="floodwait", description="Аккаунты в FloodWait"),
    BotCommand(command="export_report", description="CSV-отчёт по кампании"),
    BotCommand(command="export_log", description="Выгрузить логи"),
    BotCommand(command="settings", description="Настройки"),
    BotCommand(command="cancel", description="Отменить текущий сценарий"),
]


async def setup_bot_commands(bot: Bot) -> None:
    """Зарегистрировать список команд (нативная кнопка «Меню»). Best-effort."""
    await bot.set_my_commands(BOT_COMMANDS)
