"""account.pause_reason — различение причины паузы (FloodWait vs quiet-hours).

ARCHITECTURE.md §6.3 (FloodWait), §5.3 (quiet-hours), §10.2 (/status, /floodwait).

Revision ID: 0005_account_pause_reason
Revises: 0004_antiflood_participants
Create Date: 2026-06-10
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0005_account_pause_reason"
down_revision: Union[str, None] = "0004_antiflood_participants"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Причина паузы аккаунта: 'flood_wait' | 'quiet_hours' | NULL. Только метка для
    # различения/видимости (§6.3, §10.2) — на логику снятия паузы не влияет.
    op.add_column(
        "accounts",
        sa.Column("pause_reason", sa.String(32), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("accounts", "pause_reason")
