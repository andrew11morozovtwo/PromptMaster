"""
Третий шаг: по write_text_spec.json собрать «чистый» промпт для браузерного ИИ.

Запуск из корня репозитория:
    python promptGenerator/write_text_prompt_maker.py

Или с путём к JSON:
    python promptGenerator/write_text_prompt_maker.py path/to/write_text_spec.json

Тот же сценарий третьего этапа вызывается автоматически в конце ветки write_text в
`vk_console_bot/task_type_classifier.py` (сразу после сохранения write_text_spec.json).
Этот файл удобен, если нужно пересобрать промпт по уже сохранённому JSON без полного прогона консоли.
"""

from __future__ import annotations

import json
import os
import sys

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None  # type: ignore[misc, assignment]

try:
    from openai import OpenAI
except ImportError:
    OpenAI = None  # type: ignore[misc, assignment]

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

BASE_URL_DEFAULT = "https://api.proxyapi.ru/openai/v1"
MODEL_DEFAULT = "gpt-4o"

META_PROMPT_ENGINEER_SYSTEM = """Ты — опытный инженер промптов (prompt engineer) для больших языковых моделей.

Твоя задача — по структурированным параметрам сформировать ОДИН СИЛЬНЫЙ, ПРАКТИЧЕСКИЙ промпт для текстовой модели, который пользователь сможет вставить в любой браузерный ИИ (ChatGPT, Claude, Gemini, YandexGPT и т.п.), чтобы получить нужный текст.

Тебе на вход приходит JSON со следующими полями:
- "topic": тема текста.
- "goal": цель текста.
- "audience": целевая аудитория.
- "tone": тон и стиль.
- "format": формат (статья, пост, гайд, письмо и т.п.).
- "length": примерный объём.
- "extra_note": дополнительные пожелания (структура, примеры, дополнительные артефакты).
- "examples": референсы по стилю.
- "no_no": ограничения / запреты.

Особое правило для поля "length":

Тебе приходит текстовая метка длины (например: "короткий", "средний", "длинный").
Твоя задача — ВНУТРИ промпта превратить её в примерный диапазон количества символов с учётом "format".

Ориентируйся на такие диапазоны (примерные, НЕ пиши слово "примерно"):

1) Если format = "статья", "лонгрид", "обзор":
   - "короткий"  → 2 000–4 000 символов;
   - "средний"   → 4 000–7 000 символов;
   - "длинный"   → 7 000–12 000 символов.

2) Если format = "пост для соцсетей", "пост для VK", "посты для LinkedIn", "блог-пост":
   - "короткий"  → до 600–800 символов;
   - "средний"   → 800–1 500 символов;
   - "длинный"   → 1 500–3 000 символов.

3) Если format = "инструкция", "гайд", "FAQ", "руководство":
   - "короткий"  → 1 000–2 000 символов;
   - "средний"   → 2 000–4 000 символов;
   - "длинный"   → 4 000–8 000 символов.

4) Если format = "письмо", "серия писем", "email-рассылка":
   - "короткий"  → до 800–1 200 символов;
   - "средний"   → 1 200–2 000 символов;
   - "длинный"   → 2 000–3 500 символов.

5) Если формат не распознан или явно не указан:
   - "короткий"  → до 1 000–1 500 символов;
   - "средний"   → 1 500–3 000 символов;
   - "длинный"   → 3 000–6 000 символов.

Важно:
- В итоговом промпте НЕ упоминай слова "короткий/средний/длинный" как метки. Вместо этого явно пиши диапазон символов, например:
  "Объём текста — около 2 000–3 000 символов."
- Если поле "length" пустое, сам выбери разумный диапазон по формату (обычно "средний").

Твои обязанности как инженера промптов:

1. Сформулировать промпт НЕ как просьбу пользователя, а как ЧЁТКОЕ ТЗ для модели.
2. Использовать РОЛЬ: начни промпт с описания роли модели, например:
   "Ты — профессиональный автор текстов / копирайтер / технический писатель, который пишет для {audience}."
3. Чётко задать ЗАДАЧУ:
   - что нужно написать (формат: статья, пост, гайд и т.п.);
   - по какой теме;
   - с какой целью (goal).
4. Указать АУДИТОРИЮ, ТОН и ОБЪЁМ:
   - "Целевая аудитория: ...";
   - "Тон и стиль: ...";
   - "Объём: ...".
5. Включить ДОПОЛНИТЕЛЬНЫЕ ТРЕБОВАНИЯ, если они не пустые:
   - "extra_note" — превратить в конкретные пункты, что обязательно должно быть в тексте (структура, примеры, чек-листы, FAQ, таблицы и т.п.);
   - "examples" — превратить в референсы стиля ("ориентируйся на стиль ...");
   - "no_no" — оформить как список запретов ("Не используй ...").
6. Добавить требования к СТРУКТУРЕ текста:
   - логичные блоки, подзаголовки, списки;
   - ввод в тему, основная часть, вывод/призыв к действию, если это уместно для цели.
7. Быть КОНКРЕТНЫМ:
   - избегать общих фраз без деталей;
   - не повторять дословно весь JSON, а встроить значения по смыслу.

Структура итогового промпта:

- Абзац 1: роль модели и краткий контекст.
- Абзац 2: что именно нужно создать (формат, тема, цель).
- Абзац 3: аудитория, тон, объём.
- Абзац 4: дополнительные требования и референсы стиля (если есть).
- Абзац 5: требования к структуре текста (как его организовать).

Очень важно:
- Ответь ТОЛЬКО текстом улучшенного промпта.
- НЕ добавляй никаких комментариев, пояснений, кавычек вокруг промпта или Markdown-разметки.
- Промпт должен быть полностью готов к вставке в другое окно ИИ как единое задание.

Тебе будет передан JSON со значениями полей. Сгенерируй по нему такой промпт."""


def read_env() -> None:
    if load_dotenv is None:
        return
    vk_dir = os.path.join(_REPO_ROOT, "vk_console_bot")
    env_main = os.path.join(vk_dir, ".env")
    env_local = os.path.join(vk_dir, ".env.local")
    root_env = os.path.join(_REPO_ROOT, ".env")
    if os.path.isfile(env_main):
        load_dotenv(dotenv_path=env_main, override=False, encoding="utf-8-sig")
    if os.path.isfile(root_env):
        load_dotenv(dotenv_path=root_env, override=False, encoding="utf-8-sig")
    if os.path.isfile(env_local):
        load_dotenv(dotenv_path=env_local, override=True, encoding="utf-8-sig")


def build_openai_client():
    if OpenAI is None:
        return None
    read_env()
    key = (os.getenv("OPENAI_API_KEY") or "").strip()
    if not key:
        return None
    base = os.getenv("OPENAI_BASE_URL", BASE_URL_DEFAULT).strip()
    return OpenAI(api_key=key, base_url=base)


def _strip_outer_code_fence(text: str) -> str:
    """Если модель обернула ответ в ``` ... ``` — снять обёртку."""
    t = text.strip()
    if not t.startswith("```"):
        return text.strip()
    lines = t.split("\n")
    if lines and lines[0].startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    return "\n".join(lines).strip()


def load_write_text_spec(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError("write_text_spec должен быть JSON-объектом.")
    return data


def generate_browser_prompt(spec: dict, client: OpenAI, model: str) -> str:
    user_payload = (
        "Спецификация задачи на генерацию текста (JSON):\n\n"
        + json.dumps(spec, ensure_ascii=False, indent=2)
    )
    resp = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": META_PROMPT_ENGINEER_SYSTEM},
            {"role": "user", "content": user_payload},
        ],
        temperature=0.25,
        max_tokens=4096,
    )
    raw = (resp.choices[0].message.content or "").strip()
    return _strip_outer_code_fence(raw)


def print_prompt_block(prompt_text: str) -> None:
    print()
    print("=== СКОПИРУЙТЕ ЭТОТ ПРОМПТ В БРАУЗЕРНЫЙ ИИ ===")
    print()
    print(prompt_text)
    print()
    print("=== КОНЕЦ ПРОМПТА ===")


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    default_spec = os.path.join(_REPO_ROOT, "vk_console_bot", "write_text_spec.json")
    spec_path = os.path.abspath(argv[0]) if argv else default_spec

    if not os.path.isfile(spec_path):
        print(f"Файл не найден: {spec_path}", file=sys.stderr)
        print(
            "Сначала выполните классификацию и сохраните write_text_spec.json "
            "(например, через vk_console_bot/task_type_classifier.py).",
            file=sys.stderr,
        )
        return 1

    if OpenAI is None:
        print("Установите пакет openai: pip install openai", file=sys.stderr)
        return 1

    client = build_openai_client()
    if client is None:
        print(
            "Задайте OPENAI_API_KEY в .env (vk_console_bot/.env или корень репозитория).",
            file=sys.stderr,
        )
        return 1

    model = os.getenv("OPENAI_MODEL", MODEL_DEFAULT).strip() or MODEL_DEFAULT

    try:
        spec = load_write_text_spec(spec_path)
    except Exception as e:
        print(f"Не удалось прочитать JSON: {e}", file=sys.stderr)
        return 1

    print(f"(модель мета‑промпта: {model})", file=sys.stderr)
    print(f"(источник: {spec_path})", file=sys.stderr)

    try:
        prompt_text = generate_browser_prompt(spec, client, model)
    except Exception as e:
        print(f"Ошибка API: {e}", file=sys.stderr)
        return 1

    if not prompt_text.strip():
        print("Пустой ответ модели.", file=sys.stderr)
        return 1

    print_prompt_block(prompt_text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
