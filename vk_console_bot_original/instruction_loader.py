'''
Загрузка инструкций для AI из файла instructions.txt.

Формат переменной: строка вида NAME = """ затем текст инструкции, затем строка только из """.
Строки, начинающиеся с #, — комментарии.

Основная программа не хранит тексты инструкций — только вызывает get_instruction().
'''

from __future__ import annotations

import os
import re
from typing import Final

_INSTRUCTIONS_FILENAME: Final[str] = "instructions.txt"
_VAR_START: Final[re.Pattern[str]] = re.compile(
    r"^([A-Za-z][A-Za-z0-9_]*)\s*=\s*\"\"\"\s*$"
)


def instructions_file_path() -> str:
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), _INSTRUCTIONS_FILENAME)


def _parse_variables(content: str) -> dict[str, str]:
    """Разбор переменных TEXT = \"\"\" ... \"\"\" из содержимого файла."""
    lines = content.splitlines()
    result: dict[str, str] = {}
    i = 0
    n = len(lines)

    while i < n:
        raw = lines[i]
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            i += 1
            continue

        m = _VAR_START.match(stripped)
        if not m:
            i += 1
            continue

        name = m.group(1)
        i += 1
        buf: list[str] = []
        while i < n:
            if lines[i].strip() == '"""':
                result[name] = "\n".join(buf).strip()
                i += 1
                break
            buf.append(lines[i])
            i += 1
        else:
            raise ValueError(
                f"В «{_INSTRUCTIONS_FILENAME}» не закрыта тройная кавычка для переменной «{name}»."
            )

    return result


def _load_all_variables() -> dict[str, str]:
    path = instructions_file_path()
    if not os.path.isfile(path):
        raise FileNotFoundError(
            f"Файл инструкций не найден: {path}\n"
            f"Создайте {_INSTRUCTIONS_FILENAME} рядом с main.py."
        )
    with open(path, encoding="utf-8-sig") as f:
        raw = f.read()
    return _parse_variables(raw)


def get_instruction(name: str) -> str:
    """
    Возвращает текст инструкции — значение переменной name из instructions.txt.

    Raises:
        FileNotFoundError: нет файла instructions.txt
        KeyError: нет переменной с таким именем
        ValueError: синтаксическая ошибка в файле (незакрытые кавычки)
    """
    variables = _load_all_variables()
    if name not in variables:
        available = ", ".join(sorted(variables)) or "(нет ни одной переменной)"
        raise KeyError(
            f"В «{_INSTRUCTIONS_FILENAME}» нет переменной «{name}». "
            f"Задайте строку {name} = \"\"\" ... \"\"\". Доступно: {available}"
        )
    text = variables[name]
    if not text:
        raise KeyError(f"Переменная «{name}» в «{_INSTRUCTIONS_FILENAME}» пуста.")
    return text


def load_ai_instruction(name: str) -> str:
    """Совместимость со старым именем: то же, что get_instruction(name)."""
    return get_instruction(name)


def list_instruction_variables() -> list[str]:
    """Имена переменных в instructions.txt."""
    path = instructions_file_path()
    if not os.path.isfile(path):
        return []
    with open(path, encoding="utf-8-sig") as f:
        raw = f.read()
    try:
        return sorted(_parse_variables(raw).keys())
    except ValueError:
        return []
