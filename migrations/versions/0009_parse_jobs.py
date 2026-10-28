"""parse_jobs + parse_results — парсер групп (/parse).

ARCHITECTURE.md §4.10, §20.

Revision ID: 0009_parse_jobs
Revises: 0008_seed_extra_settings
Create Date: 2026-10-28
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0009_parse_jobs"
down_revision: Union[str, None] = "0008_seed_extra_settings"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "parse_jobs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "account_id",
            sa.Integer(),
            sa.ForeignKey("accounts.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="running"),
        sa.Column("groups", JSONB(), nullable=False),
        sa.Column("options", JSONB(), nullable=False),
        sa.Column("report", JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("dropped", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_table(
        "parse_results",
        sa.Column(
            "job_id",
            sa.Integer(),
            sa.ForeignKey("parse_jobs.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("group_idx", sa.Integer(), primary_key=True),
        sa.Column("username", sa.String(64), primary_key=True),
    )


def downgrade() -> None:
    op.drop_table("parse_results")
    op.drop_table("parse_jobs")
