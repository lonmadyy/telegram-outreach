"""Постоянный профиль устройства аккаунта (initConnection). ARCHITECTURE.md §21.2.

Раньше все аккаунты представлялись одинаково: «PC 64bit / Linux / 1.0.0 / en», при этом
глобальный ключ 2040 — это официальный Telegram Desktop, у которого такие значения не
бывают. Теперь новый аккаунт при входе получает правдоподобный desktop-профиль, который
хранится в accounts.device и больше не меняется (смена «устройства» у живой сессии —
сама по себе сигнал). Язык — по стране номера. Чистые функции, tests/test_fingerprint.py.

Пул версий — константа; обновлять раз в полгода.
"""

from __future__ import annotations

import random

OFFICIAL_TDESKTOP_API_ID = 2040

TDESKTOP_VERSIONS = (
    "5.5.5", "5.6.3", "5.7.1", "5.8.3", "5.9.0", "5.10.7", "5.11.1", "5.12.3",
)

WINDOWS_MODELS = (
    "MS-7C02", "MS-7B86", "MS-7C91", "B450M DS3H", "B550M PRO-VDH WIFI",
    "PRIME B450M-A", "ROG STRIX B550-F GAMING", "Inspiron 15 3511", "Vostro 3500",
    "HP Pavilion Laptop 15-eg0xxx", "HP Laptop 15s-fq2xxx", "Lenovo IdeaPad 3 15ITL6",
    "ThinkPad T14 Gen 2i", "Aspire A515-56", "Desktop",
)
WINDOWS_SYSTEMS = ("Windows 10", "Windows 10", "Windows 11")

MAC_MODELS = ("MacBookAir10,1", "MacBookPro18,3", "MacBookPro17,1", "Mac14,2", "iMac21,1")
MAC_SYSTEMS = ("macOS 13.6", "macOS 14.4", "macOS 14.6", "macOS 15.1")

# Код страны → (lang_code, system_lang_code). Порядок: длинные коды раньше.
_LANG_BY_CC: tuple[tuple[str, tuple[str, str]], ...] = (
    ("375", ("ru", "ru-BY")),
    ("380", ("uk", "uk-UA")),
    ("998", ("ru", "ru-UZ")),
    ("996", ("ru", "ru-KG")),
    ("995", ("ru", "ru-GE")),
    ("374", ("ru", "ru-AM")),
    ("48", ("pl", "pl-PL")),
    ("49", ("de", "de-DE")),
    ("44", ("en", "en-GB")),
    ("62", ("id", "id-ID")),
    ("7", ("ru", "ru-RU")),
    ("1", ("en", "en-US")),
)
_DEFAULT_LANG = ("en", "en-US")


def lang_for_phone(phone: str) -> tuple[str, str]:
    digits = "".join(ch for ch in (phone or "") if ch.isdigit())
    for cc, lang in _LANG_BY_CC:
        if digits.startswith(cc):
            return lang
    return _DEFAULT_LANG


def make_device(phone: str, api_id: int, rng: random.Random | None = None) -> dict:
    """Новый профиль: {device_model, system_version, app_version, lang_code,
    system_lang_code, lang_pack}. api_id — эффективный ключ аккаунта."""
    rng = rng or random.Random()
    version = rng.choice(TDESKTOP_VERSIONS)
    if rng.random() < 0.85:
        model = rng.choice(WINDOWS_MODELS)
        system = rng.choice(WINDOWS_SYSTEMS)
        app_version = f"{version} x64"
    else:
        model = rng.choice(MAC_MODELS)
        system = rng.choice(MAC_SYSTEMS)
        app_version = version
    lang_code, system_lang_code = lang_for_phone(phone)
    return {
        "device_model": model,
        "system_version": system,
        "app_version": app_version,
        "lang_code": lang_code,
        "system_lang_code": system_lang_code,
        # «langPacks are for official apps only» — только для ключа Telegram Desktop.
        "lang_pack": "tdesktop" if api_id == OFFICIAL_TDESKTOP_API_ID else "",
    }
