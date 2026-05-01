"""
VK-бот PromptMaster: Long Poll (vkbottle), inline-клавиатура, та же логика, что у `vk_console_bot`.

Движок пайплайна и `instructions.txt` — в каталоге `vk_console_bot` (родительский репозиторий).

Токен сообщества: `VK_GROUP_TOKEN` или `vk_group_token`.
OpenAI: `OPENAI_API_KEY` (корень репо, `vk_prompt_bot/.env` или `vk_console_bot/.env`).
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from vkbottle import Keyboard, KeyboardButtonColor, Text
from vkbottle.bot import Bot, Message

_ROOT = Path(__file__).resolve().parent.parent
_VK_CONSOLE = _ROOT / "vk_console_bot"
if _VK_CONSOLE.is_dir():
    sys.path.insert(0, str(_VK_CONSOLE))

from bot_database import EventKind, PromptBotDatabase  # noqa: E402

from main import (  # noqa: E402
    REFINEMENT_DONE_CMDS,
    VK_GREY_BUTTONS_FOOTER,
    VK_KB_BRANCH_MENU,
    VK_KB_BRANCH_MENU_WELCOME,
    VK_KB_JSON_NO_MENU,
    VK_KB_REFINEMENT_DONE,
    Stage3RefinementContext,
    format_welcome_vk_menu_message,
    vk_dispatch_sync,
)

START_CMDS = frozenset({"/start", "/старт", "старт"})
EXIT_CMDS = frozenset({"/выход", "/exit", "/stop", "/стоп", "стоп"})
CONTINUE_CMDS = frozenset({"продолжить"})
# vk_dispatch_sync: ответы без вызова LLM (не списывать paid_request_units).
_DISPATCH_NO_LLM_PREFIXES = frozenset(
    {"/menu", "/start", "меню", "/help", "/weather", "/joke"}
)

BTN_WRITE_TEXT = "написать текст"
BTN_STOP = "Стоп"

BTN_DRAW = "нарисовать картинку"
BTN_VIDEO = "снять видео"
BTN_SONG = "написать песню"
BTN_CODE = "написать код"
BTN_EXPLAIN = "объяснить"
BTN_OTHER = "другое"

STUB_SCENARIO_BUTTONS: frozenset[str] = frozenset(
    {
        BTN_DRAW,
        BTN_VIDEO,
        BTN_SONG,
        BTN_CODE,
        BTN_OTHER,
    }
)


def text_with_branch_stub_note(body: str) -> str:
    return VK_GREY_BUTTONS_FOOTER + "\n\n" + body


BTN_DONE = "Готово"

IDLE_SECONDS = 180

_MSG_NEED_START_AGAIN = (
    "Вы вышли из сессии. Нажмите «Старт», чтобы снова составить промпт для ИИ."
)

MSG_IDLE_TIMEOUT = _MSG_NEED_START_AGAIN
MSG_EXIT = _MSG_NEED_START_AGAIN

MSG_THINKING_PROMPT = "Я думаю, как улучшить ваш промпт."
MSG_NEED_TEXT = "Нужно текстовое сообщение."
MSG_CONTINUE = (
    "Кнопка в разработке. Опишите задачу текстом или отправьте /help."
)
MSG_AWAIT_TEXT_AFTER_BUTTON = (
    "Опишите задачу одним сообщением: какой текст нужен от ИИ, для кого, объём и стиль."
)
MSG_AWAIT_EXPLAIN_AFTER_BUTTON = (
    "Опишите одним сообщением, что нужно объяснить: тему, желаемую глубину и стиль."
)
MSG_SCENARIO_IN_DEVELOPMENT = (
    "Этот сценарий в разработке. Используйте «написать текст» или опишите задачу в свободной форме."
)


def _vk_random_id() -> int:
    return random.randint(1, 2**31 - 1)


def _actor_id(message: Message) -> int:
    uid = getattr(message, "from_id", None)
    if uid is not None:
        return int(uid)
    return int(message.peer_id)


def _vk_dispatch_is_llm_free(stripped: str) -> bool:
    tok = stripped.strip().lower().split()[0] if stripped.strip() else ""
    return tok in _DISPATCH_NO_LLM_PREFIXES


def _snippet(text: str, max_len: int = 500) -> str:
    t = (text or "").replace("\n", " ").strip()
    if len(t) <= max_len:
        return t
    return t[: max_len - 1] + "…"


async def _audit(db: PromptBotDatabase, **kwargs: Any) -> int | None:
    try:
        return await asyncio.to_thread(db.log_event, **kwargs)
    except Exception as exc:
        print(f"audit log failed: {exc}", file=sys.stderr)
        return None


async def _log_user_ai_turn(
    db: PromptBotDatabase,
    uid: int,
    peer_id: int,
    user_raw: str,
    outgoing: list[tuple[str, str]],
    *,
    context: str,
    bill_llm: bool,
) -> None:
    eid = await _audit(
        db,
        vk_user_id=uid,
        peer_id=peer_id,
        kind=EventKind.USER_TEXT,
        summary=f"Ввод ({context}): {_snippet(user_raw)}",
        payload={"text": user_raw, "context": context},
    )
    for part, kb_kind in outgoing:
        if not part:
            continue
        await _audit(
            db,
            vk_user_id=uid,
            peer_id=peer_id,
            kind=EventKind.AI_TEXT,
            summary=f"Ответ ИИ: {_snippet(part)}",
            payload={"text": part, "keyboard_kind": kb_kind},
        )
    if bill_llm:
        try:
            await asyncio.to_thread(
                db.record_paid_unit,
                vk_user_id=uid,
                peer_id=peer_id,
                unit_type="prompt_pipeline",
                bot_event_id=eid,
                settled=True,
                metadata={"context": context},
            )
        except Exception as exc:
            print(f"paid_unit log failed: {exc}", file=sys.stderr)




def start_only_keyboard_json() -> str:
    return (
        Keyboard(one_time=False, inline=True)
        .add(Text("Старт"), KeyboardButtonColor.POSITIVE)
        .get_json()
    )


def branch_menu_keyboard_json() -> str:
    kb = Keyboard(one_time=False, inline=True)
    kb.add(Text(BTN_WRITE_TEXT), KeyboardButtonColor.POSITIVE).row()
    kb.add(Text(BTN_DRAW), KeyboardButtonColor.SECONDARY).add(
        Text(BTN_VIDEO),
        KeyboardButtonColor.SECONDARY,
    ).row()
    kb.add(Text(BTN_SONG), KeyboardButtonColor.SECONDARY).add(
        Text(BTN_CODE),
        KeyboardButtonColor.SECONDARY,
    ).row()
    kb.add(Text(BTN_EXPLAIN), KeyboardButtonColor.POSITIVE).add(
        Text(BTN_OTHER),
        KeyboardButtonColor.SECONDARY,
    ).row()
    kb.add(Text(BTN_STOP), KeyboardButtonColor.NEGATIVE)
    return kb.get_json()


def refinement_done_keyboard_json() -> str:
    return (
        Keyboard(one_time=False, inline=True)
        .add(Text(BTN_DONE), KeyboardButtonColor.POSITIVE)
        .get_json()
    )


def empty_inline_keyboard_json() -> str:
    return '{"one_time":false,"inline":true,"buttons":[]}'


def _message_id_from_send_item(item: Any) -> int | None:
    """Один элемент ответа messages.send: dict API, int или модель vkbottle (MessagesSendUserIdsResponseItem)."""
    if item is None:
        return None
    if isinstance(item, int):
        return item
    if isinstance(item, dict):
        mid = item.get("message_id")
        return int(mid) if mid is not None else None
    mid_obj = getattr(item, "message_id", None)
    if mid_obj is not None:
        return int(mid_obj)
    if hasattr(item, "model_dump"):
        try:
            dumped = item.model_dump()
            if isinstance(dumped, dict):
                mid = dumped.get("message_id")
                return int(mid) if mid is not None else None
        except Exception:
            pass
    return None


def _message_id_from_messages_send_response(raw: Any) -> int | None:
    """Из ответа messages.send достаётся message_id первого отправленного сообщения."""
    if raw is None:
        return None
    if isinstance(raw, int):
        return raw
    if isinstance(raw, list) and raw:
        return _message_id_from_send_item(raw[0])
    if isinstance(raw, dict):
        r = raw.get("response", raw)
        if isinstance(r, list) and r:
            return _message_id_from_send_item(r[0])
        if isinstance(r, int):
            return r
    resp = getattr(raw, "response", None)
    if resp is not None and resp is not raw:
        return _message_id_from_messages_send_response({"response": resp})
    if hasattr(raw, "model_dump"):
        try:
            return _message_id_from_messages_send_response(raw.model_dump())
        except Exception:
            return None
    return None


def keyboard_json_for_vk(kind: str) -> str:
    if kind == VK_KB_REFINEMENT_DONE:
        return refinement_done_keyboard_json()
    if kind == VK_KB_JSON_NO_MENU:
        return empty_inline_keyboard_json()
    return branch_menu_keyboard_json()


@dataclass
class PeerState:
    session_active: bool = False
    refinement_pending: Stage3RefinementContext | None = None
    awaiting_branch1_prompt: bool = False
    awaiting_explain_prompt: bool = False
    last_vk_user_id: int | None = None
    timeout_task: asyncio.Task[None] | None = field(default=None, repr=False)


def _load_env() -> None:
    load_dotenv(_ROOT / ".env")
    load_dotenv(_ROOT / ".env.local", override=True)
    load_dotenv(Path(__file__).resolve().parent / ".env")
    load_dotenv(Path(__file__).resolve().parent / ".env.local", override=True)


def main() -> None:
    if not _VK_CONSOLE.is_dir():
        print(
            f"Не найден каталог движка: {_VK_CONSOLE}. "
            "Ожидается vk_console_bot рядом с vk_prompt_bot.",
            file=sys.stderr,
        )
        sys.exit(1)

    _load_env()
    if (os.getenv("VK_BOT_DEBUG") or "").strip().lower() not in ("1", "true", "yes"):
        logging.getLogger("vkbottle").setLevel(logging.WARNING)
    token = (os.getenv("VK_GROUP_TOKEN") or os.getenv("vk_group_token") or "").strip()
    if not token:
        print(
            "Задайте vk_group_token или VK_GROUP_TOKEN в .env (см. vk_prompt_bot/.env.example).",
            file=sys.stderr,
        )
        sys.exit(1)

    bot = Bot(token=token)
    states: dict[int, PeerState] = {}
    group_id_cache: int | None = None
    db_path = (os.getenv("VK_BOT_DB_PATH") or "").strip() or None
    db = PromptBotDatabase(db_path)

    async def get_community_group_id() -> int:
        nonlocal group_id_cache
        if group_id_cache is not None:
            return group_id_cache
        data = await bot.api.request("groups.getById", {})
        group_id_cache = int(data["response"][0]["id"])
        return group_id_cache

    async def send_from_community(peer_id: int, text: str, keyboard: str) -> None:
        gid = await get_community_group_id()
        await bot.api.messages.send(
            peer_ids=[peer_id],
            message=text,
            keyboard=keyboard,
            random_id=_vk_random_id(),
            group_id=gid,
        )

    async def reply_from_community(message: Message, text: str, keyboard: str) -> None:
        await message.answer(
            text,
            keyboard=keyboard,
            random_id=_vk_random_id(),
        )

    async def send_thinking_placeholder(peer_id: int) -> int | None:
        """Временное сообщение на время синхронного пайплайна OpenAI; возвращает message_id для удаления."""
        gid = await get_community_group_id()
        try:
            raw = await bot.api.messages.send(
                peer_ids=[peer_id],
                message=MSG_THINKING_PROMPT,
                keyboard=empty_inline_keyboard_json(),
                random_id=_vk_random_id(),
                group_id=gid,
            )
        except Exception as exc:
            print(f"thinking message send failed peer={peer_id}: {exc}", file=sys.stderr)
            return None
        mid = _message_id_from_messages_send_response(raw)
        if mid is None:
            print(f"thinking message: could not parse message_id, raw={raw!r}", file=sys.stderr)
        return mid

    async def delete_community_message(peer_id: int, message_id: int) -> None:
        gid = await get_community_group_id()
        try:
            await bot.api.messages.delete(
                message_ids=str(message_id),
                delete_for_all=1,
                peer_id=peer_id,
                group_id=gid,
            )
        except Exception as exc:
            print(f"messages.delete failed peer={peer_id} mid={message_id}: {exc}", file=sys.stderr)

    def state_for(peer_id: int) -> PeerState:
        if peer_id not in states:
            states[peer_id] = PeerState()
        return states[peer_id]

    def cancel_idle_timer(st: PeerState) -> None:
        if st.timeout_task is not None and not st.timeout_task.done():
            st.timeout_task.cancel()
        st.timeout_task = None

    async def idle_timeout_worker(peer_id: int) -> None:
        try:
            await asyncio.sleep(IDLE_SECONDS)
        except asyncio.CancelledError:
            return
        st = states.get(peer_id)
        if st is None or not st.session_active:
            return
        log_uid = int(st.last_vk_user_id or peer_id)
        st.session_active = False
        st.refinement_pending = None
        st.awaiting_branch1_prompt = False
        st.awaiting_explain_prompt = False
        st.timeout_task = None
        await _audit(
            db,
            vk_user_id=log_uid,
            peer_id=peer_id,
            kind=EventKind.IDLE_TIMEOUT,
            summary="Сессия: таймаут неактивности",
            payload={"idle_seconds": IDLE_SECONDS},
        )
        try:
            await send_from_community(
                peer_id,
                MSG_IDLE_TIMEOUT,
                start_only_keyboard_json(),
            )
        except Exception as exc:
            print(f"idle timeout send failed peer={peer_id}: {exc}", file=sys.stderr)

    def arm_idle_timer(peer_id: int, st: PeerState) -> None:
        cancel_idle_timer(st)
        st.timeout_task = asyncio.create_task(idle_timeout_worker(peer_id))

    async def send_bot(
        message: Message,
        st: PeerState,
        text: str,
        *,
        in_session: bool,
        apply_branch_stub_prefix: bool = True,
    ) -> None:
        if in_session:
            if apply_branch_stub_prefix:
                text = text_with_branch_stub_note(text)
            kb = branch_menu_keyboard_json()
        else:
            kb = start_only_keyboard_json()
        await reply_from_community(message, text, kb)

    @bot.on.message()
    async def handle(message: Message) -> None:
        peer_id = message.peer_id
        st = state_for(peer_id)
        uid = _actor_id(message)
        await asyncio.to_thread(db.ensure_user, uid, peer_id)
        st.last_vk_user_id = uid

        raw = message.text or ""
        cmd = raw.strip().lower()
        stripped = raw.strip()

        if cmd in START_CMDS:
            cancel_idle_timer(st)
            st.session_active = True
            st.refinement_pending = None
            st.awaiting_branch1_prompt = False
            st.awaiting_explain_prompt = False
            await _audit(
                db,
                vk_user_id=uid,
                peer_id=peer_id,
                kind=EventKind.SESSION_START,
                summary="Сессия: старт",
                payload={"action": "start", "label": stripped or cmd},
            )
            await send_bot(
                message,
                st,
                format_welcome_vk_menu_message(),
                in_session=True,
                apply_branch_stub_prefix=False,
            )
            arm_idle_timer(peer_id, st)
            return

        if not st.session_active:
            return

        if cmd in EXIT_CMDS or stripped == BTN_STOP:
            cancel_idle_timer(st)
            st.session_active = False
            st.refinement_pending = None
            st.awaiting_branch1_prompt = False
            st.awaiting_explain_prompt = False
            label = "Стоп" if stripped == BTN_STOP else stripped or cmd
            await _audit(
                db,
                vk_user_id=uid,
                peer_id=peer_id,
                kind=EventKind.SESSION_END,
                summary=f"Сессия: выход ({label})",
                payload={"action": "exit", "label": label},
            )
            await send_bot(message, st, MSG_EXIT, in_session=False)
            return

        if cmd in CONTINUE_CMDS:
            arm_idle_timer(peer_id, st)
            await _audit(
                db,
                vk_user_id=uid,
                peer_id=peer_id,
                kind=EventKind.BUTTON,
                summary="Кнопка/команда: продолжить",
                payload={"label": stripped},
            )
            await reply_from_community(
                message,
                text_with_branch_stub_note(MSG_CONTINUE),
                branch_menu_keyboard_json(),
            )
            return

        arm_idle_timer(peer_id, st)

        if not stripped:
            await _audit(
                db,
                vk_user_id=uid,
                peer_id=peer_id,
                kind=EventKind.USER_TEXT,
                summary="Пустое сообщение",
                payload={"text": ""},
            )
            await send_bot(message, st, MSG_NEED_TEXT, in_session=True)
            return

        branch_kb = branch_menu_keyboard_json()

        if stripped in STUB_SCENARIO_BUTTONS:
            st.awaiting_branch1_prompt = False
            st.awaiting_explain_prompt = False
            st.refinement_pending = None
            await _audit(
                db,
                vk_user_id=uid,
                peer_id=peer_id,
                kind=EventKind.BUTTON,
                summary=f"Кнопка сценария: {stripped}",
                payload={"label": stripped},
            )
            await reply_from_community(
                message,
                text_with_branch_stub_note(MSG_SCENARIO_IN_DEVELOPMENT),
                branch_kb,
            )
            return

        if stripped == BTN_WRITE_TEXT:
            st.refinement_pending = None
            st.awaiting_branch1_prompt = True
            st.awaiting_explain_prompt = False
            await _audit(
                db,
                vk_user_id=uid,
                peer_id=peer_id,
                kind=EventKind.BUTTON,
                summary="Кнопка: написать текст",
                payload={"label": stripped},
            )
            await reply_from_community(
                message,
                MSG_AWAIT_TEXT_AFTER_BUTTON,
                empty_inline_keyboard_json(),
            )
            return

        if stripped == BTN_EXPLAIN:
            st.awaiting_branch1_prompt = False
            st.awaiting_explain_prompt = True
            st.refinement_pending = None
            await _audit(
                db,
                vk_user_id=uid,
                peer_id=peer_id,
                kind=EventKind.BUTTON,
                summary="Кнопка: объяснить",
                payload={"label": stripped},
            )
            await reply_from_community(
                message,
                MSG_AWAIT_EXPLAIN_AFTER_BUTTON,
                empty_inline_keyboard_json(),
            )
            return

        if st.awaiting_branch1_prompt:
            if stripped.lower() in REFINEMENT_DONE_CMDS:
                st.awaiting_branch1_prompt = False
                await _audit(
                    db,
                    vk_user_id=uid,
                    peer_id=peer_id,
                    kind=EventKind.BUTTON,
                    summary=f"Отмена ожидания текста: {stripped}",
                    payload={"label": stripped},
                )
                await reply_from_community(
                    message,
                    text_with_branch_stub_note(
                        "Ожидание описания отменено. Выберите «написать текст» или опишите задачу.",
                    ),
                    branch_kb,
                )
                return
            st.awaiting_branch1_prompt = False
            outgoing: list[tuple[str, str]] = []

            def emit_sync(chunk: str, kb: str = VK_KB_BRANCH_MENU) -> None:
                outgoing.append((chunk, kb))

            def run_dispatch() -> Stage3RefinementContext | None:
                return vk_dispatch_sync(
                    raw,
                    emit_sync,
                    st.refinement_pending,
                    force_branch_1=True,
                )

            thinking_mid = await send_thinking_placeholder(peer_id)
            try:
                new_pending = await asyncio.to_thread(run_dispatch)
            except Exception as exc:
                if thinking_mid is not None:
                    await delete_community_message(peer_id, thinking_mid)
                await _audit(
                    db,
                    vk_user_id=uid,
                    peer_id=peer_id,
                    kind=EventKind.ERROR,
                    summary=f"Ошибка пайплайна: {_snippet(str(exc), 200)}",
                    payload={"error": str(exc), "context": "branch1_write_text"},
                )
                await reply_from_community(
                    message,
                    text_with_branch_stub_note(f"Внутренняя ошибка: {exc}"),
                    branch_kb,
                )
                return

            if thinking_mid is not None:
                await delete_community_message(peer_id, thinking_mid)

            st.refinement_pending = new_pending
            await _log_user_ai_turn(
                db,
                uid,
                peer_id,
                raw,
                outgoing,
                context="branch1_write_text",
                bill_llm=True,
            )
            for part, kb_kind in outgoing:
                if part:
                    if kb_kind == VK_KB_BRANCH_MENU_WELCOME:
                        out_text = part
                    elif kb_kind == VK_KB_BRANCH_MENU:
                        out_text = text_with_branch_stub_note(part)
                    else:
                        out_text = part
                    await reply_from_community(
                        message,
                        out_text,
                        keyboard_json_for_vk(kb_kind),
                    )
            return

        if st.awaiting_explain_prompt:
            if stripped.lower() in REFINEMENT_DONE_CMDS:
                st.awaiting_explain_prompt = False
                await _audit(
                    db,
                    vk_user_id=uid,
                    peer_id=peer_id,
                    kind=EventKind.BUTTON,
                    summary=f"Отмена ожидания explain: {stripped}",
                    payload={"label": stripped},
                )
                await reply_from_community(
                    message,
                    text_with_branch_stub_note(
                        "Ожидание объяснения отменено. Выберите «объяснить» или опишите задачу."
                    ),
                    branch_kb,
                )
                return
            st.awaiting_explain_prompt = False
            outgoing: list[tuple[str, str]] = []

            def emit_sync_explain(chunk: str, kb: str = VK_KB_BRANCH_MENU) -> None:
                outgoing.append((chunk, kb))

            def run_dispatch_explain() -> Stage3RefinementContext | None:
                return vk_dispatch_sync(
                    raw,
                    emit_sync_explain,
                    st.refinement_pending,
                    force_branch_6=True,
                )

            thinking_mid = await send_thinking_placeholder(peer_id)
            try:
                new_pending = await asyncio.to_thread(run_dispatch_explain)
            except Exception as exc:
                if thinking_mid is not None:
                    await delete_community_message(peer_id, thinking_mid)
                await _audit(
                    db,
                    vk_user_id=uid,
                    peer_id=peer_id,
                    kind=EventKind.ERROR,
                    summary=f"Ошибка explain-пайплайна: {_snippet(str(exc), 200)}",
                    payload={"error": str(exc), "context": "branch6_explain"},
                )
                await reply_from_community(
                    message,
                    text_with_branch_stub_note(f"Внутренняя ошибка: {exc}"),
                    branch_kb,
                )
                return

            if thinking_mid is not None:
                await delete_community_message(peer_id, thinking_mid)

            st.refinement_pending = new_pending
            await _log_user_ai_turn(
                db,
                uid,
                peer_id,
                raw,
                outgoing,
                context="branch6_explain",
                bill_llm=True,
            )
            for part, kb_kind in outgoing:
                if part:
                    if kb_kind == VK_KB_BRANCH_MENU_WELCOME:
                        out_text = part
                    elif kb_kind == VK_KB_BRANCH_MENU:
                        out_text = text_with_branch_stub_note(part)
                    else:
                        out_text = part
                    await reply_from_community(
                        message,
                        out_text,
                        keyboard_json_for_vk(kb_kind),
                    )
            return

        outgoing2: list[tuple[str, str]] = []

        def emit_sync2(chunk: str, kb: str = VK_KB_BRANCH_MENU) -> None:
            outgoing2.append((chunk, kb))

        def run_dispatch2() -> Stage3RefinementContext | None:
            return vk_dispatch_sync(raw, emit_sync2, st.refinement_pending)

        # Без вызова LLM: «Готово», пустой ввод при ожидании уточнения — не показывать «Я думаю…»
        ref_before = st.refinement_pending
        skip_thinking = ref_before is not None and (
            not stripped or stripped.lower() in REFINEMENT_DONE_CMDS
        )
        calls_llm_heuristic = (not skip_thinking) or (
            ref_before is not None
            and bool(stripped)
            and stripped.lower() not in REFINEMENT_DONE_CMDS
        )
        may_bill_llm = calls_llm_heuristic and not (
            ref_before is None and _vk_dispatch_is_llm_free(stripped)
        )
        thinking_mid = (
            None
            if skip_thinking
            else await send_thinking_placeholder(peer_id)
        )
        try:
            new_pending = await asyncio.to_thread(run_dispatch2)
        except Exception as exc:
            if thinking_mid is not None:
                await delete_community_message(peer_id, thinking_mid)
            await _audit(
                db,
                vk_user_id=uid,
                peer_id=peer_id,
                kind=EventKind.ERROR,
                summary=f"Ошибка пайплайна: {_snippet(str(exc), 200)}",
                payload={"error": str(exc), "context": "main_dispatch"},
            )
            await reply_from_community(
                message,
                text_with_branch_stub_note(f"Внутренняя ошибка: {exc}"),
                branch_kb,
            )
            return

        if thinking_mid is not None:
            await delete_community_message(peer_id, thinking_mid)

        st.refinement_pending = new_pending
        await _log_user_ai_turn(
            db,
            uid,
            peer_id,
            raw,
            outgoing2,
            context="main_dispatch",
            bill_llm=may_bill_llm,
        )
        for part, kb_kind in outgoing2:
            if part:
                if kb_kind == VK_KB_BRANCH_MENU_WELCOME:
                    out_text = part
                elif kb_kind == VK_KB_BRANCH_MENU:
                    out_text = text_with_branch_stub_note(part)
                else:
                    out_text = part
                await reply_from_community(
                    message,
                    out_text,
                    keyboard_json_for_vk(kb_kind),
                )

    bot.run_forever()


if __name__ == "__main__":
    main()
