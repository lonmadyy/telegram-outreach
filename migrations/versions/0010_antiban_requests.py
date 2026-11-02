"""accounts.device, campaigns.source_parse_job_id, chat_members (+ загрузки).

ARCHITECTURE.md §4.1 (device), §4.4 (source_parse_job_id), §4.11, §21.

Revision ID: 0010_antiban_requests
Revises: 0009_parse_jobs
Create Date: 2026-11-02
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0010_antiban_requests"
down_revision: Union[str, None] = "0009_parse_jobs"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Постоянный профиль устройства аккаунта (§21.2). NULL = прежние общие параметры.
    op.add_column("accounts", sa.Column("device", JSONB(), nullable=True))
    # Кампания из итога /parse → получатели берутся из списков исходных групп (§21.1).
    op.add_column(
        "campaigns",
        sa.Column(
            "source_parse_job_id",
            sa.Integer(),
            sa.ForeignKey("parse_jobs.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    # Снимок участников целевых чатов инвайт-кампаний (§21.4).
    op.create_table(
        "chat_member_loads",
        sa.Column("chat_id", sa.BigInteger(), primary_key=True),
        sa.Column("loaded_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("members", sa.Integer(), nullable=False),
    )
    op.create_table(
        "chat_members",
        sa.Column("chat_id", sa.BigInteger(), primary_key=True),
        sa.Column("user_id", sa.BigInteger(), primary_key=True),
    )


def downgrade() -> None:
    op.drop_table("chat_members")
    op.drop_table("chat_member_loads")
    op.drop_column("campaigns", "source_parse_job_id")
    op.drop_column("accounts", "device")
