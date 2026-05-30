"""Юнит-тесты маппинга ошибок Telethon → result_code. ARCHITECTURE.md §16.1."""

from __future__ import annotations

from telethon.errors import (
    ChannelPrivateError,
    ChatAdminRequiredError,
    InputUserDeactivatedError,
    UserAlreadyParticipantError,
    UserDeactivatedError,
    UserNotMutualContactError,
    UserPrivacyRestrictedError,
)

from app.db.models import ResultCode
from app.telegram import errors as err


def test_classify_already_member_skip():
    assert err.classify_error(UserAlreadyParticipantError(request=None)) == (
        ResultCode.already_member,
        "skip",
    )


def test_classify_not_mutual_contact_skip():
    assert err.classify_error(UserNotMutualContactError(request=None)) == (
        ResultCode.not_mutual_contact,
        "skip",
    )


def test_classify_privacy_restricted_skip():
    assert err.classify_error(UserPrivacyRestrictedError(request=None)) == (
        ResultCode.privacy_restricted,
        "skip",
    )


def test_classify_channel_private_fatal():
    assert err.classify_error(ChannelPrivateError(request=None)) == (
        ResultCode.channel_private,
        "fatal",
    )


def test_classify_unknown_returns_none():
    assert err.classify_error(ValueError("nope")) is None


def test_account_scoped_invite_errors_membership():
    assert ChatAdminRequiredError in err.ACCOUNT_SCOPED_INVITE_ERRORS
    assert ChannelPrivateError in err.ACCOUNT_SCOPED_INVITE_ERRORS


def test_auth_key_unregistered_is_session_dead():
    # «The key is not registered in the system» = AuthKeyUnregisteredError (401),
    # подкласс UnauthorizedError → должен ловиться как SESSION_DEAD_ERRORS.
    from telethon.errors import AuthKeyUnregisteredError, UnauthorizedError

    assert err.SESSION_DEAD_ERRORS == (UnauthorizedError,)
    assert isinstance(AuthKeyUnregisteredError(request=None), err.SESSION_DEAD_ERRORS)


def test_floodwait_is_not_session_dead():
    # FloodWait не должен попадать в session-dead (это пауза, не смерть).
    from telethon.errors import FloodWaitError

    assert not isinstance(FloodWaitError(request=None), err.SESSION_DEAD_ERRORS)


def test_user_deactivated_is_session_dead_not_skip():
    # UserDeactivatedError = НАШ аккаунт удалён/забанен (401, подкласс
    # UnauthorizedError) → session-dead. В ERROR_MAP его быть НЕ должно (MVP-6 §16).
    assert err.classify_error(UserDeactivatedError(request=None)) is None
    assert isinstance(UserDeactivatedError(request=None), err.SESSION_DEAD_ERRORS)


def test_input_user_deactivated_is_skip():
    # InputUserDeactivatedError = удалён ПОЛУЧАТЕЛЬ (код 400) → skip, остаётся в мапе.
    assert err.classify_error(InputUserDeactivatedError(request=None)) == (
        ResultCode.deactivated,
        "skip",
    )
    assert not isinstance(
        InputUserDeactivatedError(request=None), err.SESSION_DEAD_ERRORS
    )
