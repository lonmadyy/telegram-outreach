"""Initial schema: accounts, settings, logs.

ARCHITECTURE.md §4.1, §4.8, §4.9. ENUM types created manually so models can
opt-out of auto-creation (create_type=False) and reference them safely.

Revision ID: 0001_initial_schema
Revises:
Create Date: 2026-05-28
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001_initial_schema"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


ACCOUNT_STATUS_VALUES = ("warmup", "active", "pause", "spam_blocked", "dead", "disabled")
LOG_LEVEL_VALUES = ("debug", "info", "warning", "error", "critical")


DEFAULT_SETTINGS = [
    # (key, value, value_type, description)
    ("daily_dm_limit_warm", "40", "int", "DM лимит для прогретого аккаунта"),
    ("daily_invite_limit_warm", "100", "int", "Invite лимит для прогретого аккаунта"),
    ("daily_dm_limit_fresh", "10", "int", "DM лимит для аккаунта на warmup"),
    ("daily_invite_limit_fresh", "5", "int", "Invite лимит для аккаунта на warmup"),
    ("interval_min_sec", "300", "int", "Минимальная пауза между действиями (сек)"),
    ("interval_max_sec", "540", "int", "Максимальная пауза между действиями (сек)"),
    ("spamcheck_interval_sec", "240", "int", "Период опроса @SpamBot (сек)"),
    ("progress_notify_interval_sec", "1800", "int", "Период сводок в управляющий бот (сек)"),
    ("quiet_hours_start", "01:00", "str", "Начало тихих часов (HH:MM)"),
    ("quiet_hours_end", "07:00", "str", "Конец тихих часов (HH:MM)"),
    ("quiet_hours_timezone", "Europe/Minsk", "str", "Таймзона тихих часов"),
    ("peerflood_limit_ratio", "0.75", "float", "Коэффициент лимита после PeerFlood"),
    ("warmup_duration_hours", "48", "int", "Длительность warmup в часах"),
    ("adaptive_limit_reduction_days", "7", "int", "Срок действия адаптивного снижения лимита"),
]


def upgrade() -> None:
    # ENUM-типы
    op.execute(
        "CREATE TYPE account_status AS ENUM ("
        + ", ".join(f"'{v}'" for v in ACCOUNT_STATUS_VALUES)
        + ")"
    )
    op.execute(
        "CREATE TYPE log_level AS ENUM ("
        + ", ".join(f"'{v}'" for v in LOG_LEVEL_VALUES)
        + ")"
    )

    # accounts (§4.1)
    op.create_table(
        "accounts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("phone", sa.String(20), unique=True, nullable=False),
        sa.Column("tg_user_id", sa.BigInteger(), unique=True, nullable=True),
        sa.Column("username", sa.String(64), nullable=True),
        sa.Column("first_name", sa.String(128), nullable=True),
        sa.Column("session_path", sa.String(255), nullable=False),
        sa.Column("proxy_url", sa.String(255), nullable=True),
        sa.Column(
            "status",
            postgresql.ENUM(*ACCOUNT_STATUS_VALUES, name="account_status", create_type=False),
            nullable=False,
            server_default="warmup",
        ),
        sa.Column("spam_unlock_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("warmup_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("daily_sent", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("daily_invited", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column(
            "last_reset_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.Column("limit_reduced_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(r"phone ~ '^\+?[0-9]{7,15}$'", name="phone_format"),
    )
    op.create_index(
        "idx_accounts_status",
        "accounts",
        ["status"],
        postgresql_where=sa.text("status IN ('active', 'pause')"),
    )
    op.create_index(
        "idx_accounts_unlock",
        "accounts",
        ["spam_unlock_at"],
        postgresql_where=sa.text("spam_unlock_at IS NOT NULL"),
    )

    # settings (§4.9)
    op.create_table(
        "settings",
        sa.Column("key", sa.String(64), primary_key=True),
        sa.Column("value", sa.Text(), nullable=False),
        sa.Column("value_type", sa.String(16), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.Column("updated_by", sa.BigInteger(), nullable=True),
    )

    # Заполнение дефолтных настроек (§11.1)
    settings_table = sa.table(
        "settings",
        sa.column("key", sa.String),
        sa.column("value", sa.Text),
        sa.column("value_type", sa.String),
        sa.column("description", sa.Text),
    )
    op.bulk_insert(
        settings_table,
        [
            {"key": k, "value": v, "value_type": t, "description": d}
            for k, v, t, d in DEFAULT_SETTINGS
        ],
    )

    # logs (§4.8). campaign_id и task_id без FK — добавим в MVP-2.
    op.create_table(
        "logs",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column(
            "ts",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.Column(
            "level",
            postgresql.ENUM(*LOG_LEVEL_VALUES, name="log_level", create_type=False),
            nullable=False,
        ),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column(
            "account_id",
            sa.Integer(),
            sa.ForeignKey("accounts.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("campaign_id", sa.BigInteger(), nullable=True),
        sa.Column("task_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "payload",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("message", sa.Text(), nullable=False),
    )
    op.create_index("idx_logs_ts", "logs", ["ts"])
    op.create_index("idx_logs_account_ts", "logs", ["account_id", "ts"])
    op.create_index("idx_logs_event", "logs", ["event_type"])


def downgrade() -> None:
    op.drop_index("idx_logs_event", table_name="logs")
    op.drop_index("idx_logs_account_ts", table_name="logs")
    op.drop_index("idx_logs_ts", table_name="logs")
    op.drop_table("logs")

    op.drop_table("settings")

    op.drop_index("idx_accounts_unlock", table_name="accounts")
    op.drop_index("idx_accounts_status", table_name="accounts")
    op.drop_table("accounts")

    op.execute("DROP TYPE log_level")
    op.execute("DROP TYPE account_status")
