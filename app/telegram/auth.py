"""FSM-сессии авторизации userbot-аккаунтов. ARCHITECTURE.md §10.3, §16.1.

Состояние одной авторизации (телефон → код → 2FA → готово) живёт в памяти
процесса: TelegramClient остаётся подключённым между шагами бота. Если шаг
не приходит дольше TIMEOUT_SECONDS — сессия выбрасывается.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from enum import Enum

from telethon import TelegramClient
from telethon.errors import (
    FloodWaitError,
    PasswordHashInvalidError,
    PhoneCodeExpiredError,
    PhoneCodeInvalidError,
    PhoneNumberBannedError,
    PhoneNumberInvalidError,
    SessionPasswordNeededError,
)
from telethon.tl.types import User

from app.telegram.client_factory import create_client, normalize_phone


TIMEOUT_SECONDS = 10 * 60  # 10 минут на всю авторизацию


class AuthError(Exception):
    """Базовый класс ошибок авторизации, безопасных для показа пользователю."""


class InvalidPhoneError(AuthError):
    pass


class InvalidCodeError(AuthError):
    pass


class CodeExpiredError(AuthError):
    pass


class InvalidPasswordError(AuthError):
    pass


class PhoneBannedError(AuthError):
    pass


class FloodError(AuthError):
    def __init__(self, seconds: int) -> None:
        super().__init__(f"Flood wait {seconds}s")
        self.seconds = seconds


class AuthStage(str, Enum):
    awaiting_code = "awaiting_code"
    awaiting_password = "awaiting_password"
    awaiting_proxy = "awaiting_proxy"
    completed = "completed"


@dataclass
class AuthSession:
    phone: str
    client: TelegramClient
    stage: AuthStage = AuthStage.awaiting_code
    phone_code_hash: str | None = None
    proxy_url: str | None = None
    started_at: float = field(default_factory=time.time)
    me: User | None = None

    def is_expired(self) -> bool:
        return time.time() - self.started_at > TIMEOUT_SECONDS


class AuthSessionStore:
    """Хранилище активных авторизаций по admin user_id."""

    def __init__(self) -> None:
        self._sessions: dict[int, AuthSession] = {}
        self._lock = asyncio.Lock()

    async def get(self, user_id: int) -> AuthSession | None:
        async with self._lock:
            sess = self._sessions.get(user_id)
            if sess and sess.is_expired():
                await self._dispose(user_id)
                return None
            return sess

    async def set(self, user_id: int, session: AuthSession) -> None:
        async with self._lock:
            old = self._sessions.get(user_id)
            if old is not None and old.client is not session.client:
                await _safe_disconnect(old.client)
            self._sessions[user_id] = session

    async def clear(self, user_id: int) -> None:
        async with self._lock:
            await self._dispose(user_id)

    async def _dispose(self, user_id: int) -> None:
        sess = self._sessions.pop(user_id, None)
        if sess is not None:
            await _safe_disconnect(sess.client)


auth_store = AuthSessionStore()


async def _safe_disconnect(client: TelegramClient) -> None:
    try:
        if client.is_connected():
            await client.disconnect()
    except Exception:
        pass


async def start_login(phone: str, proxy_url: str | None = None) -> AuthSession:
    """Запускает SMS/Telegram-код. Возвращает свежий AuthSession."""
    phone = normalize_phone(phone)
    if not phone or len(phone) < 8:
        raise InvalidPhoneError("Слишком короткий номер")
    if len(phone) > 16:  # '+' + до 15 цифр (E.164)
        raise InvalidPhoneError("Слишком длинный номер")
    client = create_client(phone=phone, proxy_url=proxy_url)
    try:
        await client.connect()
    except Exception:
        await _safe_disconnect(client)
        raise

    try:
        sent = await client.send_code_request(phone)
    except PhoneNumberInvalidError as e:
        await _safe_disconnect(client)
        raise InvalidPhoneError("Неверный формат номера") from e
    except PhoneNumberBannedError as e:
        await _safe_disconnect(client)
        raise PhoneBannedError("Этот номер заблокирован Telegram") from e
    except FloodWaitError as e:
        await _safe_disconnect(client)
        raise FloodError(e.seconds) from e

    return AuthSession(
        phone=phone,
        client=client,
        stage=AuthStage.awaiting_code,
        phone_code_hash=sent.phone_code_hash,
        proxy_url=proxy_url,
    )


async def submit_code(session: AuthSession, code: str) -> bool:
    """Возвращает True, если авторизация завершена. False — нужен 2FA пароль."""
    try:
        me = await session.client.sign_in(
            phone=session.phone,
            code=code.strip(),
            phone_code_hash=session.phone_code_hash,
        )
    except PhoneCodeInvalidError as e:
        raise InvalidCodeError("Неверный код") from e
    except PhoneCodeExpiredError as e:
        raise CodeExpiredError("Код истёк, запросите авторизацию заново") from e
    except SessionPasswordNeededError:
        session.stage = AuthStage.awaiting_password
        return False
    except FloodWaitError as e:
        raise FloodError(e.seconds) from e

    if isinstance(me, User):
        session.me = me
    session.stage = AuthStage.awaiting_proxy
    return True


async def submit_password(session: AuthSession, password: str) -> None:
    """Завершает авторизацию с облачным паролем 2FA."""
    try:
        me = await session.client.sign_in(password=password)
    except PasswordHashInvalidError as e:
        raise InvalidPasswordError("Неверный облачный пароль") from e
    except FloodWaitError as e:
        raise FloodError(e.seconds) from e

    if isinstance(me, User):
        session.me = me
    session.stage = AuthStage.awaiting_proxy


async def finalize(session: AuthSession) -> None:
    """После сохранения в БД — отключаем клиент (worker подымет позже)."""
    session.stage = AuthStage.completed
    await _safe_disconnect(session.client)
