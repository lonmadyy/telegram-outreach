"""SQLAlchemy ORM models. Schema source of truth — ARCHITECTURE.md §4."""

from __future__ import annotations

import enum
from datetime import datetime

from sqlalchemy import (
    Boolean,
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


class CampaignType(str, enum.Enum):
    message = "message"
    invite = "invite"


class CampaignStatus(str, enum.Enum):
    pending = "pending"
    running = "running"
    paused = "paused"
    done = "done"
    failed = "failed"
    cancelled = "cancelled"


class TaskStatus(str, enum.Enum):
    queued = "queued"
    in_progress = "in_progress"
    done = "done"
    failed = "failed"
    skipped = "skipped"


class ResultCode(str, enum.Enum):
    ok = "ok"
    flood_wait = "flood_wait"
    peer_flood = "peer_flood"
    privacy_restricted = "privacy_restricted"
    not_mutual_contact = "not_mutual_contact"
    not_found = "not_found"
    already_member = "already_member"
    channel_private = "channel_private"
    too_many_channels = "too_many_channels"
    banned_in_channel = "banned_in_channel"
    deactivated = "deactivated"
    other_error = "other_error"


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

campaign_type_enum = ENUM(
    CampaignType,
    name="campaign_type",
    create_type=False,
    values_callable=lambda e: [m.value for m in e],
)

campaign_status_enum = ENUM(
    CampaignStatus,
    name="campaign_status",
    create_type=False,
    values_callable=lambda e: [m.value for m in e],
)

task_status_enum = ENUM(
    TaskStatus,
    name="task_status",
    create_type=False,
    values_callable=lambda e: [m.value for m in e],
)

result_code_enum = ENUM(
    ResultCode,
    name="result_code",
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
    # Per-account Telegram API-ключ (§11.1). NULL → используется глобальный ключ из .env
    # (legacy-аккаунты). Telethon привязывает api_id к .session при логине, поэтому ключ
    # должен оставаться неизменным весь срок жизни аккаунта.
    api_id: Mapped[int | None] = mapped_column(Integer)
    api_hash: Mapped[str | None] = mapped_column(String(64))
    # Постоянный профиль устройства (§21.2): device_model, system_version, app_version,
    # lang_code, system_lang_code, lang_pack. NULL → прежние общие параметры клиента.
    device: Mapped[dict | None] = mapped_column(JSONB)
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
    # Причина паузы (когда status=pause): 'flood_wait' (§6.3) или 'quiet_hours' (§5.3).
    # NULL = не на паузе / прочее. Только метка для различения и видимости (/status,
    # /floodwait); на саму логику пауз не влияет (снятие — is_pause_expired/таймер).
    pause_reason: Mapped[str | None] = mapped_column(String(32))
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Роли (§22.1): личка (кампании в ЛС и инвайты) и группы (автопостинг §22).
    role_dm: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("TRUE")
    )
    role_groups: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("FALSE")
    )
    # Счётчики групповой линии за сутки (§22.5), сброс в 00:00 UTC.
    daily_group_posts: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    daily_group_joins: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    last_group_join_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

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
    """ARCHITECTURE.md §4.8."""

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
    campaign_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("campaigns.id", ondelete="SET NULL")
    )
    task_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("tasks.id", ondelete="SET NULL")
    )
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    message: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        Index("idx_logs_ts", "ts"),
        Index("idx_logs_account_ts", "account_id", "ts"),
        Index("idx_logs_campaign_ts", "campaign_id", "ts"),
        Index("idx_logs_event", "event_type"),
    )


class Template(Base):
    """ARCHITECTURE.md §4.3."""

    __tablename__ = "templates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    variables: Mapped[list] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    # 'dm' — для кампаний в личку (§8), 'group' — для рассылки по группам (§22.2).
    kind: Mapped[str] = mapped_column(String(8), nullable=False, server_default="dm")


class Campaign(Base):
    """ARCHITECTURE.md §4.4."""

    __tablename__ = "campaigns"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    type: Mapped[CampaignType] = mapped_column(campaign_type_enum, nullable=False)
    template_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("templates.id")
    )
    target_chat: Mapped[str | None] = mapped_column(String(255))
    target_chat_id: Mapped[int | None] = mapped_column(BigInteger)
    status: Mapped[CampaignStatus] = mapped_column(
        campaign_status_enum,
        nullable=False,
        server_default=CampaignStatus.pending.value,
    )
    total_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    sent_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    skipped_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    failed_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    resend_old: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("FALSE")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by_user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    notes: Mapped[str | None] = mapped_column(Text)
    # Причина паузы (§5.3): 'global_flood' → авто-возобновление джобой. NULL = прочее.
    paused_reason: Mapped[str | None] = mapped_column(String(32))
    # Кампания создана из итога /parse (§21.1): получатели ищутся в исходных группах.
    source_parse_job_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("parse_jobs.id", ondelete="SET NULL")
    )

    __table_args__ = (
        Index(
            "idx_campaigns_status",
            "status",
            postgresql_where=text("status IN ('running', 'paused')"),
        ),
    )


class Task(Base):
    """ARCHITECTURE.md §4.5."""

    __tablename__ = "tasks"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    campaign_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("campaigns.id", ondelete="CASCADE"),
        nullable=False,
    )
    username: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[TaskStatus] = mapped_column(
        task_status_enum, nullable=False, server_default=TaskStatus.queued.value
    )
    assigned_account_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("accounts.id")
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    result_code: Mapped[ResultCode | None] = mapped_column(result_code_enum)
    error_message: Mapped[str | None] = mapped_column(Text)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        Index(
            "idx_tasks_queue",
            "campaign_id",
            "status",
            "locked_until",
            postgresql_where=text("status = 'queued'"),
        ),
        Index(
            "idx_tasks_campaign_username",
            "campaign_id",
            "username",
            unique=True,
        ),
    )


class SpamCheckHistory(Base):
    """ARCHITECTURE.md §4.7."""

    __tablename__ = "spam_check_history"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    account_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("accounts.id", ondelete="CASCADE"),
        nullable=False,
    )
    checked_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    raw_response: Mapped[str] = mapped_column(Text, nullable=False)
    parsed_status: Mapped[str] = mapped_column(String(32), nullable=False)
    unlock_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        Index(
            "idx_spam_check_account_time",
            "account_id",
            "checked_at",
        ),
    )


class ProcessedClient(Base):
    """ARCHITECTURE.md §4.6. Глобальный реестр обработанных клиентов между кампаниями."""

    __tablename__ = "processed_clients"

    username: Mapped[str] = mapped_column(String(64), primary_key=True)
    last_action: Mapped[str] = mapped_column(String(16), nullable=False)
    last_result_code: Mapped[ResultCode] = mapped_column(result_code_enum, nullable=False)
    first_processed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    last_processed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    account_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("accounts.id")
    )
    campaign_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("campaigns.id")
    )

    __table_args__ = (Index("idx_processed_last_at", "last_processed_at"),)


class InvitedParticipant(Base):
    """DB-снимок приглашённых НАМИ участников целевых чатов. §19 #11 (инкрементально).

    Пишем только тех, кого пригласили мы — чтобы после рестарта не пытаться
    пригласить повторно (двойной инвайт палит аккаунт). PK (chat_id, user_id).
    """

    __tablename__ = "invited_participants"

    chat_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    campaign_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("campaigns.id", ondelete="SET NULL")
    )
    invited_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (Index("idx_invited_chat", "chat_id"),)


class ParseJob(Base):
    """Задание парсинга групп (/parse). ARCHITECTURE.md §4.10, §20.

    status: running → done | stopped | failed | interrupted (рестарт посреди работы).
    groups — исходные токены [[kind, value], …]; report — сводка по группам (по мере
    прохождения); dropped — счётчики отсева по причинам.
    """

    __tablename__ = "parse_jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("accounts.id", ondelete="SET NULL")
    )
    chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="running"
    )
    groups: Mapped[list] = mapped_column(JSONB, nullable=False)
    options: Mapped[dict] = mapped_column(JSONB, nullable=False)
    report: Mapped[list] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    dropped: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ParseResult(Base):
    """Собранные usernames (lower, без @) по группам задания. §4.10.

    PK (job_id, group_idx, username) — дедуп внутри группы на уровне БД; общий
    дедуп между группами — при выдаче (app/parsing/collector.build_outputs).
    """

    __tablename__ = "parse_results"

    job_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("parse_jobs.id", ondelete="CASCADE"), primary_key=True
    )
    group_idx: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(64), primary_key=True)


class ChatMemberLoad(Base):
    """Когда загружен снимок участников целевого чата. ARCHITECTURE.md §4.11, §21.4."""

    __tablename__ = "chat_member_loads"

    chat_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    loaded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    members: Mapped[int] = mapped_column(Integer, nullable=False)


class ChatMember(Base):
    """Снимок участников целевого чата инвайт-кампании (user_id). §4.11, §21.4."""

    __tablename__ = "chat_members"

    chat_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)


# ---------------------------------------------------------------------------
# Рассылка по группам. ARCHITECTURE.md §22.8.
# ---------------------------------------------------------------------------


class GroupCampaign(Base):
    """Групповая рассылка (автопостинг). status: running | paused | stopped."""

    __tablename__ = "group_campaigns"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    template_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("templates.id", ondelete="RESTRICT"), nullable=False
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="running"
    )
    # even | all | manual (§22.4)
    distribution: Mapped[str] = mapped_column(
        String(8), nullable=False, server_default="even"
    )
    period_min: Mapped[int] = mapped_column(Integer, nullable=False)  # минуты
    period_max: Mapped[int] = mapped_column(Integer, nullable=False)
    gap_min_sec: Mapped[int] = mapped_column(Integer, nullable=False)
    gap_max_sec: Mapped[int] = mapped_column(Integer, nullable=False)
    window_start: Mapped[str | None] = mapped_column(String(5))  # 'HH:MM'
    window_end: Mapped[str | None] = mapped_column(String(5))
    created_by_user_id: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    stopped_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class GroupCampaignAccount(Base):
    """Аккаунты, участвующие в групповой рассылке."""

    __tablename__ = "group_campaign_accounts"

    campaign_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("group_campaigns.id", ondelete="CASCADE"), primary_key=True
    )
    account_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("accounts.id", ondelete="CASCADE"), primary_key=True
    )


class GroupTarget(Base):
    """Пара «группа × аккаунт» в рассылке.

    state: new → (pending) → active → manual | excluded (§22.6).
    next_at — следующий пост (active) или перепроверка заявки (pending).
    """

    __tablename__ = "group_targets"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    campaign_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("group_campaigns.id", ondelete="CASCADE"), nullable=False
    )
    account_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("accounts.id", ondelete="SET NULL")
    )
    ref_kind: Mapped[str] = mapped_column(String(8), nullable=False)  # username|invite|id
    ref_value: Mapped[str] = mapped_column(String(128), nullable=False)
    ref_key: Mapped[str] = mapped_column(String(140), nullable=False)
    period_min: Mapped[int | None] = mapped_column(Integer)  # свой период строки, мин
    period_max: Mapped[int | None] = mapped_column(Integer)
    state: Mapped[str] = mapped_column(String(16), nullable=False, server_default="new")
    state_reason: Mapped[str | None] = mapped_column(Text)
    title: Mapped[str | None] = mapped_column(String(255))
    chat_id: Mapped[int | None] = mapped_column(BigInteger)
    access_hash: Mapped[int | None] = mapped_column(BigInteger)
    peer_type: Mapped[str | None] = mapped_column(String(8))  # channel | chat
    slowmode_sec: Mapped[int | None] = mapped_column(Integer)
    joined_by_us: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("FALSE")
    )
    joined_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_post_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    posts_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    fail_streak: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        Index("idx_group_targets_account_state_next", "account_id", "state", "next_at"),
        Index("idx_group_targets_campaign", "campaign_id"),
    )


class GroupPost(Base):
    """Журнал постов групповой рассылки (статистика, CSV)."""

    __tablename__ = "group_posts"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    campaign_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("group_campaigns.id", ondelete="CASCADE"), nullable=False
    )
    target_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("group_targets.id", ondelete="CASCADE"), nullable=False
    )
    account_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("accounts.id", ondelete="SET NULL")
    )
    posted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    message_id: Mapped[int | None] = mapped_column(BigInteger)

    __table_args__ = (Index("idx_group_posts_campaign_time", "campaign_id", "posted_at"),)
