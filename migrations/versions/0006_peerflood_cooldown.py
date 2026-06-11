"""Настройки: кулдаун PeerFlood-карантина + ночная пауза спам-чека.

ARCHITECTURE.md §6.5 (кулдаун: no_limits не снимает spam_blocked сразу после
PeerFlood), §7.1 (не опрашивать SpamBot в тихие часы). Схема НЕ меняется —
только сидируются 2 ключа настроек (idempotent, как в 0004).

Revision ID: 0006_peerflood_cooldown
Revises: 0005_account_pause_reason
Create Date: 2026-06-12
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import insert as pg_insert

revision: str = "0006_peerflood_cooldown"
down_revision: Union[str, None] = "0005_account_pause_reason"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# (key, value, value_type, description). Меняются через /set без передеплоя.
_SETTINGS = [
    (
        "peerflood_cooldown_hours",
        "3",
        "int",
        "Кулдаун (ч): no_limits не снимает spam_blocked после недавнего PeerFlood (§6.5). 0 = выкл",
    ),
    (
        "spamcheck_quiet_pause",
        "true",
        "bool",
        "Не опрашивать SpamBot в тихие часы (§7.1)",
    ),
]

_KEYS = "(" + ",".join(f"'{k}'" for k, _, _, _ in _SETTINGS) + ")"


def upgrade() -> None:
    settings_tbl = sa.table(
        "settings",
        sa.column("key", sa.String),
        sa.column("value", sa.Text),
        sa.column("value_type", sa.String),
        sa.column("description", sa.Text),
    )
    for key, value, value_type, desc in _SETTINGS:
        stmt = (
            pg_insert(settings_tbl)
            .values(key=key, value=value, value_type=value_type, description=desc)
            .on_conflict_do_nothing(index_elements=["key"])
        )
        op.execute(stmt)


def downgrade() -> None:
    op.execute(f"DELETE FROM settings WHERE key IN {_KEYS}")
