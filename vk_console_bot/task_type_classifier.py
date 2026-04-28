"""
Консоль для отработки классификатора типа задачи (task_type) через OpenAI-совместимый API.

Запуск из каталога vk_console_bot:
    python task_type_classifier.py

Настройки — те же, что у main.py: OPENAI_API_KEY, OPENAI_BASE_URL, OPENAI_MODEL (по умолчанию gpt-4o).

Для ветки write_text: этап 1 — классификация; этап 2 — извлечение JSON + интерактивные уточнения и сохранение
write_text_spec.json; этап 3 — мета‑промпт для браузерного ИИ (блок «СКОПИРУЙТЕ ЭТОТ ПРОМПТ …»).
Отдельно тот же третий этап можно вызвать: python promptGenerator/write_text_prompt_maker.py
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any

# Пакет write_text_analyzer лежит в корне репозитория (родитель каталога vk_console_bot).
_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from promptGenerator.write_text_prompt_maker import (
    generate_browser_prompt,
    print_prompt_block,
)
from promptGenerator.explain_prompt_maker import build_explain_browser_prompt
from write_text_analyzer import analyze_write_text_request
from write_text_analyzer.analyzer import _canonical_length

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None  # type: ignore[misc, assignment]

try:
    from openai import OpenAI
except ImportError:
    OpenAI = None  # type: ignore[misc, assignment]

# === Настройки (переопределяются через .env) ===
BASE_URL_DEFAULT = "https://api.proxyapi.ru/openai/v1"
MODEL_DEFAULT = "gpt-4o"

# Поля write_text_spec, которые при пустоте уточняются в терминале (examples / no_no не спрашиваем).
_WRITE_TEXT_INTERACTIVE_FIELDS: tuple[str, ...] = (
    "topic",
    "goal",
    "audience",
    "tone",
    "format",
    "length",
    "extra_note",
)

_FIELD_QUESTIONS: dict[str, str] = {
    "topic": (
        "Тема текста не определена. Пожалуйста, коротко опишите, про что должен быть текст:"
    ),
    "goal": (
        "Цель текста не указана. Чего вы хотите добиться этим текстом "
        "(например, продать продукт, обучить читателя, убедить HR)? Ответьте одной фразой:"
    ),
    "audience": (
        "Аудитория не указана. Для кого вы пишете этот текст "
        "(например, начинающие программисты, предприниматели, HR‑менеджеры)?"
    ),
    "tone": (
        "Тон текста не определён. Какой стиль вам нужен "
        "(например, нейтральный, деловой, дружелюбный, экспертный, мотивационный, продающий)?"
    ),
    "format": (
        "Формат текста не указан. Что это должно быть: статья, посты, лендинг, письмо, "
        "сценарий видео, лонгрид и т.п.?"
    ),
    "length": (
        "Длина текста не уточнена. Какой объём вам нужен: короткий, средний или длинный?"
    ),
    "extra_note": (
        "Есть ли дополнительные пожелания к структуре, примерам или особенностям текста? "
        "Если нет — просто нажмите Enter."
    ),
}

# Поля explain_spec, которые при пустоте уточняются в терминале.
_EXPLAIN_INTERACTIVE_FIELDS: tuple[str, ...] = (
    "topic",
    "goal",
    "audience",
    "depth",
    "format",
    "style",
)

_EXPLAIN_FIELD_QUESTIONS: dict[str, str] = {
    "topic": "По какой теме нужно объяснение? Коротко назовите предмет/вопрос:",
    "goal": (
        "Какова цель объяснения? Что вы хотите понять или уметь после чтения "
        "(одной фразой):"
    ),
    "audience": (
        "Для кого объяснение (уровень): сам пользователь, новичок, джун, мидл и т.п.?"
    ),
    "depth": (
        "Нужная глубина: поверхностно, базово, подробно, с примерами, с формулами и т.п.?"
    ),
    "format": (
        "Формат объяснения: конспект, пошаговый разбор, мини-лекция, шпаргалка, FAQ и т.п.?"
    ),
    "style": (
        "Какой стиль нужен: простой, технический, разговорный, академический и т.п.?"
    ),
}

EXPLAIN_SPEC_SYSTEM_PROMPT = """Ты — опытный инженер промптов и методист.

Твоя задача — прочитать исходный запрос пользователя (он будет следующим сообщением)
и заполнить JSON-спеку объяснения темы.

Верни ТОЛЬКО один JSON-объект без комментариев и markdown.

Структура JSON:
{
  "original_text": "полный текст запроса пользователя без изменений",
  "topic": "что объяснить (тема/предмет) или null",
  "goal": "какой результат объяснения нужен пользователю: что понять/научиться делать или null",
  "audience": "кто спрашивает (уровень): сам пользователь, новичок, джун, мидл и т.п. или null",
  "depth": "глубина: поверхностно/базово/подробно/с примерами/с формулами и т.п. или null",
  "format": "формат объяснения: конспект, пошаговый разбор, мини-лекция, шпаргалка, FAQ и т.п. или null",
  "style": "стиль объяснения: простой, технический, разговорный, академический и т.п. или null",
  "no_no": "чего избегать в объяснении или null"
}

Правила:
- original_text всегда заполняй полным текстом входного запроса без изменений.
- Если поле явно не задано и не следует из формулировки — ставь null, не выдумывай.
- Если пользователь явно просит объяснение "для себя", можно указывать audience = "сам пользователь".
- Возвращай строго валидный JSON-объект верхнего уровня, без текста до/после JSON.
"""


def _is_field_empty(spec: dict[str, Any], key: str) -> bool:
    v = spec.get(key)
    if v is None:
        return True
    return str(v).strip() == ""


def interactive_fill_write_text_spec(spec: dict[str, Any]) -> dict[str, Any]:
    """
    Дополняет в терминале пустые topic, goal, audience, tone, format, length, extra_note.
    Поля examples и no_no не запрашиваются. Пустой Enter: tone → нейтральный, length → средний;
    для остальных (кроме extra_note) оставляется пустая строка.
    """
    out: dict[str, Any] = dict(spec)
    missing = [k for k in _WRITE_TEXT_INTERACTIVE_FIELDS if _is_field_empty(out, k)]
    if not missing:
        return out

    print("\n— Уточняем недостающие параметры текста (Enter без текста — пропуск или значение по умолчанию, см. подсказки) —")

    for key in _WRITE_TEXT_INTERACTIVE_FIELDS:
        if not _is_field_empty(out, key):
            continue
        question = _FIELD_QUESTIONS[key]
        answer = input(f"\n{question}\n> ").strip()

        if not answer:
            if key == "tone":
                out[key] = "нейтральный"
            elif key == "length":
                out[key] = "средний"
            else:
                out[key] = ""
            continue

        if key == "length":
            out[key] = _canonical_length(answer)
        else:
            out[key] = answer

    return out


def analyze_explain_request(user_query: str, client: OpenAI, model: str) -> dict[str, Any]:
    """Второй этап для explain: извлечь explain_spec в JSON."""
    resp = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": EXPLAIN_SPEC_SYSTEM_PROMPT},
            {"role": "user", "content": user_query.strip()},
        ],
        temperature=0.2,
        response_format={"type": "json_object"},
    )
    raw_content = (resp.choices[0].message.content or "").strip()
    data = json.loads(raw_content)
    if not isinstance(data, dict):
        raise RuntimeError("Этап explain вернул не JSON-объект.")
    return data


def interactive_fill_explain_spec(spec: dict[str, Any]) -> dict[str, Any]:
    """Дополняет пустые поля explain_spec вопросами в терминале."""
    out: dict[str, Any] = dict(spec)
    missing = [k for k in _EXPLAIN_INTERACTIVE_FIELDS if _is_field_empty(out, k)]
    if not missing:
        return out

    print("\n— Недостаточно данных для explain: уточняем параметры (Enter — пропуск) —")
    for key in _EXPLAIN_INTERACTIVE_FIELDS:
        if not _is_field_empty(out, key):
            continue
        question = _EXPLAIN_FIELD_QUESTIONS[key]
        answer = input(f"\n{question}\n> ").strip()
        out[key] = answer if answer else ""
    if _is_field_empty(out, "no_no"):
        out["no_no"] = None
    return out


# Промпт‑классификатор для system‑сообщения
SYSTEM_PROMPT = '''Ты — классификатор пользовательских запросов.

На входе — один запрос пользователя.
На выходе — JSON с одним полем: "task_type".

Допустимые значения "task_type":
- "write_text" — основной результат — связный текст: статья, пост, сценарий выступления, письмо, рекламный текст, художественная история, руководство/гайд (включая примеры кода как иллюстрации к тексту).
- "draw_image" — нужно получить графический артефакт: картинку, иллюстрацию, логотип, мем, обложку, персонажа, комикс‑кадр (в т.ч. если параллельно просят «текст к картинке», «историю персонажа», «слоган» — если в запросе явно генерировать изображение/нарисовать/создать визуал — это draw_image).
- "make_video" — сценарий ролика, раскадровка, описание кадров, промпт для видеогенератора.
- "explain" — объяснить тему, «как это работает», ответ на вопрос «почему» без основного акцента на готовый документ или код.
- "code" — главная цель — работающая программа/скрипт/запросы/SQL/API: пользователь хочет прежде всего код как продукт, а не статью с кодом внутри.
- "plan" — бизнес‑план, дорожная карта, roadmap, поэтапный план работ, стратегия, алгоритм действий как отдельный структурированный план (не путать с «напиши статью о планировании» → write_text).
- "analyze" — разбор, оценка, сравнение вариантов, выводы по тексту/данным/ситуации.
- "other" — не укладывается в список выше.

Смешанные и многоцелевые запросы: выбери ОДИН тип по приоритету (сверху вниз — что встретилось первым по смыслу запроса):
1) Явная генерация визуала: нарисуй, сгенерируй изображение/картинку/логотип/иллюстрацию, создай визуал/meme/обложку → draw_image (даже если ниже по фразе просят «ещё и текст», «историю», «рецепты»).
2) Видео: сценарий ролика, кадры, промпт под видео → make_video.
3) Именно план как документ: «составь бизнес‑план», «roadmap», «пошаговый план запуска» → plan (даже если внутри просят фрагменты кода или текст для инвесторов как часть плана).
4) Код как главный результат: «напиши скрипт/приложение/бота», «реализуй на Python» без запроса большого объёма связного текста вне кода → code.
5) Объяснение без задачи «сделай документ» → explain.
6) Разбор/оценка → analyze.
7) Связный текст статья/гайд/пост/история как основная поставка, код только в примерах → write_text.

Важно: не отдавай всё подряд в "write_text". Статья/гайд **о** коде или видео — write_text; отдельно сформулированный запрос на картинку + текст — по п.1.

Требования к ответу:
- Только JSON с одним полем "task_type", без текста вокруг.
- Только значения из списка выше.

Примеры (ориентиры):
Пользователь: "Напиши статью о написании кода на Python для обработки видео"
Ответ:
{ "task_type": "write_text" }

Пользователь: "Составь бизнес-план с кодом на JavaScript и текстом для презентации инвесторам"
Ответ:
{ "task_type": "plan" }

Пользователь: "Сгенерируй изображение логотипа и напиши текст рекламы с инструкцией для соцсетей"
Ответ:
{ "task_type": "draw_image" }

Пользователь: "Нарисуй фэнтези-персонажа и составь историю его жизни с рецептами"
Ответ:
{ "task_type": "draw_image" }

Пользователь: {user_query}
Ответ (только JSON):'''


def read_env() -> None:
    if load_dotenv is None:
        return
    base_dir = os.path.dirname(os.path.abspath(__file__))
    env_path = os.path.join(base_dir, ".env")
    env_local = os.path.join(base_dir, ".env.local")
    if os.path.isfile(env_path):
        load_dotenv(dotenv_path=env_path, override=False, encoding="utf-8-sig")
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


def classify_task_type(user_query: str, client: OpenAI, model: str) -> dict:
    messages = [
        {
            "role": "system",
            "content": SYSTEM_PROMPT.replace("{user_query}", user_query.strip()),
        }
    ]
    resp = client.chat.completions.create(
        model=model,
        messages=messages,
        temperature=0.0,
        max_tokens=200,
    )
    raw_content = (resp.choices[0].message.content or "").strip()

    try:
        if raw_content.startswith("{") and raw_content.endswith("}"):
            return json.loads(raw_content)
        for i in range(len(raw_content)):
            if raw_content[i] == "{":
                sub = raw_content[i:]
                try:
                    return json.loads(sub)
                except json.JSONDecodeError:
                    continue
    except Exception as e:
        print(f"Ошибка парсинга JSON: {e}")
        print(f"Сырой ответ ИИ:\n{raw_content}")
        raise RuntimeError("Не удалось извлечь JSON из ответа ИИ.") from e

    raise RuntimeError("Не удалось найти JSON в ответе ИИ.")


def main() -> None:
    if OpenAI is None:
        print("Установите пакет openai: pip install openai", file=sys.stderr)
        sys.exit(1)

    client = build_openai_client()
    if client is None:
        print(
            "Задайте OPENAI_API_KEY в .env (каталог vk_console_bot или корень репозитория).",
            file=sys.stderr,
        )
        sys.exit(1)

    model = os.getenv("OPENAI_MODEL", MODEL_DEFAULT).strip() or MODEL_DEFAULT

    print("=== Бот‑классификатор задач ===")
    print("Введите запрос пользователя, например:")
    print("  Напиши статью о написании кода на Python для обработки видео")
    print("Для выхода введите: quit")
    print(f"(модель: {model})")

    base_dir = os.path.dirname(os.path.abspath(__file__))
    out_path = os.path.join(base_dir, "task_type.json")
    write_text_spec_path = os.path.join(base_dir, "write_text_spec.json")
    explain_spec_path = os.path.join(base_dir, "explain_spec.json")

    while True:
        try:
            user_input = input("\nЗапрос пользователя: ").strip()
            if not user_input:
                print("Пустой запрос — попробуйте снова.")
                continue
            if user_input.lower() in ("quit", "exit", "выход"):
                print("Завершение программы.")
                break

            task_data = classify_task_type(user_input, client, model)

            if "task_type" not in task_data:
                print("Ошибка: в ответе нет ключа task_type.")
                print("Полный ответ:", task_data)
                continue

            with open(out_path, "w", encoding="utf-8") as f:
                json.dump(task_data, f, ensure_ascii=False, indent=2)

            print("✅ Тип задачи определён:")
            print(f"   task_type: {task_data['task_type']}")
            print(f"   Файл сохранён: {out_path}")

            if task_data.get("task_type") == "write_text":
                print("\n→ Ветка write_text: извлекаю параметры текста (второй запрос к API)...")
                spec = analyze_write_text_request(user_input, client, model)
                spec = interactive_fill_write_text_spec(spec)
                with open(write_text_spec_path, "w", encoding="utf-8") as f:
                    json.dump(spec, f, ensure_ascii=False, indent=2)
                print("\n✅ Итоговые параметры текста:")
                for key in (
                    "topic",
                    "goal",
                    "audience",
                    "tone",
                    "format",
                    "length",
                    "extra_note",
                    "examples",
                    "no_no",
                ):
                    val = spec.get(key, "")
                    display = "(пусто)" if val in ("", None) else val
                    print(f"   {key}: {display}")
                print(f"   Файл сохранён: {write_text_spec_path}")

                print("\n→ Третий этап: промпт для браузерного ИИ (мета‑промпт)...")
                try:
                    browser_prompt = generate_browser_prompt(spec, client, model)
                    if browser_prompt.strip():
                        print_prompt_block(browser_prompt)
                    else:
                        print("⚠️ Пустой ответ модели на мета‑промпт — блок для копирования не сформирован.")
                except Exception as meta_err:
                    print(f"❌ Ошибка третьего этапа: {meta_err}")
            elif task_data.get("task_type") == "explain":
                print("\n→ Ветка explain: извлекаю параметры объяснения (второй запрос к API)...")
                explain_spec = analyze_explain_request(user_input, client, model)
                explain_spec = interactive_fill_explain_spec(explain_spec)
                with open(explain_spec_path, "w", encoding="utf-8") as f:
                    json.dump(explain_spec, f, ensure_ascii=False, indent=2)

                print("\n✅ Итоговые параметры explain:")
                for key in (
                    "topic",
                    "goal",
                    "audience",
                    "depth",
                    "format",
                    "style",
                    "no_no",
                ):
                    val = explain_spec.get(key, "")
                    display = "(пусто)" if val in ("", None) else val
                    print(f"   {key}: {display}")
                print(f"   Файл сохранён: {explain_spec_path}")

                print("\n→ Третий этап: мета‑промпт для объяснения (по шаблону)...")
                explain_prompt = build_explain_browser_prompt(explain_spec)
                if explain_prompt.strip():
                    print_prompt_block(explain_prompt)
                else:
                    print("⚠️ Не удалось собрать мета‑промпт для explain (пустой результат).")

        except Exception as e:
            print(f"❌ Ошибка: {e}")


if __name__ == "__main__":
    main()
