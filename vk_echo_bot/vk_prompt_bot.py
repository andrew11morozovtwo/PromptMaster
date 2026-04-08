"""
VK-бот PromptMaster на базе vk_echo_trial: Long Poll, inline-клавиатура, логика vk_console_bot.

После «Старт» — меню веток; после 3 мин бездействия — снова полное приветствие и только кнопка «Старт».
«написать текст» задаёт ветку 1 без классификатора;
серые кнопки — короткие ярлыки; пояснение «в разработке» в тексте над клавиатурой. Свободный текст — классификатор.

Токен: VK_GROUP_TOKEN / vk_group_token. OpenAI: OPENAI_API_KEY (корень репо или vk_echo_bot/.env).
"""

from __future__ import annotations

import asyncio
import os
import random
import sys
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv
from vkbottle import Keyboard, KeyboardButtonColor, Text
from vkbottle.bot import Bot, Message

_ROOT = Path(__file__).resolve().parent.parent
_VK_CONSOLE = _ROOT / "vk_console_bot"
if _VK_CONSOLE.is_dir():
    sys.path.insert(0, str(_VK_CONSOLE))

from main import (  # noqa: E402
    REFINEMENT_DONE_CMDS,
    VK_GREY_BUTTONS_FOOTER,
    VK_KB_BRANCH_MENU,
    VK_KB_BRANCH_MENU_WELCOME,
    VK_KB_JSON_NO_MENU,
    VK_KB_REFINEMENT_DONE,
    Stage3RefinementContext,
    format_welcome,
    format_welcome_vk_menu_message,
    vk_dispatch_sync,
)

START_CMDS = frozenset({"/start", "/старт", "старт"})
EXIT_CMDS = frozenset({"/выход", "/exit", "/stop", "/стоп", "стоп"})
CONTINUE_CMDS = frozenset({"продолжить"})

BTN_WRITE_TEXT = "написать текст"
BTN_STOP = "Стоп"

# Короткие ярлыки на кнопках; «в разработке» — в тексте сообщения над клавиатурой.
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
        BTN_EXPLAIN,
        BTN_OTHER,
    }
)

def text_with_branch_stub_note(body: str) -> str:
    """Пояснение про серые кнопки в начале + основной текст (не для полного приветствия VK)."""
    return VK_GREY_BUTTONS_FOOTER + "\n\n" + body

BTN_DONE = "Готово"

IDLE_SECONDS = 180

MSG_EXIT = (
    "Вы вышли из сессии. Нажмите «Старт», чтобы снова составить промпт для ИИ."
)
MSG_NEED_TEXT = "Нужно текстовое сообщение."
MSG_CONTINUE = (
    "Кнопка в разработке. Опишите задачу текстом или отправьте /help."
)
MSG_AWAIT_TEXT_AFTER_BUTTON = (
    "Опишите задачу одним сообщением: какой текст нужен от ИИ, для кого, объём и стиль."
)
MSG_SCENARIO_IN_DEVELOPMENT = (
    "Этот сценарий в разработке. Используйте «написать текст» или опишите задачу в свободной форме."
)


def _vk_random_id() -> int:
    return random.randint(1, 2**31 - 1)


def start_only_keyboard_json() -> str:
    return (
        Keyboard(one_time=False, inline=True)
        .add(Text("Старт"), KeyboardButtonColor.POSITIVE)
        .get_json()
    )


def branch_menu_keyboard_json() -> str:
    """Меню после Старт: короткие ярлыки на серых кнопках; пояснение — в тексте сообщения."""
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
    kb.add(Text(BTN_EXPLAIN), KeyboardButtonColor.SECONDARY).add(
        Text(BTN_OTHER),
        KeyboardButtonColor.SECONDARY,
    ).row()
    kb.add(Text(BTN_STOP), KeyboardButtonColor.NEGATIVE)
    return kb.get_json()


def refinement_done_keyboard_json() -> str:
    """Только кнопка «Готово» при вопросе про уточнение промпта."""
    return (
        Keyboard(one_time=False, inline=True)
        .add(Text(BTN_DONE), KeyboardButtonColor.POSITIVE)
        .get_json()
    )


def empty_inline_keyboard_json() -> str:
    """Снимает меню веток под сообщением (JSON с промптом без кнопок)."""
    return '{"one_time":false,"inline":true,"buttons":[]}'


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
    timeout_task: asyncio.Task[None] | None = field(default=None, repr=False)


def _load_env() -> None:
    load_dotenv(_ROOT / ".env")
    load_dotenv(_ROOT / ".env.local", override=True)
    load_dotenv(Path(__file__).resolve().parent / ".env")
    load_dotenv(Path(__file__).resolve().parent / ".env.local", override=True)


def main() -> None:
    _load_env()
    token = (os.getenv("VK_GROUP_TOKEN") or os.getenv("vk_group_token") or "").strip()
    if not token:
        print(
            "Задайте vk_group_token или VK_GROUP_TOKEN в .env (см. vk_echo_bot/.env_test).",
            file=sys.stderr,
        )
        sys.exit(1)

    bot = Bot(token=token)
    states: dict[int, PeerState] = {}
    group_id_cache: int | None = None

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
        st.session_active = False
        st.refinement_pending = None
        st.awaiting_branch1_prompt = False
        st.timeout_task = None
        try:
            await send_from_community(
                peer_id,
                format_welcome(),
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
        raw = message.text or ""
        cmd = raw.strip().lower()
        stripped = raw.strip()

        if cmd in START_CMDS:
            cancel_idle_timer(st)
            st.session_active = True
            st.refinement_pending = None
            st.awaiting_branch1_prompt = False
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
            await send_bot(message, st, MSG_EXIT, in_session=False)
            return

        if cmd in CONTINUE_CMDS:
            arm_idle_timer(peer_id, st)
            await reply_from_community(
                message,
                text_with_branch_stub_note(MSG_CONTINUE),
                branch_menu_keyboard_json(),
            )
            return

        arm_idle_timer(peer_id, st)

        if not stripped:
            await send_bot(message, st, MSG_NEED_TEXT, in_session=True)
            return

        branch_kb = branch_menu_keyboard_json()

        if stripped in STUB_SCENARIO_BUTTONS:
            st.awaiting_branch1_prompt = False
            st.refinement_pending = None
            await reply_from_community(
                message,
                text_with_branch_stub_note(MSG_SCENARIO_IN_DEVELOPMENT),
                branch_kb,
            )
            return

        if stripped == BTN_WRITE_TEXT:
            st.refinement_pending = None
            st.awaiting_branch1_prompt = True
            await reply_from_community(
                message,
                text_with_branch_stub_note(MSG_AWAIT_TEXT_AFTER_BUTTON),
                branch_kb,
            )
            return

        if st.awaiting_branch1_prompt:
            if stripped.lower() in REFINEMENT_DONE_CMDS:
                st.awaiting_branch1_prompt = False
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

            try:
                new_pending = await asyncio.to_thread(run_dispatch)
            except Exception as exc:
                await reply_from_community(
                    message,
                    text_with_branch_stub_note(f"Внутренняя ошибка: {exc}"),
                    branch_kb,
                )
                return

            st.refinement_pending = new_pending
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

        try:
            new_pending = await asyncio.to_thread(run_dispatch2)
        except Exception as exc:
            await reply_from_community(
                message,
                text_with_branch_stub_note(f"Внутренняя ошибка: {exc}"),
                branch_kb,
            )
            return

        st.refinement_pending = new_pending
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
