"""SQLAlchemy ORM models. Schema source of truth — ARCHITECTURE.md §4."""

from __future__ import annotations

import enum
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ENUM, JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class AccountStatus(str, enum.Enum):
    warmup = "warmup"
    active = "active"
    pause = "pause"
    spam_blocked = "spam_blocked"
    dead = "dead"
    disabled = "disabled"


class LogLevel(str, enum.Enum):
    debug = "debug"
    info = "info"
    warning = "warning"
    error = "error"
    critical = "critical"


account_status_enum = ENUM(
    AccountStatus,
    name="account_status",
    create_type=False,
    values_callable=lambda e: [m.value for m in e],
)

log_level_enum = ENUM(
    LogLevel,
    name="log_level",
    create_type=False,
    values_callable=lambda e: [m.value for m in e],
)


class Account(Base):
    """ARCHITECTURE.md §4.1"""

    __tablename__ = "accounts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    phone: Mapped[str] = mapped_column(String(20), unique=True, nullable=False)
    tg_user_id: Mapped[int | None] = mapped_column(BigInteger, unique=True)
    username: Mapped[str | None] = mapped_column(String(64))
    first_name: Mapped[str | None] = mapped_column(String(128))
    session_path: Mapped[str] = mapped_column(String(255), nullable=False)
    proxy_url: Mapped[str | None] = mapped_column(String(255))
    status: Mapped[AccountStatus] = mapped_column(
        account_status_enum, nullable=False, server_default=AccountStatus.warmup.value
    )
    spam_unlock_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    warmup_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    daily_sent: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    daily_invited: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    last_reset_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    limit_reduced_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint(r"phone ~ '^\+?[0-9]{7,15}$'", name="phone_format"),
        Index(
            "idx_accounts_status",
            "status",
            postgresql_where=text("status IN ('active', 'pause')"),
        ),
        Index(
            "idx_accounts_unlock",
            "spam_unlock_at",
            postgresql_where=text("spam_unlock_at IS NOT NULL"),
        ),
    )


class Setting(Base):
    """ARCHITECTURE.md §4.9. Runtime-mutable settings with LISTEN/NOTIFY invalidation."""

    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    value_type: Mapped[str] = mapped_column(String(16), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_by: Mapped[int | None] = mapped_column(BigInteger)


class Log(Base):
    """ARCHITECTURE.md §4.8.

    campaign_id and task_id are unconstrained BigInteger in MVP-1 — FKs are added
    in the migration that introduces `campaigns` and `tasks` (MVP-2).
    """

    __tablename__ = "logs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    ts: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    level: Mapped[LogLevel] = mapped_column(log_level_enum, nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    account_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("accounts.id", ondelete="SET NULL")
    )
    campaign_id: Mapped[int | None] = mapped_column(BigInteger)
    task_id: Mapped[int | None] = mapped_column(BigInteger)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    message: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        Index("idx_logs_ts", "ts"),
        Index("idx_logs_account_ts", "account_id", "ts"),
        Index("idx_logs_event", "event_type"),
    )
