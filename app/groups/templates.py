"""Проверка шаблонов для групп. ARCHITECTURE.md §22.2. Чистые функции."""

from __future__ import annotations

from app.campaigns.template_engine import TemplateError, validate_template


def check_group_template(body: str) -> str | None:
    """None — шаблон годится, иначе текст ошибки для человека.

    Спинтакс как в §8, но без переменных: в группе нет «получателя», и `{слово}` без
    `|` почти всегда опечатка в спинтаксе.
    """
    try:
        used = validate_template(body)
    except TemplateError as e:
        return str(e)
    if used:
        names = ", ".join("{" + v + "}" for v in used)
        return (
            f"В шаблонах для групп переменных нет, а здесь есть: {names}. "
            "Если это вариант спинтакса — добавьте второй вариант через «|», "
            "например {Привет|Здравствуйте}."
        )
    return None


def unique_name(base: str, existing: set[str], *, limit: int = 128) -> str:
    """Имя шаблона, не совпадающее с существующими: «Имя», «Имя (2)», …"""
    base = base.strip()[:limit] or "Шаблон"
    if base not in existing:
        return base
    n = 2
    while True:
        suffix = f" ({n})"
        name = base[: limit - len(suffix)] + suffix
        if name not in existing:
            return name
        n += 1
