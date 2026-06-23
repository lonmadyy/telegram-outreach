"""Сид доп. настроек: flood_*, spamcheck_quiet_pause, peerflood_cooldown_hours.

Эти ключи код уже читает из settings_cache с дефолтами, но они не были засеяны
в таблицу settings → их нельзя было менять через /set и не видно в /settings (§4.9).
Значения = текущим дефолтам кода. ON CONFLICT DO NOTHING — не перетирать значение,
если ключ уже был добавлен вручную.

Revision ID: 0008_seed_extra_settings
Revises: 0007_account_api_credentials
Create Date: 2026-06-23
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0008_seed_extra_settings"
down_revision: Union[str, None] = "0007_account_api_credentials"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# (key, value, value_type, description)
_EXTRA_SETTINGS = [
    ("flood_interval_min_sec", "450", "int",
     "Мин. пауза между действиями при адаптивном замедлении (сек)"),
    ("flood_interval_max_sec", "720", "int",
     "Макс. пауза между действиями при адаптивном замедлении (сек)"),
    ("flood_window_quorum_sec", "1800", "int",
     "Окно для кворума массового PeerFlood (сек)"),
    ("flood_window_adaptive_sec", "3600", "int",
     "Окно недавнего PeerFlood для адаптивного интервала (сек)"),
    ("flood_adaptive_hold_sec", "3600", "int",
     "Сколько держать адаптивное замедление после флуда (сек)"),
    ("peerflood_cooldown_hours", "3", "int",
     "Кулдаун: no_limits не снимает spam_blocked раньше (ч; 0=выкл)"),
    ("spamcheck_quiet_pause", "true", "bool",
     "Не опрашивать SpamBot в тихие часы"),
]


def upgrade() -> None:
    stmt = sa.text(
        "INSERT INTO settings (key, value, value_type, description) "
        "VALUES (:k, :v, :t, :d) ON CONFLICT (key) DO NOTHING"
    )
    for key, value, vtype, desc in _EXTRA_SETTINGS:
        op.execute(stmt.bindparams(k=key, v=value, t=vtype, d=desc))


def downgrade() -> None:
    keys = ", ".join(f"'{k}'" for k, *_ in _EXTRA_SETTINGS)
    op.execute(f"DELETE FROM settings WHERE key IN ({keys})")
