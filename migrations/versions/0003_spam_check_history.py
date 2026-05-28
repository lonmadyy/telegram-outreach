"""spam_check_history table. ARCHITECTURE.md §4.7.

Revision ID: 0003_spam_check_history
Revises: 0002_campaign_schema
Create Date: 2026-05-28
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0003_spam_check_history"
down_revision: Union[str, None] = "0002_campaign_schema"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "spam_check_history",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column(
            "account_id",
            sa.Integer(),
            sa.ForeignKey("accounts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "checked_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.Column("raw_response", sa.Text(), nullable=False),
        sa.Column("parsed_status", sa.String(32), nullable=False),
        sa.Column("unlock_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "idx_spam_check_account_time",
        "spam_check_history",
        [sa.text("account_id"), sa.text("checked_at DESC")],
    )


def downgrade() -> None:
    op.drop_index("idx_spam_check_account_time", table_name="spam_check_history")
    op.drop_table("spam_check_history")
