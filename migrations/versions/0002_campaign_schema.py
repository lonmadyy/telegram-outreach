"""Schema for campaigns: templates, campaigns, tasks, processed_clients + FK from logs.

ARCHITECTURE.md §4.3, §4.4, §4.5, §4.6, §4.8 (FK добавления к campaign_id/task_id).

Revision ID: 0002_campaign_schema
Revises: 0001_initial_schema
Create Date: 2026-05-28
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002_campaign_schema"
down_revision: Union[str, None] = "0001_initial_schema"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


CAMPAIGN_TYPE_VALUES = ("message", "invite")
CAMPAIGN_STATUS_VALUES = ("pending", "running", "paused", "done", "failed", "cancelled")
TASK_STATUS_VALUES = ("queued", "in_progress", "done", "failed", "skipped")
RESULT_CODE_VALUES = (
    "ok",
    "flood_wait",
    "peer_flood",
    "privacy_restricted",
    "not_mutual_contact",
    "not_found",
    "already_member",
    "channel_private",
    "too_many_channels",
    "banned_in_channel",
    "deactivated",
    "other_error",
)


def upgrade() -> None:
    # ENUM types
    op.execute(
        "CREATE TYPE campaign_type AS ENUM ("
        + ", ".join(f"'{v}'" for v in CAMPAIGN_TYPE_VALUES)
        + ")"
    )
    op.execute(
        "CREATE TYPE campaign_status AS ENUM ("
        + ", ".join(f"'{v}'" for v in CAMPAIGN_STATUS_VALUES)
        + ")"
    )
    op.execute(
        "CREATE TYPE task_status AS ENUM ("
        + ", ".join(f"'{v}'" for v in TASK_STATUS_VALUES)
        + ")"
    )
    op.execute(
        "CREATE TYPE result_code AS ENUM ("
        + ", ".join(f"'{v}'" for v in RESULT_CODE_VALUES)
        + ")"
    )

    # templates (§4.3)
    op.create_table(
        "templates",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(128), unique=True, nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column(
            "variables",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
    )

    # campaigns (§4.4)
    op.create_table(
        "campaigns",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "type",
            postgresql.ENUM(*CAMPAIGN_TYPE_VALUES, name="campaign_type", create_type=False),
            nullable=False,
        ),
        sa.Column(
            "template_id",
            sa.Integer(),
            sa.ForeignKey("templates.id"),
            nullable=True,
        ),
        sa.Column("target_chat", sa.String(255), nullable=True),
        sa.Column("target_chat_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "status",
            postgresql.ENUM(*CAMPAIGN_STATUS_VALUES, name="campaign_status", create_type=False),
            nullable=False,
            server_default="pending",
        ),
        sa.Column("total_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("sent_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("skipped_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("failed_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column(
            "resend_old",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("FALSE"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by_user_id", sa.BigInteger(), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
    )
    op.create_index(
        "idx_campaigns_status",
        "campaigns",
        ["status"],
        postgresql_where=sa.text("status IN ('running', 'paused')"),
    )

    # tasks (§4.5)
    op.create_table(
        "tasks",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column(
            "campaign_id",
            sa.Integer(),
            sa.ForeignKey("campaigns.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("username", sa.String(64), nullable=False),
        sa.Column(
            "status",
            postgresql.ENUM(*TASK_STATUS_VALUES, name="task_status", create_type=False),
            nullable=False,
            server_default="queued",
        ),
        sa.Column(
            "assigned_account_id",
            sa.Integer(),
            sa.ForeignKey("accounts.id"),
            nullable=True,
        ),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("last_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "result_code",
            postgresql.ENUM(*RESULT_CODE_VALUES, name="result_code", create_type=False),
            nullable=True,
        ),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "idx_tasks_queue",
        "tasks",
        ["campaign_id", "status", "locked_until"],
        postgresql_where=sa.text("status = 'queued'"),
    )
    op.create_index(
        "idx_tasks_campaign_username",
        "tasks",
        ["campaign_id", "username"],
        unique=True,
    )

    # processed_clients (§4.6)
    op.create_table(
        "processed_clients",
        sa.Column("username", sa.String(64), primary_key=True),
        sa.Column("last_action", sa.String(16), nullable=False),
        sa.Column(
            "last_result_code",
            postgresql.ENUM(*RESULT_CODE_VALUES, name="result_code", create_type=False),
            nullable=False,
        ),
        sa.Column(
            "first_processed_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.Column(
            "last_processed_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.Column(
            "account_id",
            sa.Integer(),
            sa.ForeignKey("accounts.id"),
            nullable=True,
        ),
        sa.Column(
            "campaign_id",
            sa.Integer(),
            sa.ForeignKey("campaigns.id"),
            nullable=True,
        ),
    )
    op.create_index(
        "idx_processed_last_at", "processed_clients", ["last_processed_at"]
    )

    # logs: добавить FK на campaigns и tasks (в MVP-1 эти колонки были BIGINT без FK).
    # Сначала меняем тип campaign_id с BIGINT на INT (соответствует campaigns.id SERIAL).
    op.execute(
        "ALTER TABLE logs ALTER COLUMN campaign_id TYPE integer USING campaign_id::integer"
    )
    op.create_foreign_key(
        "logs_campaign_id_fkey",
        "logs",
        "campaigns",
        ["campaign_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        "logs_task_id_fkey",
        "logs",
        "tasks",
        ["task_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("idx_logs_campaign_ts", "logs", ["campaign_id", "ts"])


def downgrade() -> None:
    op.drop_index("idx_logs_campaign_ts", table_name="logs")
    op.drop_constraint("logs_task_id_fkey", "logs", type_="foreignkey")
    op.drop_constraint("logs_campaign_id_fkey", "logs", type_="foreignkey")
    op.execute("ALTER TABLE logs ALTER COLUMN campaign_id TYPE bigint")

    op.drop_index("idx_processed_last_at", table_name="processed_clients")
    op.drop_table("processed_clients")

    op.drop_index("idx_tasks_campaign_username", table_name="tasks")
    op.drop_index("idx_tasks_queue", table_name="tasks")
    op.drop_table("tasks")

    op.drop_index("idx_campaigns_status", table_name="campaigns")
    op.drop_table("campaigns")

    op.drop_table("templates")

    op.execute("DROP TYPE result_code")
    op.execute("DROP TYPE task_status")
    op.execute("DROP TYPE campaign_status")
    op.execute("DROP TYPE campaign_type")
