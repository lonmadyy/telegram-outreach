"""antiflood (paused_reason + flood settings) + invited_participants.

ARCHITECTURE.md §5.3 (глобальная пауза, адаптивный интервал), §19 #11.

Revision ID: 0004_antiflood_participants
Revises: 0003_spam_check_history
Create Date: 2026-05-31
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import insert as pg_insert

revision: str = "0004_antiflood_participants"
down_revision: Union[str, None] = "0003_spam_check_history"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Дефолты антифлуд-настроек (§5.3). Меняются через /set без передеплоя.
_FLOOD_SETTINGS = [
    ("flood_window_quorum_sec", "1800", "Окно (сек) кворума глобальной паузы (§5.3)"),
    ("flood_window_adaptive_sec", "3600", "Окно (сек) для адаптивного интервала (§5.3)"),
    ("flood_adaptive_hold_sec", "3600", "Длительность (сек) адаптивного интервала (§5.3)"),
    ("flood_interval_min_sec", "450", "Мин. интервал (сек) при недавнем флуде (§5.3)"),
    ("flood_interval_max_sec", "720", "Макс. интервал (сек) при недавнем флуде (§5.3)"),
]

_FLOOD_KEYS = "(" + ",".join(f"'{k}'" for k, _, _ in _FLOOD_SETTINGS) + ")"


def upgrade() -> None:
    # 1. Причина паузы кампании — для авто-возобновления флуд-пауз (§5.3).
    op.add_column(
        "campaigns",
        sa.Column("paused_reason", sa.String(32), nullable=True),
    )

    # 2. DB-снимок приглашённых НАМИ участников целевых чатов (§19 #11).
    op.create_table(
        "invited_participants",
        sa.Column("chat_id", sa.BigInteger(), primary_key=True),
        sa.Column("user_id", sa.BigInteger(), primary_key=True),
        sa.Column(
            "campaign_id",
            sa.Integer(),
            sa.ForeignKey("campaigns.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "invited_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
    )
    op.create_index("idx_invited_chat", "invited_participants", ["chat_id"])

    # 3. Дефолты flood-настроек (idempotent — не перетираем существующие).
    settings_tbl = sa.table(
        "settings",
        sa.column("key", sa.String),
        sa.column("value", sa.Text),
        sa.column("value_type", sa.String),
        sa.column("description", sa.Text),
    )
    for key, value, desc in _FLOOD_SETTINGS:
        stmt = (
            pg_insert(settings_tbl)
            .values(key=key, value=value, value_type="int", description=desc)
            .on_conflict_do_nothing(index_elements=["key"])
        )
        op.execute(stmt)


def downgrade() -> None:
    op.execute(f"DELETE FROM settings WHERE key IN {_FLOOD_KEYS}")
    op.drop_index("idx_invited_chat", table_name="invited_participants")
    op.drop_table("invited_participants")
    op.drop_column("campaigns", "paused_reason")
