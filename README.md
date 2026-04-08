# PromptMaster

Репозиторий с инструментами для работы с промптами и ИИ: отдельный **консольный прототип бота** (цепочка «классификатор → разбор текста → улучшение промпта»), **VK-бот** на Long Poll и ранний **терминальный агент** (`agent.py`).

## Компоненты

| Каталог / файл | Назначение |
|------------------|------------|
| `vk_console_bot/` | Логика пайплайна, `instructions.txt`, консольный REPL. Подробнее: [vk_console_bot/README.md](vk_console_bot/README.md). |
| `vk_echo_bot/` | Бот ВКонтакте: `vk_prompt_bot.py` (PromptMaster), `vk_echo_trial.py` (демо-эхо). Подробнее: [vk_echo_bot/README.md](vk_echo_bot/README.md). |
| `main.py` (корень) | Запуск консольного бота из корня репозитория (переход в `vk_console_bot`). |
| `agent.py`, `build_prompt.py`, … | Прочие скрипты проекта. |

## Быстрый старт (Windows PowerShell)

```powershell
cd C:\zero_code\PromptMaster
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

- **Консольный PromptMaster** (из корня): `python main.py`  
- **Консоль** (из каталога): `cd vk_console_bot` → `python main.py`  
- **VK**: см. [vk_echo_bot/README.md](vk_echo_bot/README.md).

## Переменные окружения

- **OpenAI-совместимый API:** `OPENAI_API_KEY`, при необходимости `OPENAI_BASE_URL`, `OPENAI_MODEL` (см. примеры в `vk_console_bot/.env`, корневой `.env`).
- **VK:** `VK_GROUP_TOKEN` или `vk_group_token` — для `vk_echo_bot`.

## Персистентность и Docker

Состояние VK-сессии и уточнений сейчас хранится **в памяти процесса**; после перезапуска контейнера оно теряется. Вынос в БД (PostgreSQL / SQLite на volume) запланирован отдельно.

## Старый сценарий: Terminal AI Agent

Интерактивный агент через `agent.py` (потоковый режим и т.д.):

```powershell
python .\agent.py --stream
```

Подробнее о переменных см. корневой `env copy.example` (шаблон) и свой `.env` (не коммитится), если используете `agent.py`.
