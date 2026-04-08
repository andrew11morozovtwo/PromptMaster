"""
Запуск консольного бота из корня репозитория.

Реализация лежит в vk_console_bot/; при старте каталог процесса переключается туда,
чтобы находились instructions.txt и локальный .env.
"""

from __future__ import annotations

import os
import sys


def main() -> int:
    root = os.path.dirname(os.path.abspath(__file__))
    bot_dir = os.path.join(root, "vk_console_bot")
    entry = os.path.join(bot_dir, "main.py")
    if not os.path.isfile(entry):
        print(
            "Ошибка: не найден vk_console_bot/main.py. Запустите из каталога проекта PromptMaster.",
            file=sys.stderr,
        )
        return 2
    sys.path.insert(0, bot_dir)
    os.chdir(bot_dir)
    import main as bot_main  # noqa: E402

    return bot_main.main()


if __name__ == "__main__":
    raise SystemExit(main())
