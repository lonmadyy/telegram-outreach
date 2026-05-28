"""Spintax + переменные. ARCHITECTURE.md §8.

Алгоритм рендерера (§8.3):
1. Spintax {a|b|c} раскрывается изнутри наружу (цикл while пока меняется).
2. Подстановка переменных {name} из словаря.
3. Снятие экранирования \\{ и \\}.

Регулярка SPIN_RE намеренно не пропускает `|`. Если внутри {…} нет `|`,
это считается переменной (или неопознанным токеном) и проходит на 2 этап.
"""

from __future__ import annotations

import random
import re

# Из §8.3:
VAR_RE = re.compile(r"(?<!\\)\{([a-z_]+)\}")  # {username}, {first_name}
SPIN_RE = re.compile(r"(?<!\\)\{([^{}]*?\|[^{}]*?)\}")  # {a|b|c} — самые внутренние

TELEGRAM_MAX_MESSAGE_LENGTH = 4096

# Дефолтные fallback-значения переменных при отсутствии данных (§8.2).
DEFAULT_VAR_FALLBACKS: dict[str, str] = {
    "username": "there",
    "first_name": "there",
    "last_name": "",
    "full_name": "there",
}


class TemplateError(ValueError):
    """Ошибка валидации шаблона, безопасная для показа пользователю."""


def _choose_spin(m: re.Match[str]) -> str:
    parts = m.group(1).split("|")
    return random.choice(parts) if len(parts) > 1 else m.group(0)


def render_template(body: str, variables: dict[str, str] | None = None) -> str:
    """Рендер одного варианта шаблона.

    Variables — словарь значений переменных. Отсутствующие переменные
    заполняются из DEFAULT_VAR_FALLBACKS, неизвестные — пустой строкой.
    """
    text = body

    # 1. Раскрытие spintax (изнутри наружу).
    while True:
        new_text = SPIN_RE.sub(_choose_spin, text)
        if new_text == text:
            break
        text = new_text

    # 2. Подстановка переменных.
    vars_combined: dict[str, str] = dict(DEFAULT_VAR_FALLBACKS)
    if variables:
        for k, v in variables.items():
            if v is not None and v != "":
                vars_combined[k] = str(v)

    def _sub_var(m: re.Match[str]) -> str:
        name = m.group(1)
        return vars_combined.get(name, "")

    text = VAR_RE.sub(_sub_var, text)

    # 3. Снятие экранирования.
    text = text.replace(r"\{", "{").replace(r"\}", "}")

    return text


def extract_variables(body: str) -> list[str]:
    """Список переменных, реально используемых в шаблоне (после удаления spintax-узлов).

    Учитывает только токены вида {name}, где name — нижний регистр и подчёркивания.
    Используется для записи в templates.variables (§4.3).
    """
    # Уберём из тела все spintax-узлы, чтобы не ловить варианты вроде {hi|hello}.
    cleaned = body
    while True:
        new_cleaned = SPIN_RE.sub("", cleaned)
        if new_cleaned == cleaned:
            break
        cleaned = new_cleaned

    names: list[str] = []
    seen: set[str] = set()
    for m in VAR_RE.finditer(cleaned):
        name = m.group(1)
        if name not in seen:
            seen.add(name)
            names.append(name)
    return names


def _has_unbalanced_braces(body: str) -> bool:
    """Грубая проверка на одиноко стоящие `{` или `}` без экранирования."""
    # Убираем все экранированные скобки.
    stripped = body.replace(r"\{", "").replace(r"\}", "")
    return stripped.count("{") != stripped.count("}")


def validate_template(body: str, *, sample_renders: int = 5) -> list[str]:
    """Валидация шаблона перед сохранением. ARCHITECTURE.md §8.4.

    Делает sample_renders пробных рендеров с дефолтными значениями переменных,
    проверяет длину ≤ 4096 и сбалансированность скобок. При любой проблеме
    бросает TemplateError. При успехе возвращает список реально использованных
    переменных (для сохранения в templates.variables).
    """
    if not body or not body.strip():
        raise TemplateError("Тело шаблона не может быть пустым")

    if _has_unbalanced_braces(body):
        raise TemplateError(
            "Несбалансированные фигурные скобки. Если хотите буквальные { или }, "
            "экранируйте: \\{ и \\}"
        )

    for i in range(sample_renders):
        try:
            rendered = render_template(body)
        except Exception as e:  # рендер падает только при неожиданных regex-issues
            raise TemplateError(f"Ошибка рендера #{i + 1}: {e}") from e
        if len(rendered) > TELEGRAM_MAX_MESSAGE_LENGTH:
            raise TemplateError(
                f"После рендера #{i + 1} длина {len(rendered)} > "
                f"{TELEGRAM_MAX_MESSAGE_LENGTH} (лимит Telegram)"
            )

    return extract_variables(body)


def preview(body: str, n: int = 5) -> list[str]:
    """N разных рендеров шаблона с дефолтами переменных. Для превью в боте."""
    return [render_template(body) for _ in range(n)]
