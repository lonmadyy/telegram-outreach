"""Профиль устройства аккаунта (app/telegram/fingerprint.py, §21.2)."""

import random

from app.telegram import fingerprint as fp


def test_lang_for_phone():
    assert fp.lang_for_phone("+79991234567") == ("ru", "ru-RU")
    assert fp.lang_for_phone("375291234567") == ("ru", "ru-BY")
    assert fp.lang_for_phone("+380671234567") == ("uk", "uk-UA")
    assert fp.lang_for_phone("+6281917289338") == ("id", "id-ID")
    assert fp.lang_for_phone("+15551234567") == ("en", "en-US")
    assert fp.lang_for_phone("+33123456789") == ("en", "en-US")


def test_make_device_shape_and_lang_pack():
    d = fp.make_device("+79991234567", 2040, random.Random(1))
    assert set(d) == {
        "device_model", "system_version", "app_version",
        "lang_code", "system_lang_code", "lang_pack",
    }
    assert d["lang_pack"] == "tdesktop" and d["lang_code"] == "ru"
    own = fp.make_device("+79991234567", 123456, random.Random(1))
    assert own["lang_pack"] == ""


def test_make_device_consistent_platform():
    for seed in range(200):
        d = fp.make_device("+79991234567", 2040, random.Random(seed))
        if d["system_version"].startswith("Windows"):
            assert d["app_version"].endswith(" x64")
            assert d["device_model"] in fp.WINDOWS_MODELS
        else:
            assert d["system_version"].startswith("macOS")
            assert d["device_model"] in fp.MAC_MODELS
            assert " " not in d["app_version"]


def test_profiles_vary_between_accounts():
    devices = {
        (d["device_model"], d["system_version"], d["app_version"])
        for d in (fp.make_device("+79991234567", 2040, random.Random(s)) for s in range(30))
    }
    assert len(devices) > 20


def test_create_client_applies_device(monkeypatch, tmp_path):
    from app.telegram import client_factory as cf

    monkeypatch.setattr(cf, "session_path_for", lambda phone: str(tmp_path / "s"))
    device = fp.make_device("+79991234567", 2040, random.Random(3))
    client = cf.create_client(phone="+79991234567", api_id=2040, api_hash="a" * 32, device=device)
    req = client._init_request
    assert req.device_model == device["device_model"]
    assert req.app_version == device["app_version"]
    assert req.lang_code == "ru" and req.system_lang_code == "ru-RU"
    assert req.lang_pack == "tdesktop"

    legacy = cf.create_client(phone="+79991234568", api_id=2040, api_hash="a" * 32)
    assert legacy._init_request.device_model == "PC 64bit"
    assert legacy._init_request.lang_pack == ""
