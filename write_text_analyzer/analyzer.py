"""
Анализатор запросов пользователя на создание текста — один вызов chat.completions, ответ JSON.
"""

from __future__ import annotations

import json
import re
from typing import Any

# Порядок ключей в сохранённом JSON
EXPECTED_KEYS = (
    "topic",
    "goal",
    "audience",
    "tone",
    "format",
    "length",
    "extra_note",
    "examples",
    "no_no",
)

LENGTH_CANONICAL = frozenset({"короткий", "средний", "длинный"})
TONE_DEFAULT = "нейтральный"


WRITE_TEXT_ANALYZER_PROMPT = '''Ты — анализатор запросов пользователя на создание текста.

На входе — один запрос пользователя, в котором он просит написать связный текст (статью, пост, гайд, письмо и т.п.).
На выходе — строго JSON с полями ниже, без markdown и без текста вне JSON.

Поля JSON:

- "topic" — тема: о чём писать (ИИ‑бот, Python, SEO и т.п.). Если не ясно — кратко сформулируй по запросу; иначе "".

- "goal" — цель текста: одно короткое предложение или фраза из 3–7 слов, по сути, без воды.
  Примеры формата: «убедить HR взять на стажировку», «обучить новичков основам кибербезопасности», «продать онлайн‑курс по JavaScript».
  Если цель не названа прямо, но однозначно следует из задачи (продать, обучить, убедить, проинформировать, развлечь и т.д.) — всё равно заполни goal такой фразой из 3–7 слов.
  Не оставляй goal пустым, если по смыслу запроса цель понятна.

- "audience" — кто читает. Если не указано — "" или null.

- "tone" — строка. ВСЕГДА непустая строка.
  Если тон в запросе явно не задан, выбери наиболее уместный из списка (ровно одно слово в нижнем регистре как в списке):
  нейтральный, деловой, дружелюбный, экспертный, мотивационный, продающий.
  Если пользователь задал другой тон (например, «мемный», «ироничный») — запиши его словами; если это укладывается в список — можно взять из списка.

- "format" — формат материала (пост VK, статья блога, письмо…). Если не указано — "".

- "length" — ТОЛЬКО одно из трёх слов (нижний регистр): короткий, средний, длинный.
  Правила нормализации:
  • Если в запросе явна краткость: «кратко», «тезисно», «короткий пост», «до N символов» (небольшой объём) — ставь короткий.
  • Если явна развёрнутость: «подробно», «длинная», «детальная статья/инструкция», «много текста» — ставь длинный.
  • Если про объём ничего нет — ставь средний.
  Не пиши в length свободные фразы вроде «подробная статья» — только короткий / средний / длинный.

- "extra_note" — необязательные уточнения: оригинальные формулировки про объём («до 300 символов», «500–700 слов»), нюансы формата, пожелания к структуре, которые не поместились в length. Если нечего добавить — "".

- "examples" — стиль, аналоги, каналы. Если нет — "".

- "no_no" — запреты и ограничения. Если нет — "".

Жёсткие правила:

1) Поле tone никогда не бывает пустой строкой. Если тон не выведен из текста — выбери из: нейтральный, деловой, дружелюбный, экспертный, мотивационный, продающий.

2) Поле length всегда ровно одно из: короткий, средний, длинный — согласно подсказкам в запросу и правилам выше.

3) Если goal напрямую не назван, но логично выводится из задачи — заполни goal фразой из 3–7 слов.

4) Дополнительные уточнения длины/формата, которые нельзя выразить только тремя значениями length, переноси в extra_note.

5) Ответ — один JSON‑объект; не добавляй полей, кроме перечисленных (topic, goal, audience, tone, format, length, extra_note, examples, no_no).

Пример 1:
Пользователь:
«Напиши короткий пост для VK про ИИ‑бота для разработчиков, мемный стиль, до 300 символов, в стиле крупных dev‑каналов в Telegram, без прямой рекламы»
Ответ:
{
  "topic": "ИИ‑бот для разработчиков",
  "goal": "привлечь разработчиков в сообщество VK",
  "audience": "разработчики",
  "tone": "мемный",
  "format": "пост для VK",
  "length": "короткий",
  "extra_note": "до 300 символов; в духе крупных dev-каналов Telegram",
  "examples": "крупные dev‑каналы в Telegram",
  "no_no": "прямая реклама"
}

Пример 2:
Пользователь:
«Напиши подробную статью для блога про обучение Python для начинающих, тон эксперт‑style, без политики»
Ответ:
{
  "topic": "обучение Python для начинающих",
  "goal": "обучить новичков основам Python",
  "audience": "новички в программировании",
  "tone": "экспертный",
  "format": "статья для блога",
  "length": "длинный",
  "extra_note": "подробная статья",
  "examples": "",
  "no_no": "без политики"
}

Пользователь:
«{user_query}»

Ответ (только JSON):'''


def _ensure_keys(data: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k in EXPECTED_KEYS:
        if k in data:
            v = data[k]
            out[k] = "" if v is None else str(v).strip()
        else:
            out[k] = ""
    return out


def _canonical_length(raw: str) -> str:
    """Приводит length к одному из: короткий / средний / длинный."""
    s = (raw or "").strip().lower()
    if s in LENGTH_CANONICAL:
        return s

    short_hints = (
        "коротк",
        "кратк",
        "тезис",
        "лайт",
        "заметк",
        "мини",
    )
    long_hints = (
        "длин",
        "подроб",
        "деталь",
        "развёрнут",
        "развернут",
        "большой объ",
        "много текста",
    )
    # явные числа символов / слов как «короткий» формат
    if any(h in s for h in ("символ", "знак")) and any(ch.isdigit() for ch in s):
        digits = [int(x) for x in re.findall(r"\d+", s)]
        if digits and max(digits) <= 500:
            return "короткий"
        if digits and max(digits) >= 1500:
            return "длинный"

    if any(h in s for h in short_hints):
        return "короткий"
    if any(h in s for h in long_hints):
        return "длинный"
    if not s:
        return "средний"
    return "средний"


def _merge_extra_note(existing: str, fragment: str) -> str:
    existing = (existing or "").strip()
    fragment = (fragment or "").strip()
    if not fragment:
        return existing
    if not existing:
        return fragment
    if fragment in existing:
        return existing
    return f"{existing}; {fragment}"


def _normalize_spec(merged: dict[str, Any], user_query: str) -> dict[str, Any]:
    """Гарантирует tone, length, goal по правилам продукта."""
    raw_length_in = str(merged.get("length") or "").strip()
    canon = _canonical_length(raw_length_in)

    extra = str(merged.get("extra_note") or "").strip()
    if raw_length_in.lower() not in LENGTH_CANONICAL and raw_length_in:
        extra = _merge_extra_note(extra, raw_length_in)

    merged["length"] = canon
    merged["extra_note"] = extra

    tone = str(merged.get("tone") or "").strip()
    if not tone:
        tone = TONE_DEFAULT
    merged["tone"] = tone

    goal = str(merged.get("goal") or "").strip()
    topic = str(merged.get("topic") or "").strip()
    if not goal:
        if topic:
            merged["goal"] = "проинформировать читателя по теме текста"
        elif user_query.strip():
            merged["goal"] = "выполнить задачу пользователя по тексту запроса"
        else:
            merged["goal"] = ""

    return merged


def analyze_write_text_request(user_query: str, client: Any, model: str) -> dict[str, Any]:
    """
    Второй этап для ветки write_text: извлекает topic, goal, audience, tone, format, length,
    extra_note, examples, no_no и нормализует tone / length / goal.
    """
    system_content = WRITE_TEXT_ANALYZER_PROMPT.replace("{user_query}", user_query.strip())

    resp = client.chat.completions.create(
        model=model,
        messages=[{"role": "system", "content": system_content}],
        temperature=0.0,
        max_tokens=1200,
        response_format={"type": "json_object"},
    )
    raw = (resp.choices[0].message.content or "").strip()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"Не удалось распарсить JSON анализатора текста: {e}\nОтвет:\n{raw}") from e

    if not isinstance(data, dict):
        raise RuntimeError("Ответ анализатора текста не является JSON-объектом.")

    merged = _ensure_keys(data)
    return _normalize_spec(merged, user_query)
