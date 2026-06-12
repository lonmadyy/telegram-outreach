"""Реестр processed_clients: какие отказы помним глобально (§4.6).

Критичный инвариант: глобально запоминаем только target-НЕЗАВИСИМЫЕ структурные
отказы. Target-специфичные (бан в конкретном чате, уже-участник) запоминать
глобально НЕЛЬЗЯ — иначе аккаунт, забаненный в чате A, не будет приглашён в чат B.
"""

from __future__ import annotations

from app.db.models import ResultCode
from app.db.repositories.processed import (
    STRUCTURAL_SKIP_CODES,
    is_structural_skip,
)


def test_structural_codes_remembered() -> None:
    for rc in (
        ResultCode.privacy_restricted,
        ResultCode.not_found,
        ResultCode.deactivated,
        ResultCode.too_many_channels,
    ):
        assert is_structural_skip(rc) is True


def test_target_specific_codes_not_remembered() -> None:
    # Привязаны к конкретному чату — НЕ глобальный skip.
    assert is_structural_skip(ResultCode.banned_in_channel) is False
    assert is_structural_skip(ResultCode.already_member) is False


def test_transient_and_ok_not_remembered() -> None:
    # ok пишется отдельным путём (успех); flood/peer/прочие ошибки — повторяемы.
    for rc in (
        ResultCode.ok,
        ResultCode.other_error,
        ResultCode.flood_wait,
        ResultCode.peer_flood,
    ):
        assert is_structural_skip(rc) is False


def test_set_membership_explicit() -> None:
    assert ResultCode.privacy_restricted in STRUCTURAL_SKIP_CODES
    assert ResultCode.banned_in_channel not in STRUCTURAL_SKIP_CODES
    assert ResultCode.already_member not in STRUCTURAL_SKIP_CODES
