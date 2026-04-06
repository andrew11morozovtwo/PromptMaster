"""
Пробный бот для VK (сообщество): демо-режим с лимитом эхо.

Клавиатура (inline, под сообщениями бота):
- В сессии: ряд «Старт», ряд «Стоп» и «Продолжить» (заглушка «Приложение в разработке»).
- После выхода, лимита или таймаута — только «Старт».
- Ответ на сообщение пользователя: «Вы написали …» (не дословное эхо текста).

Клавиатура: inline=True — кнопки под сообщением бота (так их чаще видно в мобильном VK).
На части клиентов ВК (старый веб) inline может не отображаться — тогда пишите из приложения.

Отправка: из хендлера — message.answer (peer_ids), иначе messages.send с peer_ids и group_id.

Настройка VK — Long Poll, message_new, токен в .env.
"""

from __future__ import annotations

import asyncio
import os
import random
import sys
from dataclasses import dataclass, field

from dotenv import load_dotenv
from vkbottle import Keyboard, KeyboardButtonColor, Text
from vkbottle.bot import Bot, Message


START_CMDS = frozenset({"/start", "/старт", "старт"})
# «Стоп» — с кнопки (label)
EXIT_CMDS = frozenset({"/выход", "/exit", "/stop", "/стоп", "стоп"})
CONTINUE_CMDS = frozenset({"продолжить"})

IDLE_SECONDS = 180

INTRO_TEXT = (
    "я готов продемонстрировать свои способности, пришли мне сообщение и я отвечу на него."
)
MSG_EXIT = "Вы вышли из режима эхо. Нажмите зелёную кнопку «Старт», чтобы продолжить."
MSG_LIMIT = (
    "Лимит эхо-ответов исчёрпан (3 раза). Нажмите «Старт», чтобы начать новый цикл."
)
MSG_NEED_TEXT = "Нужно текстовое сообщение, чтобы я мог ответить."
MSG_IDLE_TIMEOUT = (
    "Прошло 3 минуты без сообщений — режим эхо отключён. Нажмите «Старт», чтобы снова начать."
)
MSG_CONTINUE = "Приложение в разработке"


def _vk_random_id() -> int:
    """Уникальный random_id на каждое сообщение — иначе VK может склеить ответы при random_id=0."""
    return random.randint(1, 2**31 - 1)


def start_only_keyboard_json() -> str:
    """Только зелёная «Старт» (inline — под последним сообщением бота)."""
    return (
        Keyboard(one_time=False, inline=True)
        .add(Text("Старт"), KeyboardButtonColor.POSITIVE)
        .get_json()
    )


def session_keyboard_json() -> str:
    """В сессии: ряд «Старт», ряд «Стоп» | «Продолжить» (inline)."""
    return (
        Keyboard(one_time=False, inline=True)
        .add(Text("Старт"), KeyboardButtonColor.POSITIVE)
        .row()
        .add(Text("Стоп"), KeyboardButtonColor.NEGATIVE)
        .add(Text("Продолжить"), KeyboardButtonColor.SECONDARY)
        .get_json()
    )


@dataclass
class PeerState:
    echo_mode: bool = False
    echoes_left: int = 0
    timeout_task: asyncio.Task[None] | None = field(default=None, repr=False)


def main() -> None:
    load_dotenv()
    token = (os.getenv("VK_GROUP_TOKEN") or os.getenv("vk_group_token") or "").strip()
    if not token:
        print("Задайте vk_group_token или VK_GROUP_TOKEN в .env (см. .env_test).", file=sys.stderr)
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
        """Без объекта Message (таймаут): peer_ids + group_id — как рекомендует VK для сообществ."""
        gid = await get_community_group_id()
        await bot.api.messages.send(
            peer_ids=[peer_id],
            message=text,
            keyboard=keyboard,
            random_id=_vk_random_id(),
            group_id=gid,
        )

    async def reply_from_community(message: Message, text: str, keyboard: str) -> None:
        """Ответ в том же диалоге через answer (peer_ids) — совпадает с примерами vkbottle."""
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
        if st is None or not st.echo_mode:
            return
        st.echo_mode = False
        st.echoes_left = 0
        st.timeout_task = None
        try:
            await send_from_community(peer_id, MSG_IDLE_TIMEOUT, start_only_keyboard_json())
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
    ) -> None:
        kb = session_keyboard_json() if in_session else start_only_keyboard_json()
        await reply_from_community(message, text, kb)

    @bot.on.message()
    async def handle(message: Message) -> None:
        peer_id = message.peer_id
        st = state_for(peer_id)
        raw = message.text or ""
        cmd = raw.strip().lower()

        if cmd in START_CMDS:
            cancel_idle_timer(st)
            st.echo_mode = True
            st.echoes_left = 3
            await send_bot(message, st, INTRO_TEXT, in_session=True)
            arm_idle_timer(peer_id, st)
            return

        if not st.echo_mode:
            return

        if cmd in EXIT_CMDS:
            cancel_idle_timer(st)
            st.echo_mode = False
            st.echoes_left = 0
            await send_bot(message, st, MSG_EXIT, in_session=False)
            return

        if cmd in CONTINUE_CMDS:
            arm_idle_timer(peer_id, st)
            await reply_from_community(message, MSG_CONTINUE, session_keyboard_json())
            return

        arm_idle_timer(peer_id, st)

        if not raw.strip():
            await send_bot(message, st, MSG_NEED_TEXT, in_session=True)
            return

        reply_text = f"Вы написали {raw}"
        await send_bot(message, st, reply_text, in_session=True)
        st.echoes_left -= 1
        if st.echoes_left <= 0:
            cancel_idle_timer(st)
            st.echo_mode = False
            await send_bot(message, st, MSG_LIMIT, in_session=False)

    bot.run_forever()


if __name__ == "__main__":
    main()
