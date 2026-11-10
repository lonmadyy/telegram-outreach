"""Рассылка по группам: роли аккаунтов, шаблоны для групп, групповые рассылки.

ARCHITECTURE.md §4.12, §22.8.

Revision ID: 0011_group_broadcast
Revises: 0010_antiban_requests
Create Date: 2026-11-11
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0011_group_broadcast"
down_revision: Union[str, None] = "0010_antiban_requests"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# (key, value, value_type, description) — §22.8. ON CONFLICT DO NOTHING.
_GROUP_SETTINGS = [
    ("group_join_daily_limit", "10", "int",
     "Группы: макс. вступлений на аккаунт в сутки"),
    ("group_join_gap_min_sec", "900", "int",
     "Группы: мин. пауза между вступлениями (сек)"),
    ("group_join_gap_max_sec", "2400", "int",
     "Группы: макс. пауза между вступлениями (сек)"),
    ("group_mixed_gap_min_sec", "60", "int",
     "Мин. пауза между любыми действиями аккаунта с ролями личка+группы (сек)"),
    ("group_mixed_gap_max_sec", "120", "int",
     "Макс. пауза между любыми действиями аккаунта с ролями личка+группы (сек)"),
    ("group_period_min_default", "30", "int",
     "Группы: период повтора по умолчанию, от (мин)"),
    ("group_period_max_default", "200", "int",
     "Группы: период повтора по умолчанию, до (мин)"),
    ("group_gap_min_default_sec", "120", "int",
     "Группы: пауза между постами по умолчанию, от (сек)"),
    ("group_gap_max_default_sec", "600", "int",
     "Группы: пауза между постами по умолчанию, до (сек)"),
    ("group_fail_streak_limit", "3", "int",
     "Группы: ошибок подряд до исключения группы"),
    ("group_pending_recheck_hours", "4", "int",
     "Группы: как часто перепроверять заявку на вступление (ч)"),
    ("group_pending_max_hours", "72", "int",
     "Группы: сколько ждать одобрения заявки до ручной проверки (ч)"),
    ("group_posts_daily_limit", "0", "int",
     "Группы: макс. постов на аккаунт в сутки (0 = без лимита)"),
]


def upgrade() -> None:
    # --- accounts: роли и групповые счётчики (§22.1, §22.5) ---
    op.add_column(
        "accounts",
        sa.Column("role_dm", sa.Boolean(), nullable=False, server_default=sa.text("TRUE")),
    )
    op.add_column(
        "accounts",
        sa.Column(
            "role_groups", sa.Boolean(), nullable=False, server_default=sa.text("FALSE")
        ),
    )
    op.add_column(
        "accounts",
        sa.Column(
            "daily_group_posts", sa.Integer(), nullable=False, server_default=sa.text("0")
        ),
    )
    op.add_column(
        "accounts",
        sa.Column(
            "daily_group_joins", sa.Integer(), nullable=False, server_default=sa.text("0")
        ),
    )
    op.add_column(
        "accounts",
        sa.Column("last_group_join_at", sa.DateTime(timezone=True), nullable=True),
    )

    # --- templates.kind (§22.2) ---
    op.add_column(
        "templates",
        sa.Column("kind", sa.String(8), nullable=False, server_default="dm"),
    )
    op.create_check_constraint("templates_kind", "templates", "kind IN ('dm', 'group')")

    # --- group_campaigns ---
    op.create_table(
        "group_campaigns",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column(
            "template_id",
            sa.Integer(),
            sa.ForeignKey("templates.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("status", sa.String(16), nullable=False, server_default="running"),
        sa.Column("distribution", sa.String(8), nullable=False, server_default="even"),
        sa.Column("period_min", sa.Integer(), nullable=False),
        sa.Column("period_max", sa.Integer(), nullable=False),
        sa.Column("gap_min_sec", sa.Integer(), nullable=False),
        sa.Column("gap_max_sec", sa.Integer(), nullable=False),
        sa.Column("window_start", sa.String(5), nullable=True),
        sa.Column("window_end", sa.String(5), nullable=True),
        sa.Column("created_by_user_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("stopped_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('running', 'paused', 'stopped')", name="group_campaigns_status"
        ),
        sa.CheckConstraint(
            "distribution IN ('even', 'all', 'manual')",
            name="group_campaigns_distribution",
        ),
    )

    # --- group_campaign_accounts ---
    op.create_table(
        "group_campaign_accounts",
        sa.Column(
            "campaign_id",
            sa.Integer(),
            sa.ForeignKey("group_campaigns.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "account_id",
            sa.Integer(),
            sa.ForeignKey("accounts.id", ondelete="CASCADE"),
            primary_key=True,
        ),
    )

    # --- group_targets ---
    op.create_table(
        "group_targets",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column(
            "campaign_id",
            sa.Integer(),
            sa.ForeignKey("group_campaigns.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "account_id",
            sa.Integer(),
            sa.ForeignKey("accounts.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("ref_kind", sa.String(8), nullable=False),
        sa.Column("ref_value", sa.String(128), nullable=False),
        sa.Column("ref_key", sa.String(140), nullable=False),
        sa.Column("period_min", sa.Integer(), nullable=True),
        sa.Column("period_max", sa.Integer(), nullable=True),
        sa.Column("state", sa.String(16), nullable=False, server_default="new"),
        sa.Column("state_reason", sa.Text(), nullable=True),
        sa.Column("title", sa.String(255), nullable=True),
        sa.Column("chat_id", sa.BigInteger(), nullable=True),
        sa.Column("access_hash", sa.BigInteger(), nullable=True),
        sa.Column("peer_type", sa.String(8), nullable=True),
        sa.Column("slowmode_sec", sa.Integer(), nullable=True),
        sa.Column(
            "joined_by_us", sa.Boolean(), nullable=False, server_default=sa.text("FALSE")
        ),
        sa.Column("joined_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_post_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("posts_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("fail_streak", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "state IN ('new', 'pending', 'active', 'manual', 'excluded')",
            name="group_targets_state",
        ),
    )
    op.create_index(
        "idx_group_targets_account_state_next",
        "group_targets",
        ["account_id", "state", "next_at"],
    )
    op.create_index("idx_group_targets_campaign", "group_targets", ["campaign_id"])

    # --- group_posts ---
    op.create_table(
        "group_posts",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column(
            "campaign_id",
            sa.Integer(),
            sa.ForeignKey("group_campaigns.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "target_id",
            sa.BigInteger(),
            sa.ForeignKey("group_targets.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "account_id",
            sa.Integer(),
            sa.ForeignKey("accounts.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "posted_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("message_id", sa.BigInteger(), nullable=True),
    )
    op.create_index(
        "idx_group_posts_campaign_time", "group_posts", ["campaign_id", "posted_at"]
    )

    # --- настройки §22.8 ---
    stmt = sa.text(
        "INSERT INTO settings (key, value, value_type, description) "
        "VALUES (:k, :v, :t, :d) ON CONFLICT (key) DO NOTHING"
    )
    for key, value, vtype, desc in _GROUP_SETTINGS:
        op.execute(stmt.bindparams(k=key, v=value, t=vtype, d=desc))


def downgrade() -> None:
    keys = ", ".join(f"'{k}'" for k, *_ in _GROUP_SETTINGS)
    op.execute(f"DELETE FROM settings WHERE key IN ({keys})")
    op.drop_index("idx_group_posts_campaign_time", table_name="group_posts")
    op.drop_table("group_posts")
    op.drop_index("idx_group_targets_campaign", table_name="group_targets")
    op.drop_index("idx_group_targets_account_state_next", table_name="group_targets")
    op.drop_table("group_targets")
    op.drop_table("group_campaign_accounts")
    op.drop_table("group_campaigns")
    op.drop_constraint("templates_kind", "templates", type_="check")
    op.drop_column("templates", "kind")
    op.drop_column("accounts", "last_group_join_at")
    op.drop_column("accounts", "daily_group_joins")
    op.drop_column("accounts", "daily_group_posts")
    op.drop_column("accounts", "role_groups")
    op.drop_column("accounts", "role_dm")
