"""
Консольный прототип логики бота для последующей адаптации под VK (Long Poll / Callback API).

Схема интеграции с VK (будущее):
- Вместо input() — обработчик события message_new из vkbottle (или аналога).
- Вместо print ответа — вызов API: messages.send(peer_id=..., message=text, ...).
"""

from __future__ import annotations

import json
import os
import random
import re
import sys
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Final

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None  # type: ignore[misc, assignment]

try:
    from openai import OpenAI
except ImportError:
    OpenAI = None  # type: ignore[misc, assignment]

from instruction_loader import get_instruction

# --- Заглушка: список шуток без внешних зависимостей (можно заменить на pyjokes) ---
_JOKES: Final[tuple[str, ...]] = (
    "Почему программисты путают Хэллоуин и Рождество? Потому что Oct 31 == Dec 25.",
    "У оптимиста стакан наполовину полон, у пессимиста — пуст. У программиста стакан в два раза больше, чем нужно.",
    "Как называется разработчик, который не пишет тесты? Пользователь.",
)

# Состояние диалога (в VK будет dict[peer_id, ...]).
SESSION: dict[str, Any] = {}
# Тексты инструкций для вызовов AI — в instructions.txt (get_instruction).

# Возврат к теме: разработка промпта под запрос пользователя (основной сценарий — ветка 1).
OFF_TOPIC_REDIRECT: Final[str] = (
    "Похоже, мы немного ушли от темы. Давайте вернёмся к разработке промпта по вашему запросу. "
    "Пожалуйста, опишите, что именно вы хотите получить от ИИ, чтобы я смог продолжить."
)

# Целые фразы (после нормализации), похожие на бытовой отвод от задачи «сформулировать промпт».
_OFF_TOPIC_EXACT: Final[frozenset[str]] = frozenset(
    {
        "привет",
        "привет!",
        "здравствуй",
        "здравствуйте",
        "хай",
        "hi",
        "hello",
        "hey",
        "как дела",
        "как дела?",
        "что как",
        "чо как",
        "как ты",
        "как ты?",
        "как жизнь",
        "как жизнь?",
        "чем занят",
        "чем занята",
        "ты тут",
        "ау",
        "алло",
        "добрый день",
        "добрый вечер",
        "доброе утро",
        "погода",
        "скажи погоду",
        "расскажи анекдот",
        "пошути",
        "поболтаем",
        "поговорим",
        "не хочу",
        "не знаю",
        "ладно",
        "ок",
        "окей",
        "давай другое",
        "хватит",
        "стоп",
        "замолчи",
        "ты кто",
        "ты кто?",
        "кто ты?",
        "а ты кто",
        "а ты кто?",
    }
)

_OFF_TOPIC_START: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(r"^как\s+дела\b", re.IGNORECASE),
    re.compile(r"^что\s+нового\b", re.IGNORECASE),
    re.compile(r"^как\s+настроение\b", re.IGNORECASE),
    re.compile(r"^(кто\s+ты|ты\s+кто|что\s+ты\s+за|ты\s+бот)\b", re.IGNORECASE),
    re.compile(r"^расскажи\s+(про\s+себя|сказку)\b", re.IGNORECASE),
    re.compile(r"^давай\s+про\s+", re.IGNORECASE),
)

# Вводные слова в начале реплики («а ты кто?»), после снятия проверяем ядро фразы.
_LEADING_CHATTER_PREFIX: Final[re.Pattern[str]] = re.compile(
    r"^(?:а|ну|ээ|эй|слушай(?:те)?|скажи(?:те)?|пожалуйста|извини(?:те)?|прости)\b[,!\s]*",
    re.IGNORECASE,
)


def _normalize_user_line(text: str) -> str:
    t = (text or "").strip().lower()
    t = re.sub(r"\s+", " ", t)
    return t


def _strip_leading_chatter_prefixes(t: str) -> str:
    s = t
    while True:
        m = _LEADING_CHATTER_PREFIX.match(s)
        if not m:
            break
        s = s[m.end() :].strip()
        if not s:
            break
    return s


def is_off_topic_user_input(text: str) -> bool:
    """
    Грубая эвристика: сообщение не похоже на формулировку задачи для ИИ, а на отвод разговора.
    Не вызывает API; длинные осмысленные тексты не режем.
    """
    t = _normalize_user_line(text)
    if not t:
        return False
    if len(t) > 160:
        return False
    if t in _OFF_TOPIC_EXACT:
        return True
    core = _strip_leading_chatter_prefixes(t)
    if not core:
        return False
    if core in _OFF_TOPIC_EXACT:
        return True
    # Ядро после «а/ну/…» короче; лимит по полной строке уже отсекает длинные тексты.
    if len(core) <= 48 and any(p.match(core) for p in _OFF_TOPIC_START):
        return True
    return False


def read_env() -> None:
    """Загрузка .env из каталога скрипта (как в основном агенте проекта)."""
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
    """Клиент OpenAI-совместимого proxy. Без ключа — None."""
    if OpenAI is None:
        return None
    read_env()
    key = (os.getenv("OPENAI_API_KEY") or "").strip()
    if not key:
        return None
    base = os.getenv("OPENAI_BASE_URL", "https://api.proxyapi.ru/openai/v1").strip()
    return OpenAI(api_key=key, base_url=base)


def _extract_json_object(text: str) -> dict[str, Any]:
    """Парсинг JSON из ответа модели (сырой JSON или блок в тексте)."""
    text = (text or "").strip()
    if not text:
        raise ValueError("Пустой ответ модели")
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            return data
    except json.JSONDecodeError:
        pass
    if text.startswith("```"):
        parts = text.split("```")
        for part in parts:
            p = part.strip()
            if p.lower().startswith("json"):
                p = p[4:].strip()
            if p.startswith("{") and p.endswith("}"):
                return json.loads(p)
    start_idxs = [m.start() for m in re.finditer(r"\{", text)]
    for start in start_idxs:
        depth = 0
        for i in range(start, len(text)):
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
                if depth == 0:
                    return json.loads(text[start : i + 1])
    raise ValueError("В ответе не найден JSON-объект")


def _chat_json_completion(client: Any, model: str, system: str, user: str) -> dict[str, Any]:
    """Один запрос chat.completions с response_format=json_object; возвращает распарсенный объект."""
    resp = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        temperature=0.2,
        response_format={"type": "json_object"},
    )
    raw = (resp.choices[0].message.content or "").strip()
    return _extract_json_object(raw)


def extract_text_task_via_ai(user_original: str) -> str:
    """
    Второй этап (текст): TEXT_EXTRACTION + user-сообщение. Для использования извне / тестов.

    TODO VK: тот же вызов из хендлера, ответ — в messages.send.
    """
    client = build_openai_client()
    if client is None:
        return (
            "Не настроен доступ к API: установите openai и python-dotenv, "
            "создайте .env с OPENAI_API_KEY (см. .env_test).\n"
            "Либо проверьте OPENAI_BASE_URL для вашего proxy."
        )

    model = os.getenv("OPENAI_MODEL", "gpt-4o-mini").strip()
    try:
        system_prompt = get_instruction("TEXT_EXTRACTION")
    except (FileNotFoundError, KeyError) as exc:
        return f"Не удалось загрузить инструкции для AI: {exc}"

    try:
        data = _chat_json_completion(client, model, system_prompt, user_original)
        return json.dumps(data, ensure_ascii=False, indent=2)
    except Exception as exc:
        return f"Ошибка при обращении к AI: {exc}"


def _normalize_branch(value: Any) -> int | None:
    """Приводит detected_branch из JSON к int 1..7 или None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and 1 <= value <= 7:
        return value
    if isinstance(value, str) and value.strip().isdigit():
        n = int(value.strip())
        if 1 <= n <= 7:
            return n
    return None


def _build_stage3_improver_user_message(
    session_user_text: str,
    structured_text_json: dict[str, Any],
) -> str:
    """
    Два JSON в одном user-сообщении: (1) как ввёл пользователь в сессии, (2) разбор TEXT_EXTRACTION.
    """
    json1 = {"original_user_message": session_user_text.strip()}
    return (
        "Ниже два JSON для обработки по system-инструкции.\n\n"
        "JSON 1 — исходный запрос пользователя (короткая формулировка в диалоге):\n"
        f"{json.dumps(json1, ensure_ascii=False, indent=2)}\n\n"
        "JSON 2 — структурированный разбор параметров текста (поля purpose, type, theme, audience, length, style, original_text и др.):\n"
        f"{json.dumps(structured_text_json, ensure_ascii=False, indent=2)}"
    )


_INTERNAL_REQUEST_MARKERS: Final[tuple[str, ...]] = (
    "для меня",
    "для себя",
    "самому разобраться",
    "сам разобраться",
    "лично мне",
    "мне нужно понять",
    "просто понять",
    "не для публикации",
    "не для блога",
    "не пост",
    "индивидуально",
)


def is_internal_request(comment_user: str | None) -> bool:
    """Уточнение пользователя про запрос «для себя», без внешней аудитории."""
    if not comment_user or not str(comment_user).strip():
        return False
    t = comment_user.lower()
    return any(m in t for m in _INTERNAL_REQUEST_MARKERS)


def extract_length_from_comment(comment_user: str | None) -> str | None:
    """
    Пытается извлечь требование по объёму из уточнения пользователя.
    Примеры: "5000 знаков", "до 3000 символов", "2000-3000 знаков".
    """
    if not comment_user:
        return None
    text = comment_user.lower()
    m_range = re.search(
        r"(\d{2,6})\s*[-–—]\s*(\d{2,6})\s*(?:знаков|символов)", text
    )
    if m_range:
        a = m_range.group(1)
        b = m_range.group(2)
        return f"{a}-{b} знаков"
    m_upto = re.search(r"(?:до|не\s+более)\s*(\d{2,6})\s*(?:знаков|символов)", text)
    if m_upto:
        return f"до {m_upto.group(1)} знаков"
    m_plain = re.search(r"(\d{2,6})\s*(?:знаков|символов)", text)
    if m_plain:
        return f"до {m_plain.group(1)} знаков"
    return None


def apply_length_hint_from_comment(
    spec: dict[str, Any], comment_user: str | None
) -> dict[str, Any]:
    """
    Если в comment_user явно указан объём, а поле length пустое — заполняет length.
    """
    out = dict(spec)
    length_hint = extract_length_from_comment(comment_user)
    if length_hint and _is_missing_text_param(out.get("length")):
        out["length"] = length_hint
    return out


def apply_internal_request_to_spec(
    spec: dict[str, Any], comment_user: str | None
) -> dict[str, Any]:
    """
    Для внутреннего запроса переписывает purpose, audience и type под объяснение
    самому пользователю; прежний type сохраняет в type_prev (если был задан).
    """
    out = dict(spec)
    if not is_internal_request(comment_user):
        return out
    prev_type = out.get("type")
    if not _is_missing_text_param(prev_type):
        out["type_prev"] = prev_type
    out["purpose"] = (
        "Объяснить тему самому пользователю для личного понимания и применения "
        "(без ориентации на внешнюю публикацию)"
    )
    out["audience"] = (
        "Сам автор запроса; индивидуальное изучение и разбор темы для себя"
    )
    out["type"] = "объяснение/гайд"
    return out


def maybe_adjust_type_for_explain(
    json2: dict[str, Any], comment_user: str | None
) -> dict[str, Any]:
    """
    Копия JSON 2 разбора текста; при уточнении про объяснение для себя
    подсказывает мета-промпту смену типа на «объяснение».
    """
    out = dict(json2)
    if not comment_user:
        return out
    comment_lower = comment_user.lower()
    trigger_words = (
        "объяснение",
        "объясни мне",
        "для меня",
        "разбор",
        "понять самому",
    )
    if any(w in comment_lower for w in trigger_words):
        if out.get("type") is not None:
            out["type_prev"] = out["type"]
        out["type"] = "объяснение"
    return out


def build_stage3_refinement_user_message(
    session_user_text: str,
    text_params: dict[str, Any],
    last_improver: dict[str, Any],
    comment_user: str,
) -> tuple[str, dict[str, Any]]:
    """User-текст для этапа 3 уточнения и обновлённый JSON 2 (патчи по comment_user)."""
    json2 = dict(text_params)
    json2 = apply_length_hint_from_comment(json2, comment_user)
    if is_internal_request(comment_user):
        json2 = apply_internal_request_to_spec(json2, comment_user)
    else:
        json2 = maybe_adjust_type_for_explain(json2, comment_user)
    base = _build_stage3_improver_user_message(session_user_text, json2)
    full = _append_stage3_refinement(base, last_improver, comment_user.strip())
    return full, json2


def _append_stage3_refinement(
    base_stage3_user: str,
    last_improver: dict[str, Any],
    comment_user: str,
) -> str:
    """Дополнение user-сообщения этапа 3: прошлый ответ улучшителя и уточнение пользователя."""
    return (
        f"{base_stage3_user}\n\n"
        "last_improver_response:\n"
        f"{json.dumps(last_improver, ensure_ascii=False, indent=2)}\n\n"
        f"comment_user:\n{comment_user.strip()}"
    )


def _stub_other_branch(_classifier: dict[str, Any], _branch: int) -> str:
    return OFF_TOPIC_REDIRECT


_BRANCH6_DETECTED_LINE: Final[str] = (
    "→ Я определил запрос как: Объяснить / разобрать тему."
)
_BRANCH6_CONFIRM_PROMPT: Final[str] = "Продолжаем обработку этой ветки? (y/n): "
_BRANCH6_CANCELLED: Final[str] = (
    "Ок, ветка 'Объяснить / разобрать тему' отменена пользователем."
)
_BRANCH6_STUB: Final[str] = (
    "Пока детальная логика ветки 'Объяснить / разобрать тему' не реализована. "
    "Возвращаюсь к ожиданию следующего запроса."
)
_BRANCH6_VK_NON_INTERACTIVE: Final[str] = (
    _BRANCH6_DETECTED_LINE
    + "\n\n"
    "В VK пока нет подтверждения y/n в чате. Сценарий «Объяснить / разобрать тему» не продолжается. "
    "Нажмите «написать текст» или опишите задачу в свободной форме — или дождитесь доработки этой ветки."
)


def _branch_6_console_confirm_flow() -> str:
    """Консоль: print + input; возвращает \"\" чтобы не дублировать вывод в send_reply_to_user."""
    print(_BRANCH6_DETECTED_LINE)
    try:
        confirm = input(_BRANCH6_CONFIRM_PROMPT).strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        print(_BRANCH6_CANCELLED)
        return ""
    if confirm not in ("y", "д", "да"):
        print(_BRANCH6_CANCELLED)
        return ""
    print(_BRANCH6_STUB)
    return ""


STAGE3_REFINEMENT_PROMPT_CONSOLE: Final[str] = (
    "Что-то уточнить? Пустой ввод — закончить уточнения."
)
STAGE3_REFINEMENT_PROMPT_CONSOLE_ALL_FILLED: Final[str] = (
    "Проверьте готовый промпт. При необходимости уточните цель, аудиторию, объём или стиль. "
    "Пустой ввод — закончить уточнения."
)
STAGE3_REFINEMENT_PROMPT_CONSOLE_SUBSEQUENT: Final[str] = (
    "Нужно что-то ещё изменить в промпте? Опишите ниже одним сообщением. Пустой ввод — закончить."
)
STAGE3_REFINEMENT_PROMPT_VK: Final[str] = (
    "Что-то уточнить? Напишите уточнение к промпту или нажмите «Готово», чтобы закончить."
)
# Первый цикл уточнения: модель заполнила все поля TEXT_EXTRACTION — всё равно конкретное приглашение проверить промпт.
STAGE3_REFINEMENT_PROMPT_VK_ALL_FILLED: Final[str] = (
    "Проверьте готовый промпт. При необходимости уточните цель, аудиторию, объём или стиль "
    "одним сообщением или нажмите «Готово», если всё устраивает."
)
STAGE3_REFINEMENT_PROMPT_VK_SUBSEQUENT: Final[str] = (
    "Нужно что-то ещё изменить в промпте? Напишите одним сообщением или нажмите «Готово»."
)

# Поля разбора TEXT_EXTRACTION (instructions.txt): подписи для вопроса о недостающих параметрах.
_TEXT_EXTRACTION_PARAM_LABELS: Final[tuple[tuple[str, str], ...]] = (
    ("purpose", "цель и место использования текста"),
    ("type", "тип текста"),
    ("theme", "тема"),
    ("audience", "аудитория"),
    ("length", "объём"),
    ("style", "стиль"),
)

# Метки клавиатуры для vk_dispatch_sync (второй аргумент emit).
VK_KB_BRANCH_MENU: Final[str] = "branch_menu"
VK_KB_REFINEMENT_DONE: Final[str] = "refinement_done"
# JSON улучшителя — без меню веток (пустая inline-клавиатура).
VK_KB_JSON_NO_MENU: Final[str] = "json_no_menu"
# То же меню веток, но текст — полное приветствие VK (подпись про серые кнопки уже внутри).
VK_KB_BRANCH_MENU_WELCOME: Final[str] = "branch_menu_welcome"

REFINEMENT_DONE_CMDS: Final[frozenset[str]] = frozenset(
    {
        "готово",
        "готово.",  # с точкой, если клиент VK добавит
        "достаточно",
        "хватит",
        "/done",
        ".",
        "ok",
        "ок",
        "окей",
        "спасибо, достаточно",
    }
)


@dataclass
class Stage3RefinementContext:
    """Состояние цикла уточнения этапа 3 (для VK и внешних транспортов без input())."""

    stage3_user: str
    last_improver: dict[str, Any]
    client: Any
    model: str
    system_improver: str
    text_params: dict[str, Any]
    post_improver_index: int
    session_user_text: str


@dataclass(frozen=True)
class _FirstImproverOk:
    improver_data: dict[str, Any]
    stage3_user: str
    client: Any
    model: str
    system_improver: str
    text_params: dict[str, Any]
    session_user_text: str


_PLACEHOLDER_TREATED_AS_MISSING: Final[frozenset[str]] = frozenset(
    {
        "",
        "null",
        "none",
        "n/a",
        "na",
        "—",
        "-",
        "…",
        "...",
        "не указано",
        "неизвестно",
        "не указан",
        "не указана",
        "не указаны",
        "unspecified",
    }
)


def _is_missing_text_param(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, bool):
        return False
    if isinstance(value, str):
        t = value.strip().lower()
        if not t:
            return True
        if t in _PLACEHOLDER_TREATED_AS_MISSING:
            return True
        return False
    return False


# Поля разбора TEXT_EXTRACTION, по которым решаем «мало данных» (кроме type).
_SPEC_CLARIFY_KEYS: Final[tuple[str, ...]] = (
    "theme",
    "purpose",
    "audience",
    "length",
    "style",
)

# Расширяемый список (ключ, вопрос в консоли) для clarify_spec_interactively.
_CLARIFY_FIELD_PROMPTS: Final[tuple[tuple[str, str], ...]] = (
    (
        "theme",
        "По какой теме нужен текст (кратко, о чём гайд/статья/пост)? ",
    ),
    (
        "audience",
        "Для кого это пишется (аудитория: студенты, разработчики, предприниматели, сотрудники и т.п.)? ",
    ),
    (
        "purpose",
        "Какова цель текста: объяснить, обучить, продать, мотивировать, дать инструкцию и т.п.? ",
    ),
    (
        "length",
        "Нужен короткий, средний или длинный текст? ",
    ),
    (
        "style",
        "Какой стиль вам подходит: нейтральный, деловой, дружелюбный, экспертный, другой? ",
    ),
)

_INSUFFICIENT_SPEC_INTRO: Final[str] = (
    "Недостаточно данных для составления качественного промпта. Пожалуйста, уточните задачу.\n"
)


def needs_clarification(spec: dict[str, Any]) -> bool:
    """
    True, если задан тип текста, но theme/purpose/audience/length/style все пустые —
    запрос слишком краткий для осмысленного этапа улучшения промпта.
    """
    if not all(_is_missing_text_param(spec.get(k)) for k in _SPEC_CLARIFY_KEYS):
        return False
    return not _is_missing_text_param(spec.get("type"))


def clarify_spec_interactively(spec: dict[str, Any]) -> dict[str, Any]:
    """
    Задаёт в консоли вопросы по полям из _CLARIFY_FIELD_PROMPTS и записывает непустые ответы в копию spec.
    original_text и type не меняются. Пустой ввод — поле не перезаписывается.
    """
    out = dict(spec)
    print(_INSUFFICIENT_SPEC_INTRO)
    for key, prompt in _CLARIFY_FIELD_PROMPTS:
        try:
            ans = input(prompt).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if ans:
            out[key] = ans
    return out


_INSUFFICIENT_SPEC_VK: Final[str] = (
    "Недостаточно данных для составления качественного промпта.\n\n"
    "Опишите в следующем сообщении: тему, аудиторию, цель, желаемый объём и стиль — "
    "или отправьте более подробную формулировку задачи одним сообщением."
)


def _missing_params_question_text(text_params: dict[str, Any]) -> str | None:
    """Текст с перечислением незаполненных полей разбора текста; None — все поля заданы."""
    missing_labels: list[str] = []
    for key, label in _TEXT_EXTRACTION_PARAM_LABELS:
        if _is_missing_text_param(text_params.get(key)):
            missing_labels.append(label)
    if not missing_labels:
        return None
    if len(missing_labels) == 1:
        head = f"Не указан параметр: {missing_labels[0]}."
    else:
        head = "Не полностью заданы параметры: " + ", ".join(missing_labels) + "."
    return head


def _vk_refinement_followup_question(text_params: dict[str, Any], post_improver_index: int) -> str:
    """Первый ответ ИИ (index 1): вопрос по недостающим полям; далее — короткий текст про доработку."""
    if post_improver_index <= 1:
        concrete = _missing_params_question_text(text_params)
        if concrete:
            return (
                concrete
                + "\n\nУточните одним сообщением, что добавить к запросу "
                "(или нажмите «Готово», если готовый промпт вас устраивает)."
            )
        return STAGE3_REFINEMENT_PROMPT_VK_ALL_FILLED
    return STAGE3_REFINEMENT_PROMPT_VK_SUBSEQUENT


def _console_refinement_followup_question(text_params: dict[str, Any], post_improver_index: int) -> str:
    if post_improver_index <= 1:
        concrete = _missing_params_question_text(text_params)
        if concrete:
            return concrete + "\n\nПустой ввод — закончить уточнения."
        return STAGE3_REFINEMENT_PROMPT_CONSOLE_ALL_FILLED
    return STAGE3_REFINEMENT_PROMPT_CONSOLE_SUBSEQUENT


def _improver_output_for_user(data: dict[str, Any]) -> str:
    """
    Сообщение пользователю после этапа 3: только поле new_prompt.
    Если поля нет или оно не строка — весь JSON (для отладки / нетипичный ответ модели).
    """
    v = data.get("new_prompt")
    if isinstance(v, str):
        return v.strip()
    if v is not None and not isinstance(v, str):
        return str(v).strip()
    return json.dumps(data, ensure_ascii=False, indent=2)


def _run_stages_through_first_improver(
    user_text: str,
    *,
    force_branch_1: bool = False,
    force_branch_6: bool = False,
    branch6_interactive: bool = True,
    spec_clarification_interactive: bool = True,
) -> str | _FirstImproverOk:
    """
    Этапы 1–3 до первого успешного ответа улучшителя; иначе строка ошибки или редирект.

    Если force_branch_1=True, этап классификатора пропускается (как при detected_branch=1, confidence=high);
    в TEXT_EXTRACTION уходит весь user_text.
    Если force_branch_6=True, этап классификатора также пропускается и запрос обрабатывается как ветка explain;
    на этапе 3 используется PROMPT_IMPROVER_EXPLAIN вместо PROMPT_IMPROVER.

    branch6_interactive: для detected_branch==6 в консоли — print + input(y/n); для VK передать False
    (одно текстовое сообщение без stdin).

    spec_clarification_interactive: если needs_clarification(text_data), в консоли — вопросы input;
    для VK False — вернуть текст без вызова улучшителя промпта.
    """
    client = build_openai_client()
    if client is None:
        return (
            "Не настроен доступ к API: установите openai и python-dotenv, "
            "создайте .env с OPENAI_API_KEY.\n"
            "Либо проверьте OPENAI_BASE_URL для вашего proxy."
        )

    model = os.getenv("OPENAI_MODEL", "gpt-4o-mini").strip()

    if force_branch_1 or force_branch_6:
        user_req = user_text.strip()
        if not user_req:
            return "Опишите запрос текстом."
        is_explain_branch = bool(force_branch_6)
    else:
        try:
            system_cls = get_instruction("system_prompt")
        except (FileNotFoundError, KeyError) as exc:
            return f"Не удалось загрузить system_prompt из instructions.txt: {exc}"

        try:
            cls_data = _chat_json_completion(client, model, system_cls, user_text)
        except Exception as exc:
            return f"Ошибка классификатора (этап 1): {exc}"

        branch = _normalize_branch(cls_data.get("detected_branch"))
        if branch is None:
            return (
                "Классификатор вернул неожиданное значение detected_branch.\n"
                f"(ответ этапа 1): {json.dumps(cls_data, ensure_ascii=False, indent=2)}\n\n"
                "Отправьте новый запрос или /menu."
            )

        if branch not in (1, 6):
            return _stub_other_branch(cls_data, branch)

        is_explain_branch = branch == 6

        user_req = cls_data.get("user_request")
        if not isinstance(user_req, str):
            user_req = ""
        user_req = user_req.strip()
        if not user_req:
            user_req = user_text.strip()

    try:
        system_txt = get_instruction("TEXT_EXTRACTION")
    except (FileNotFoundError, KeyError) as exc:
        return f"Ветки 1/6: не загружен TEXT_EXTRACTION: {exc}"

    try:
        text_data = _chat_json_completion(client, model, system_txt, user_req)
    except Exception as exc:
        return f"Ошибка второго этапа (разбор текста): {exc}"

    if needs_clarification(text_data):
        if spec_clarification_interactive:
            text_data = clarify_spec_interactively(text_data)
        else:
            return _INSUFFICIENT_SPEC_VK

    improver_name = "PROMPT_IMPROVER_EXPLAIN" if is_explain_branch else "PROMPT_IMPROVER"
    try:
        system_improver = get_instruction(improver_name)
    except (FileNotFoundError, KeyError) as exc:
        return f"Этап 3 ({improver_name}): инструкция не загружена: {exc}"

    stage3_user = _build_stage3_improver_user_message(user_text, text_data)
    try:
        improver_data = _chat_json_completion(client, model, system_improver, stage3_user)
    except Exception as exc:
        return f"Ошибка третьего этапа (улучшение промпта): {exc}"

    return _FirstImproverOk(
        improver_data=improver_data,
        stage3_user=stage3_user,
        client=client,
        model=model,
        system_improver=system_improver,
        text_params=text_data,
        session_user_text=user_text.strip(),
    )


def vk_dispatch_sync(
    text: str,
    emit: Callable[[str, str], None],
    pending: Stage3RefinementContext | None,
    *,
    force_branch_1: bool = False,
    force_branch_6: bool = False,
) -> Stage3RefinementContext | None:
    """
    Обработка одного сообщения в VK-сессии. Ответы через emit(text, keyboard_key):
    VK_KB_BRANCH_MENU — меню веток; VK_KB_JSON_NO_MENU — текст поля new_prompt без кнопок;
    VK_KB_REFINEMENT_DONE — вопрос про уточнение (после первого результата — про недостающие параметры разбора текста) и кнопка «Готово».
    """
    line = (text or "").strip()
    cmd0 = line.split()[0].lower() if line else ""

    def em(msg: str, kb: str = VK_KB_BRANCH_MENU) -> None:
        emit(msg, kb)

    if cmd0 in ("/menu", "/start", "меню"):
        em(format_welcome_vk_menu_message(), VK_KB_BRANCH_MENU_WELCOME)
        return None

    if cmd0 == "/help":
        em(cmd_help())
        return pending

    if cmd0 == "/weather":
        em(cmd_weather())
        return pending

    if cmd0 == "/joke":
        em(cmd_joke())
        return pending

    if pending is not None:
        if not line:
            em("Напишите уточнение или нажмите «Готово».", VK_KB_REFINEMENT_DONE)
            return pending

        if line.lower() in REFINEMENT_DONE_CMDS:
            em("Уточнения завершены. Можете ввести новый запрос.")
            return None

        if is_off_topic_user_input(line):
            em(OFF_TOPIC_REDIRECT)
            em("Уточнения завершены. Опишите новую задачу для промпта.")
            return None

        refined_user, text_params_adj = build_stage3_refinement_user_message(
            pending.session_user_text,
            pending.text_params,
            pending.last_improver,
            line,
        )
        try:
            last_improver = _chat_json_completion(
                pending.client,
                pending.model,
                pending.system_improver,
                refined_user,
            )
        except Exception as exc:
            em(f"Ошибка уточнения промпта (этап 3): {exc}")
            return None

        em(_improver_output_for_user(last_improver), VK_KB_JSON_NO_MENU)
        next_pi = pending.post_improver_index + 1
        new_base = _build_stage3_improver_user_message(
            pending.session_user_text, text_params_adj
        )
        em(_vk_refinement_followup_question(text_params_adj, next_pi), VK_KB_REFINEMENT_DONE)
        return Stage3RefinementContext(
            stage3_user=new_base,
            last_improver=last_improver,
            client=pending.client,
            model=pending.model,
            system_improver=pending.system_improver,
            text_params=text_params_adj,
            post_improver_index=next_pi,
            session_user_text=pending.session_user_text,
        )

    if not line:
        em("Опишите запрос на промпт текстом или отправьте /menu.")
        return None

    if is_off_topic_user_input(line):
        em(OFF_TOPIC_REDIRECT)
        return None

    first = _run_stages_through_first_improver(
        line,
        force_branch_1=force_branch_1,
        force_branch_6=force_branch_6,
        branch6_interactive=False,
        spec_clarification_interactive=False,
    )
    if isinstance(first, str):
        em(first)
        return None

    em(_improver_output_for_user(first.improver_data), VK_KB_JSON_NO_MENU)
    em(_vk_refinement_followup_question(first.text_params, 1), VK_KB_REFINEMENT_DONE)
    return Stage3RefinementContext(
        stage3_user=first.stage3_user,
        last_improver=first.improver_data,
        client=first.client,
        model=first.model,
        system_improver=first.system_improver,
        text_params=first.text_params,
        post_improver_index=1,
        session_user_text=first.session_user_text,
    )


def run_prompt_pipeline(
    user_text: str,
    *,
    stage3_emit: Callable[[str], None] | None = None,
    refinement_reader: Callable[[], str | None] | None = None,
) -> str:
    """
    Этап 1: классификатор (system_prompt).
    Ветки 1 и 6: этап 2 — TEXT_EXTRACTION (в user только user_request из этапа 1);
    этап 3 — PROMPT_IMPROVER (ветка «текст») или PROMPT_IMPROVER_EXPLAIN (ветка «объяснить») + два JSON.
    Итог при ветках 1/6: пользователю показывается только new_prompt из ответа этапа 3. Иначе — заглушка.

    Если передан stage3_emit (консоль), после каждого ответа этапа 3 вызывается emit(new_prompt);
    затем вопрос про уточнение; ввод обрабатывается refinement_reader (по умолчанию read_user_message)
    или пустой ввод завершает цикл.
    """
    first = _run_stages_through_first_improver(user_text, force_branch_1=False)
    if isinstance(first, str):
        return first

    improver_data = first.improver_data
    stage3_user = first.stage3_user
    client = first.client
    model = first.model
    system_improver = first.system_improver
    session_user_text = first.session_user_text

    out_text = _improver_output_for_user(improver_data)
    if stage3_emit is None:
        return out_text

    stage3_emit(out_text)
    last_improver: dict[str, Any] = improver_data
    text_params_snapshot = dict(first.text_params)
    post_improver_index = 1
    _reader = refinement_reader if refinement_reader is not None else (lambda: read_user_message("Уточнение: "))
    while True:
        stage3_emit(_console_refinement_followup_question(text_params_snapshot, post_improver_index))
        ref = _reader()
        if ref is None:
            break
        if not ref.strip():
            break
        if is_off_topic_user_input(ref):
            stage3_emit(OFF_TOPIC_REDIRECT)
            break
        refined_user, text_params_snapshot = build_stage3_refinement_user_message(
            session_user_text,
            text_params_snapshot,
            last_improver,
            ref.strip(),
        )
        stage3_user = _build_stage3_improver_user_message(
            session_user_text, text_params_snapshot
        )
        try:
            last_improver = _chat_json_completion(client, model, system_improver, refined_user)
        except Exception as exc:
            return f"Ошибка уточнения промпта (этап 3): {exc}"
        stage3_emit(_improver_output_for_user(last_improver))
        post_improver_index += 1

    return "Уточнения завершены. Можете ввести новый запрос или /menu."


# Общий текст приветствия (консоль и VK). Без строки «Команды…» и без абзаца про серые кнопки — их добавляют отдельно.
_WELCOME_BODY: Final[str] = (
    "Привет! Я создаю «промпты» (запросы) для ИИ, чтобы другие нейросети выдавали именно то, что вам нужно.\n"
    "\n"
    "Просто опишите задачу:\n"
    "• «Хочу статью о Python для новичков»\n"
    "• «Нужна картинка космического корабля»\n"
    "• «Напиши код бота для Telegram»\n"
    "• «Объясни квантовую физику простыми словами»\n"
    "\n"
    "Я сделаю «готовый промпт», который вы скопируете и вставите в ChatGPT, Midjourney или любой другой ИИ.\n"
    "\n"
    "Можете добавить:\n"
    "- Для кого (школьники, разработчики...)\n"
    "- Длина (500 слов, 10 слайдов...)\n"
    "- Где использовать (сайт, Telegram...)\n"
    "\n"
    "Что за промпт нужен?"
)

_WELCOME_COMMANDS_SUFFIX_CONSOLE: Final[str] = (
    "\n\nКоманды: /menu или /start — повторить это сообщение · /help · /exit"
)


def format_welcome() -> str:
    """Приветствие и приглашение сформулировать запрос на промпт (консоль: со строкой команд)."""
    return _WELCOME_BODY + _WELCOME_COMMANDS_SUFFIX_CONSOLE


# Абзац про серые кнопки в конце приветствия VK (не дублировать через text_with_branch_stub_note).
VK_GREY_BUTTONS_FOOTER: Final[str] = (
    "Серые кнопки — сценарии в разработке. "
    "Уже работают: зелёные «написать текст», «объяснить» или обычное текстовое описание задачи."
)


def format_welcome_body_vk() -> str:
    """Текст приветствия для VK без абзаца про серые кнопки и без строки команд консоли."""
    return _WELCOME_BODY


def format_welcome_vk_menu_message() -> str:
    """Приветствие для VK после «Старт»: основной текст, затем абзац про серые кнопки (клавиатура — отдельно)."""
    return format_welcome_body_vk() + "\n\n" + VK_GREY_BUTTONS_FOOTER


def cmd_help() -> str:
    """Текст справки по командам."""
    return (
        "Доступные команды:\n"
        "  (свободный текст) — ветка 1: классификатор → разбор → улучшенный промпт (JSON); в консоли затем можно уточнять промпт до пустого ввода; иначе заглушка\n"
        "  /menu, /start — повторить приветствие\n"
        "  /help    — этот список\n"
        "  /weather — заглушка погоды\n"
        "  /joke    — случайная шутка\n"
        "  /exit    — выход из программы"
    )


def cmd_weather() -> str:
    """Заглушка погоды; позже — реальный API или сервис."""
    return (
        "Погода (заглушка): в Москве +5 °C, облачно.\n"
        "(Здесь позже: запрос к погодному API и форматирование ответа.)"
    )


def cmd_joke() -> str:
    """Случайная шутка из встроенного списка."""
    return random.choice(_JOKES)


def _reset_session() -> None:
    SESSION.clear()


def handle_message(
    text: str,
    *,
    stage3_emit: Callable[[str], None] | None = None,
) -> str | None:
    """
    Обрабатывает одно входящее сообщение как текст от пользователя.

    Возвращает:
        str — ответ бота для отправки пользователю;
        None — специальный сигнал «завершить диалог» (аналог /exit).

    Интеграция с VK (заглушка):
        # peer_id = message.peer_id  # из объекта Message
        # user_text = message.text or ""
        # reply = handle_message(user_text)  # + хранить SESSION по peer_id
        # if reply is None:
        #     return  # или не отвечать / закрыть сессию по политике бота
        # await api.messages.send(peer_id=peer_id, message=reply, random_id=..., group_id=...)
    """
    line = (text or "").strip()
    if not line:
        return "Опишите запрос на промпт текстом или введите /menu, /help."

    cmd = line.split()[0].lower()

    if cmd in ("/exit", "/quit"):
        _reset_session()
        return None

    if cmd in ("/menu", "/start", "меню"):
        _reset_session()
        return format_welcome()

    if cmd == "/help":
        return cmd_help()

    if cmd == "/weather":
        return cmd_weather()

    if cmd == "/joke":
        return cmd_joke()

    if is_off_topic_user_input(line):
        return OFF_TOPIC_REDIRECT

    # Классификатор → при ветке 1 второй ИИ-вызов только с user_request
    return run_prompt_pipeline(line, stage3_emit=stage3_emit)


def send_reply_to_user(outgoing_text: str) -> None:
    """
    «Отправка» ответа пользователю.

    Сейчас: печать в консоль.
    Позже — VK API:

        # TODO VK: await ctx_api.messages.send(
        #     peer_ids=[peer_id],
        #     message=outgoing_text,
        #     random_id=unique_random_int(),
        #     group_id=community_id,  # при токене сообщества — по необходимости
        # )
    """
    print(outgoing_text)


def read_user_message(prompt: str = "Вы: ") -> str | None:
    """
    «Получение» сообщения от пользователя.

    Сейчас: input() из консоли.
    Позже — событие из VK:

        # TODO VK: текст приходит из @bot.on.message() async def handler(message: Message)
        # return message.text
    """
    try:
        return input(prompt).rstrip("\n")
    except (EOFError, KeyboardInterrupt):
        print()
        return None


def run_console_loop() -> None:
    """
    Главный цикл: приветствие → ввод → handle_message → вывод, пока не /exit.

    Интеграция с VK (заглушка):
        # TODO VK: вместо этого цикла — bot.run_forever() и хендлеры событий;
        # для каждого message_new вызывать handle_message и send через API.
    """
    # TODO VK: при первом контакте отправить приветствие через messages.send (как format_welcome())
    send_reply_to_user(format_welcome())

    while True:
        raw = read_user_message()
        if raw is None:
            print("Завершение.")
            break

        reply = handle_message(raw, stage3_emit=send_reply_to_user)
        if reply is None:
            print("До свидания!")
            break

        if reply:
            send_reply_to_user(reply)
        # TODO VK: при необходимости здесь же обновлять клавиатуру (keyboard=...)


def main() -> int:
    run_console_loop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
