"""
Точка входа для совместимости: основной код VK-бота в `vk_prompt_bot/vk_bot.py`.
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_VK_PROMPT_BOT = _ROOT / "vk_prompt_bot"
sys.path.insert(0, str(_VK_PROMPT_BOT))

from vk_bot import main  # noqa: E402

if __name__ == "__main__":
    main()
