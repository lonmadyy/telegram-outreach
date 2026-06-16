"""Создание Telethon-клиентов. ARCHITECTURE.md §11.1, §10.3.

Один аккаунт = один `.session` файл в `data/sessions/<phone>.session` +
опциональный прокси из `accounts.proxy_url`.
"""

from __future__ import annotations

import base64
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from telethon import TelegramClient
from telethon.network import ConnectionTcpMTProxyRandomizedIntermediate

from app.config import settings

_NON_DIGIT = re.compile(r"\D")
_HEX_RE = re.compile(r"[0-9a-fA-F]+")
# mtproto://SECRET@HOST:PORT — secret разбираем regex'ом, а не urlparse: base64-secret
# содержит '/' и '+', которые ломают urlparse (secret стоит в позиции username).
_MTPROTO_RE = re.compile(
    r"mtproto://(?P<secret>[^@]+)@(?P<host>[^:/@]+):(?P<port>\d+)/?$", re.IGNORECASE
)


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


def session_file_paths(sessions_path: str, phone: str) -> list[str]:
    """Пути к файлам сессии Telethon для телефона: `.session` и `.session-journal`.

    Чистая функция (для удаления файлов при /remove_account). Имя — цифры телефона,
    как в `session_path_for`.
    """
    digits = _NON_DIGIT.sub("", phone or "")
    base = Path(sessions_path) / digits
    return [f"{base}.session", f"{base}.session-journal"]


def parse_account_ref(arg: str | None) -> tuple[str, str | int] | None:
    """Разбор аргумента команды (`/remove_account`): телефон или числовой id.

    `+номер` или длинные цифры (>= 7) → `("phone", нормализованный_телефон)`;
    короткие цифры (< 7) → `("id", int)`; пусто/мусор → `None`. Чистая функция.
    """
    if not arg:
        return None
    a = arg.strip().lstrip("@")
    if not a:
        return None
    if a.startswith("+"):
        phone = normalize_phone(a)
        return ("phone", phone) if len(phone) >= 8 else None
    if a.isdigit():
        if len(a) < 7:
            return ("id", int(a))
        return ("phone", normalize_phone(a))
    return None


@dataclass(frozen=True)
class ProxyConfig:
    """Разобранный прокси для передачи в `TelegramClient`.

    `proxy` — значение аргумента `proxy=` (dict для socks5 / кортеж
    `(host, port, secret_hex)` для MTProto). `connection` — connection-класс
    Telethon (только для MTProto, иначе None — обычный TCP).
    """

    proxy: dict | tuple
    connection: type | None = None


def _normalize_mtproto_secret(secret: str) -> str:
    """Secret MTProto-прокси → hex-строка, как ожидает Telethon.

    Принимает secret в hex (как есть) либо base64/base64url (декодирует в hex).
    Отклоняет fake-TLS секреты (первый байт 0xEE) — их транспорт Telethon не
    поддерживает. Замечание: secret из одних hex-символов трактуется как hex,
    а не base64 — это неустранимая неоднозначность форматов.
    """
    s = (secret or "").strip()
    if not s:
        raise ValueError("MTProto secret пустой")
    if _HEX_RE.fullmatch(s) and len(s) % 2 == 0:
        hexs = s.lower()
    else:
        s2 = s.replace("-", "+").replace("_", "/")
        s2 += "=" * (-len(s2) % 4)
        try:
            raw = base64.b64decode(s2)
        except Exception as e:  # noqa: BLE001 — любой сбой декодирования = невалидный secret
            raise ValueError(f"Невалидный MTProto secret: {secret!r}") from e
        if not raw:
            raise ValueError(f"Невалидный MTProto secret: {secret!r}")
        hexs = raw.hex()
    if hexs.startswith("ee"):
        raise ValueError(
            "MTProto fake-TLS (secret с префиксом ee) не поддерживается Telethon — "
            "используйте обычный MTProxy-secret или SOCKS5."
        )
    return hexs


def parse_proxy(proxy_url: str | None) -> ProxyConfig | None:
    """Разбор строки `accounts.proxy_url` в конфиг прокси для Telethon.

    Поддерживаемые форматы:
      • `socks5://[user:pass@]host:port`                         — SOCKS5 (python-socks);
      • `mtproto://secret@host:port`                             — MTProto-прокси Telegram;
      • `tg://proxy?server=host&port=port&secret=secret`         — родная ссылка Telegram.
    Secret (для MTProto) принимается в hex или base64. Возвращает None для пустой
    строки, бросает ValueError для невалидного ввода/неизвестной схемы.
    """
    if not proxy_url:
        return None
    raw = proxy_url.strip()
    if not raw:
        return None
    p = urlparse(raw)
    scheme = p.scheme.lower()

    if scheme == "socks5":
        if not p.hostname or not p.port:
            raise ValueError(f"Невалидный socks5-URL (нужны host/port): {proxy_url!r}")
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
        return ProxyConfig(proxy=proxy)

    if scheme == "mtproto":
        m = _MTPROTO_RE.fullmatch(raw)
        if not m:
            raise ValueError(
                f"Невалидный MTProto-URL (нужно mtproto://secret@host:port): {proxy_url!r}"
            )
        return ProxyConfig(
            proxy=(
                m.group("host"),
                int(m.group("port")),
                _normalize_mtproto_secret(m.group("secret")),
            ),
            connection=ConnectionTcpMTProxyRandomizedIntermediate,
        )

    if scheme == "tg" and (p.netloc or "").lower() == "proxy":
        q = parse_qs(p.query)
        host = (q.get("server") or [None])[0]
        port_s = (q.get("port") or [None])[0]
        secret = (q.get("secret") or [None])[0]
        if not host or not port_s or not secret:
            raise ValueError(
                f"Невалидная tg://-ссылка (нужны server, port, secret): {proxy_url!r}"
            )
        try:
            port = int(port_s)
        except ValueError as e:
            raise ValueError(f"Невалидный port в tg://-ссылке: {port_s!r}") from e
        return ProxyConfig(
            proxy=(host, port, _normalize_mtproto_secret(secret)),
            connection=ConnectionTcpMTProxyRandomizedIntermediate,
        )

    raise ValueError(
        f"Неподдерживаемая схема прокси: {p.scheme!r} "
        f"(поддерживаются socks5://, mtproto://, tg://proxy?...)"
    )


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
    cfg = parse_proxy(proxy_url)
    proxy_kwargs: dict = {}
    if cfg is not None:
        proxy_kwargs["proxy"] = cfg.proxy
        if cfg.connection is not None:
            proxy_kwargs["connection"] = cfg.connection
    return TelegramClient(
        session=session_path_for(phone),
        api_id=settings.tg_api_id,
        api_hash=settings.tg_api_hash,
        **proxy_kwargs,
        device_model=device_model,
        system_version=system_version,
        app_version=app_version,
        lang_code=lang_code,
        system_lang_code=system_lang_code,
        connection_retries=3,
        retry_delay=2,
    )
