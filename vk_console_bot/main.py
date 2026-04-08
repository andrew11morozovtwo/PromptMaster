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


STAGE3_REFINEMENT_PROMPT_CONSOLE: Final[str] = (
    "Что-то уточнить? Пустой ввод — закончить уточнения."
)
STAGE3_REFINEMENT_PROMPT_VK: Final[str] = (
    "Что-то уточнить? Напишите уточнение к промпту или нажмите «Готово», чтобы закончить."
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


@dataclass(frozen=True)
class _FirstImproverOk:
    improver_data: dict[str, Any]
    stage3_user: str
    client: Any
    model: str
    system_improver: str


def _run_stages_through_first_improver(
    user_text: str,
    *,
    force_branch_1: bool = False,
) -> str | _FirstImproverOk:
    """
    Этапы 1–3 до первого успешного ответа улучшителя; иначе строка ошибки или редирект.

    Если force_branch_1=True, этап классификатора пропускается (как при detected_branch=1, confidence=high);
    в TEXT_EXTRACTION уходит весь user_text.
    """
    client = build_openai_client()
    if client is None:
        return (
            "Не настроен доступ к API: установите openai и python-dotenv, "
            "создайте .env с OPENAI_API_KEY.\n"
            "Либо проверьте OPENAI_BASE_URL для вашего proxy."
        )

    model = os.getenv("OPENAI_MODEL", "gpt-4o-mini").strip()

    if force_branch_1:
        user_req = user_text.strip()
        if not user_req:
            return "Опишите запрос текстом."
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

        if branch != 1:
            return _stub_other_branch(cls_data, branch)

        user_req = cls_data.get("user_request")
        if not isinstance(user_req, str):
            user_req = ""
        user_req = user_req.strip()
        if not user_req:
            user_req = user_text.strip()

    try:
        system_txt = get_instruction("TEXT_EXTRACTION")
    except (FileNotFoundError, KeyError) as exc:
        return f"Ветка 1, но не загружен TEXT_EXTRACTION: {exc}"

    try:
        text_data = _chat_json_completion(client, model, system_txt, user_req)
    except Exception as exc:
        return f"Ошибка второго этапа (разбор текста): {exc}"

    try:
        system_improver = get_instruction("PROMPT_IMPROVER")
    except (FileNotFoundError, KeyError) as exc:
        return f"Ветка 1: этап 2 выполнен, но не загружен PROMPT_IMPROVER: {exc}"

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
    )


def vk_dispatch_sync(
    text: str,
    emit: Callable[[str, str], None],
    pending: Stage3RefinementContext | None,
    *,
    refinement_question: str = STAGE3_REFINEMENT_PROMPT_VK,
    force_branch_1: bool = False,
) -> Stage3RefinementContext | None:
    """
    Обработка одного сообщения в VK-сессии. Ответы через emit(text, keyboard_key):
    VK_KB_BRANCH_MENU — меню веток; VK_KB_JSON_NO_MENU — ответ с промптом (JSON) без кнопок;
    VK_KB_REFINEMENT_DONE — вопрос про уточнение и одна кнопка «Готово».
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

        refined_user = _append_stage3_refinement(pending.stage3_user, pending.last_improver, line)
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

        em(json.dumps(last_improver, ensure_ascii=False, indent=2), VK_KB_JSON_NO_MENU)
        em(refinement_question, VK_KB_REFINEMENT_DONE)
        return Stage3RefinementContext(
            stage3_user=pending.stage3_user,
            last_improver=last_improver,
            client=pending.client,
            model=pending.model,
            system_improver=pending.system_improver,
        )

    if not line:
        em("Опишите запрос на промпт текстом или отправьте /menu.")
        return None

    if is_off_topic_user_input(line):
        em(OFF_TOPIC_REDIRECT)
        return None

    first = _run_stages_through_first_improver(line, force_branch_1=force_branch_1)
    if isinstance(first, str):
        em(first)
        return None

    em(json.dumps(first.improver_data, ensure_ascii=False, indent=2), VK_KB_JSON_NO_MENU)
    em(refinement_question, VK_KB_REFINEMENT_DONE)
    return Stage3RefinementContext(
        stage3_user=first.stage3_user,
        last_improver=first.improver_data,
        client=first.client,
        model=first.model,
        system_improver=first.system_improver,
    )


def run_prompt_pipeline(
    user_text: str,
    *,
    stage3_emit: Callable[[str], None] | None = None,
    refinement_reader: Callable[[], str | None] | None = None,
) -> str:
    """
    Этап 1: классификатор (system_prompt).
    Ветка 1: этап 2 — TEXT_EXTRACTION (в user только user_request из этапа 1);
    этап 3 — PROMPT_IMPROVER + два JSON (исходная реплика + разбор этапа 2).
    Итог при ветке 1: JSON этапа 3 (old_prompt, new_prompt, advantages). Иначе — заглушка.

    Если передан stage3_emit (консоль), после каждого JSON этапа 3 вызывается emit(json);
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

    out_json = json.dumps(improver_data, ensure_ascii=False, indent=2)
    if stage3_emit is None:
        return out_json

    stage3_emit(out_json)
    last_improver: dict[str, Any] = improver_data
    _reader = refinement_reader if refinement_reader is not None else (lambda: read_user_message("Уточнение: "))
    while True:
        stage3_emit(STAGE3_REFINEMENT_PROMPT_CONSOLE)
        ref = _reader()
        if ref is None:
            break
        if not ref.strip():
            break
        if is_off_topic_user_input(ref):
            stage3_emit(OFF_TOPIC_REDIRECT)
            break
        refined_user = _append_stage3_refinement(stage3_user, last_improver, ref.strip())
        try:
            last_improver = _chat_json_completion(client, model, system_improver, refined_user)
        except Exception as exc:
            return f"Ошибка уточнения промпта (этап 3): {exc}"
        stage3_emit(json.dumps(last_improver, ensure_ascii=False, indent=2))

    return "Уточнения завершены. Можете ввести новый запрос или /menu."


def format_welcome() -> str:
    """Приветствие и приглашение сформулировать запрос на промпт."""
    return (
        "Привет! Я создаю «промпты» (запросы) для ИИ, чтобы другие нейросети выдавали именно то, что вам нужно.\n"
        "\n"
        "Просто опишите задачу:\n"
        '• «Хочу статью о Python для новичков»\n'
        '• «Нужна картинка космического корабля»\n'
        '• «Напиши код бота для Telegram»\n'
        '• «Объясни квантовую физику простыми словами»\n'
        "\n"
        "Я сделаю «готовый промпт», который вы скопируете и вставите в ChatGPT, Midjourney или любой другой ИИ.\n"
        "\n"
        "Можете добавить:\n"
        "- Для кого (школьники, разработчики...)\n"
        "- Длина (500 слов, 10 слайдов...)\n"
        "- Где использовать (сайт, Telegram...)\n"
        "\n"
        "Что за промпт нужен?\n"
        "\n"
        "Команды: /menu или /start — повторить это сообщение · /help · /exit"
    )


_WELCOME_COMMANDS_SUFFIX_VK: Final[str] = (
    "\n\nКоманды: /menu или /start — повторить это сообщение · /help · /exit"
)

# Абзац про серые кнопки в конце приветствия VK (не дублировать через text_with_branch_stub_note).
VK_GREY_BUTTONS_FOOTER: Final[str] = (
    "Серые кнопки — сценарии в разработке. "
    "Уже работают: зелёная «написать текст» или обычное текстовое описание задачи."
)


def format_welcome_body_vk() -> str:
    """Текст приветствия без последней строки про команды (для VK)."""
    full = format_welcome()
    if full.endswith(_WELCOME_COMMANDS_SUFFIX_VK):
        return full[: -len(_WELCOME_COMMANDS_SUFFIX_VK)].rstrip()
    return full.rstrip()


def format_welcome_vk_menu_message() -> str:
    """Приветствие для VK: основной текст, затем абзац про серые кнопки."""
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

        send_reply_to_user(reply)
        # TODO VK: при необходимости здесь же обновлять клавиатуру (keyboard=...)


def main() -> int:
    run_console_loop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
