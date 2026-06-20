"""account.api_id / api_hash — per-account Telegram API-ключ.

ARCHITECTURE.md §4.1 (схема accounts), §11.1 (per-account ключи, fallback на .env).

Revision ID: 0007_account_api_credentials
Revises: 0006_peerflood_cooldown
Create Date: 2026-06-20
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0007_account_api_credentials"
down_revision: Union[str, None] = "0006_peerflood_cooldown"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Per-account API-ключ (§11.1). NULL = глобальный ключ из .env (legacy-аккаунты).
    op.add_column("accounts", sa.Column("api_id", sa.Integer(), nullable=True))
    op.add_column("accounts", sa.Column("api_hash", sa.String(64), nullable=True))


def downgrade() -> None:
    op.drop_column("accounts", "api_hash")
    op.drop_column("accounts", "api_id")
