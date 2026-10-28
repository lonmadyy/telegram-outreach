"""ALLOWED_USER_IDS из окружения (app/config.py, §11.1)."""

import pytest

from app.config import Settings

_REQUIRED = {
    "TG_API_ID": "1",
    "TG_API_HASH": "x",
    "BOT_TOKEN": "1:x",
    "POSTGRES_DB": "db",
    "POSTGRES_USER": "u",
    "POSTGRES_PASSWORD": "p",
}


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("123", [123]),
        ("123,456", [123, 456]),
        (" 123 , 456 ,", [123, 456]),
    ],
)
def test_allowed_user_ids_comma_separated(monkeypatch, raw, expected):
    for k, v in _REQUIRED.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("ALLOWED_USER_IDS", raw)
    assert Settings(_env_file=None).allowed_user_ids == expected
