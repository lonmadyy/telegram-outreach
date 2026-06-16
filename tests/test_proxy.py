"""Юнит-тесты разбора прокси. ARCHITECTURE.md §11.1, §10.3.

Чистые функции parse_proxy / _normalize_mtproto_secret без БД и сети.
"""

from __future__ import annotations

import base64

import pytest
from telethon.network import ConnectionTcpMTProxyRandomizedIntermediate

from app.telegram.client_factory import (
    ProxyConfig,
    _normalize_mtproto_secret,
    parse_proxy,
)


# --- пустой ввод ---


def test_parse_proxy_none():
    assert parse_proxy(None) is None
    assert parse_proxy("") is None
    assert parse_proxy("   ") is None


# --- socks5 (обратная совместимость) ---


def test_socks5_with_auth():
    cfg = parse_proxy("socks5://user:pass@1.2.3.4:1080")
    assert isinstance(cfg, ProxyConfig)
    assert cfg.connection is None
    assert cfg.proxy == {
        "proxy_type": "socks5",
        "addr": "1.2.3.4",
        "port": 1080,
        "rdns": True,
        "username": "user",
        "password": "pass",
    }


def test_socks5_without_auth():
    cfg = parse_proxy("socks5://1.2.3.4:1080")
    assert cfg.connection is None
    assert "username" not in cfg.proxy
    assert cfg.proxy["addr"] == "1.2.3.4" and cfg.proxy["port"] == 1080


def test_socks5_missing_port():
    with pytest.raises(ValueError):
        parse_proxy("socks5://1.2.3.4")


# --- MTProto: mtproto://secret@host:port ---


def test_mtproto_scheme_hex_secret():
    cfg = parse_proxy("mtproto://0123456789abcdef0123456789abcdef@5.6.7.8:443")
    assert cfg.connection is ConnectionTcpMTProxyRandomizedIntermediate
    assert cfg.proxy == ("5.6.7.8", 443, "0123456789abcdef0123456789abcdef")


def test_mtproto_scheme_missing_secret():
    with pytest.raises(ValueError):
        parse_proxy("mtproto://5.6.7.8:443")


# --- MTProto: родная ссылка tg://proxy?... ---


def test_tg_proxy_link():
    cfg = parse_proxy("tg://proxy?server=9.9.9.9&port=443&secret=AQIDBA==")
    assert cfg.connection is ConnectionTcpMTProxyRandomizedIntermediate
    assert cfg.proxy == ("9.9.9.9", 443, "01020304")  # base64 AQIDBA== -> 01020304


def test_tg_proxy_link_missing_secret():
    with pytest.raises(ValueError):
        parse_proxy("tg://proxy?server=9.9.9.9&port=443")


def test_tg_proxy_link_bad_port():
    with pytest.raises(ValueError):
        parse_proxy("tg://proxy?server=9.9.9.9&port=abc&secret=AQIDBA==")


# --- неподдерживаемые схемы ---


@pytest.mark.parametrize("url", ["http://1.2.3.4:8080", "https://1.2.3.4:8080", "socks4://1.2.3.4:1080"])
def test_unsupported_scheme(url):
    with pytest.raises(ValueError):
        parse_proxy(url)


# --- _normalize_mtproto_secret ---


def test_secret_hex_passthrough():
    assert _normalize_mtproto_secret("0123ABCD") == "0123abcd"


def test_secret_base64():
    assert _normalize_mtproto_secret("AQIDBA==") == "01020304"


def test_secret_base64_no_padding():
    assert _normalize_mtproto_secret("AQIDBA") == "01020304"


def test_secret_base64url():
    # '-' и '_' вместо '+' и '/'
    payload = base64.urlsafe_b64encode(bytes.fromhex("fbff")).decode().rstrip("=")
    assert _normalize_mtproto_secret(payload) == "fbff"


def test_secret_empty():
    with pytest.raises(ValueError):
        _normalize_mtproto_secret("")


def test_secret_arbitrary_base64_smoke():
    # произвольный base64-secret (как у MTProto-прокси) → непустой hex
    out = _normalize_mtproto_secret("dGVzdC1tdHByb3RvLXNlY3JldC0xMjM0")
    assert out and all(c in "0123456789abcdef" for c in out)


# --- mtproto:// с base64-secret со спецсимволами (regex, не urlparse) ---


def test_mtproto_base64_secret_special_chars():
    secret_bytes = bytes([0xFB, 0xFF, 0xBF] * 5)  # base64 даёт "+/+/..." (есть + и /)
    b64 = base64.b64encode(secret_bytes).decode()
    assert "+" in b64 and "/" in b64
    cfg = parse_proxy(f"mtproto://{b64}@1.2.3.4:443")
    assert cfg.connection is ConnectionTcpMTProxyRandomizedIntermediate
    assert cfg.proxy == ("1.2.3.4", 443, secret_bytes.hex())


# --- fake-TLS (ee) отклоняется во всех путях ---


def test_fake_tls_secret_hex_rejected():
    with pytest.raises(ValueError, match="fake-TLS"):
        _normalize_mtproto_secret("ee" + "00" * 16)


def test_fake_tls_via_tg_link_rejected():
    raw = bytes([0xEE]) + bytes(range(15)) + b"mail.ru"  # как у реального proxy6-fake-TLS
    b64 = base64.b64encode(raw).decode()
    with pytest.raises(ValueError, match="fake-TLS"):
        parse_proxy(f"tg://proxy?server=1.2.3.4&port=443&secret={b64}")


def test_fake_tls_via_mtproto_scheme_rejected():
    raw = bytes([0xEE]) + bytes(range(15))
    b64 = base64.b64encode(raw).decode()
    with pytest.raises(ValueError, match="fake-TLS"):
        parse_proxy(f"mtproto://{b64}@1.2.3.4:443")
