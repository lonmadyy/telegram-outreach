"""Pydantic settings из переменных окружения. ARCHITECTURE.md §11.1, §11.2."""

from __future__ import annotations

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # Telegram API
    tg_api_id: int
    tg_api_hash: str
    bot_token: str

    # Admin
    allowed_user_ids: list[int]

    # Database
    postgres_host: str = "db"
    postgres_port: int = 5432
    postgres_db: str
    postgres_user: str
    postgres_password: str

    # Encryption (опционально)
    fernet_key: str | None = None

    # Runtime
    tz: str = "Europe/Minsk"
    log_level: str = "INFO"
    sessions_path: str = "/app/data/sessions"
    logs_path: str = "/app/data/logs"

    # Дефолты поведения (фактические значения берутся из таблицы settings)
    daily_dm_limit_warm: int = 40
    daily_invite_limit_warm: int = 100
    daily_dm_limit_fresh: int = 10
    daily_invite_limit_fresh: int = 5
    interval_min_sec: int = 300
    interval_max_sec: int = 540
    spamcheck_interval_sec: int = 240
    progress_notify_interval_sec: int = 1800
    quiet_hours_start: str = "01:00"
    quiet_hours_end: str = "07:00"
    peerflood_limit_ratio: float = 0.75
    warmup_duration_hours: int = 48
    adaptive_limit_reduction_days: int = 7

    @field_validator("allowed_user_ids", mode="before")
    @classmethod
    def _parse_user_ids(cls, v: object) -> list[int]:
        if isinstance(v, str):
            return [int(x.strip()) for x in v.split(",") if x.strip()]
        if isinstance(v, (list, tuple)):
            return [int(x) for x in v]
        raise ValueError(f"ALLOWED_USER_IDS: ожидается строка или список, получено {type(v)}")

    @property
    def database_url(self) -> str:
        return (
            f"postgresql+asyncpg://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    @property
    def database_dsn(self) -> str:
        """Sync-DSN для asyncpg LISTEN/NOTIFY и Alembic CLI."""
        return (
            f"postgresql://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )


settings = Settings()  # type: ignore[call-arg]
