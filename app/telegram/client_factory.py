"""Создание Telethon-клиентов. ARCHITECTURE.md §11.1, §10.3.

Один аккаунт = один `.session` файл в `data/sessions/<phone>.session` +
опциональный прокси из `accounts.proxy_url`.
"""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import urlparse

from telethon import TelegramClient

from app.config import settings

_NON_DIGIT = re.compile(r"\D")


def normalize_phone(raw: str) -> str:
    """`+7 (778) 878-66-14` -> `+77788786614`.

    Убирает все нецифровые символы и принудительно добавляет `+` в начало.
    Возвращает пустую строку, если цифр нет.
    Должна давать строку, проходящую CHECK `^\\+?[0-9]{7,15}$` из §4.1.
    """
    digits = _NON_DIGIT.sub("", raw or "")
    if not digits:
        return ""
    return "+" + digits


def session_path_for(phone: str) -> str:
    """Полный путь к session-файлу без `.session` (Telethon добавит сам).

    Имя файла = телефон без `+`, только цифры — чтобы избежать любых проблем
    с экранированием в shell/file system.
    """
    digits = _NON_DIGIT.sub("", phone or "")
    return str(Path(settings.sessions_path) / digits)


def parse_proxy(proxy_url: str | None) -> dict | None:
    """`socks5://user:pass@host:port` -> dict для Telethon (python-socks).

    Поддерживается только socks5 в MVP. MTProto-прокси могут быть добавлены позже.
    Возвращает None если proxy_url пустой/невалидный.
    """
    if not proxy_url:
        return None
    p = urlparse(proxy_url)
    if p.scheme.lower() != "socks5":
        raise ValueError(f"Unsupported proxy scheme: {p.scheme!r} (only socks5 in MVP)")
    if not p.hostname or not p.port:
        raise ValueError(f"Invalid proxy URL (host/port required): {proxy_url!r}")
    proxy: dict = {
        "proxy_type": "socks5",
        "addr": p.hostname,
        "port": p.port,
        "rdns": True,
    }
    if p.username:
        proxy["username"] = p.username
    if p.password:
        proxy["password"] = p.password
    return proxy


def create_client(
    *,
    phone: str,
    proxy_url: str | None = None,
    device_model: str = "PC 64bit",
    system_version: str = "Linux",
    app_version: str = "1.0.0",
    lang_code: str = "en",
    system_lang_code: str = "en",
) -> TelegramClient:
    """Создаёт (но не подключает) Telethon-клиент. Подключение и авторизация — отдельно."""
    return TelegramClient(
        session=session_path_for(phone),
        api_id=settings.tg_api_id,
        api_hash=settings.tg_api_hash,
        proxy=parse_proxy(proxy_url),
        device_model=device_model,
        system_version=system_version,
        app_version=app_version,
        lang_code=lang_code,
        system_lang_code=system_lang_code,
        connection_retries=3,
        retry_delay=2,
    )
