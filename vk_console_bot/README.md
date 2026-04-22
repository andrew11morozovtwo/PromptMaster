# VK Console Bot — логика PromptMaster

Модуль с промптами для OpenAI-совместимого API и консольным REPL. Та же логика подключается из **VK-бота** (`vk_echo_bot/vk_prompt_bot.py`).

## Зависимости

```powershell
pip install openai python-dotenv
```

(Или общий `requirements.txt` из корня репозитория.)

## Настройка `.env`

В `vk_console_bot/` или в корне репозитория:

| Переменная | Описание |
|------------|----------|
| `OPENAI_API_KEY` | Обязательно |
| `OPENAI_BASE_URL` | По умолчанию в коде часто proxy API |
| `OPENAI_MODEL` | По умолчанию: `gpt-4o-mini` |

## Запуск консоли

```powershell
cd путь\к\PromptMaster\vk_console_bot
python main.py
```

Либо из **корня** репозитория: `python main.py` (обёртка переключает рабочий каталог на `vk_console_bot`).

После старта: приветствие → ввод `Вы: `.

## Команды в консоли

- `/menu`, `/start`, `меню` — сброс и приветствие  
- `/help` — справка  
- `/weather`, `/joke` — заглушки  
- `/exit`, `/quit` — выход  

Свободный текст — сценарий пайплайна (см. ниже).

---

## Консоль классификатора и write_text (`task_type_classifier.py`)

Отдельная терминальная утилита для ветки **`write_text`**: три этапа подряд без VK и без `instructions.txt`.

| Этап | Что делает |
|------|------------|
| **1** | Классификация запроса → `task_type` (JSON). Сохраняется в `task_type.json`. |
| **2** | Если `task_type == write_text`: извлечение спеки через пакет `write_text_analyzer`, интерактивное заполнение пустых полей (`topic`, `goal`, …), сохранение в **`write_text_spec.json`**. Поля **`examples`** и **`no_no`** при пустоте не спрашиваются. |
| **3** | Мета‑промпт для браузерного ИИ: один вызов API по спеке → блок «СКОПИРУЙТЕ ЭТОТ ПРОМПТ В БРАУЗЕРНЫЙ ИИ». Логика в корне репозитория: **`promptGenerator/write_text_prompt_maker.py`**. |

Запуск:

```powershell
cd путь\к\PromptMaster\vk_console_bot
pip install openai python-dotenv
python task_type_classifier.py
```

Тот же **`OPENAI_API_KEY`** и при необходимости **`OPENAI_BASE_URL`**, **`OPENAI_MODEL`** — в `.env` здесь или в корне репозитория.

Выход из консоли: `quit`, `exit`, `выход`.

### Только третий этап по уже сохранённой спеке

Из корня репозитория:

```powershell
cd путь\к\PromptMaster
python promptGenerator/write_text_prompt_maker.py
```

По умолчанию читается `vk_console_bot/write_text_spec.json`; можно передать путь к JSON первым аргументом.

### Артефакты и `.gitignore`

Файлы **`task_type.json`** и **`write_text_spec.json`** — локальные результаты прогона; в репозитории они игнорируются (см. корневой `.gitignore`), коммитить их не нужно.

### Связанные каталоги в корне репозитория

| Каталог | Назначение |
|---------|------------|
| `write_text_analyzer/` | Промпт и код извлечения полей спеки текста (`analyze_write_text_request`), нормализация `tone` / `length` и т.д. |
| `promptGenerator/` | Системный мета‑промпт инженера промптов и скрипт вывода «чистого» промпта в терминал. |

---

## Пайплайн (ветка 1 — «написать текст»)

1. **Этап 1** — `system_prompt` в `instructions.txt`: классификатор, JSON (`detected_branch` 1–7, `user_request`, …).  
2. **Этап 2** — `TEXT_EXTRACTION`: в user передаётся `user_request`; ответ — JSON (`original_text`, `purpose`, `type`, …).  
3. **Этап 3** — `PROMPT_IMPROVER`: два JSON в user (реплика сессии + разбор этапа 2); ответ — `old_prompt`, `new_prompt`, `advantages`.

Ветки **2–7** сейчас дают единый ответ «возврат к теме» (`OFF_TOPIC_REDIRECT`), без отдельных сценариев.

### Уточнение промпта (консоль)

После первого JSON этапа 3 задаётся вопрос про уточнение; ввод в `Уточнение: ` уходит в повторный этап 3 (`last_improver_response` + `comment_user`). **Пустой ввод** завершает цикл. Оффтоп-эвристика (`is_off_topic_user_input`) прерывает уточнение с текстом возврата к теме.

Параметры `handle_message(..., stage3_emit=...)` и `run_prompt_pipeline(..., refinement_reader=...)` позволяют подставить другой транспорт вместо `print` / `input`.

### VK-слой (без консоли)

Функция **`vk_dispatch_sync(text, emit, pending, ...)`** — синхронная обработка одного сообщения: `emit(message, keyboard_kind)` для многошаговых ответов. Используются константы клавиатур (`VK_KB_BRANCH_MENU`, `VK_KB_JSON_NO_MENU`, `VK_KB_REFINEMENT_DONE`, `VK_KB_BRANCH_MENU_WELCOME`), принудительная ветка 1 — **`force_branch_1`** (обход классификатора). Текст приветствия для VK с подписью про серые кнопки: **`format_welcome_vk_menu_message()`**.

## Файлы

| Файл | Назначение |
|------|------------|
| `main.py` | Клиент OpenAI, пайплайн, `handle_message`, консольный цикл, `vk_dispatch_sync`, эвристики оффтопа |
| `task_type_classifier.py` | Консоль: классификация → `write_text_spec` → мета‑промпт для браузерного ИИ (ветка `write_text`) |
| `instruction_loader.py` | Разбор `ИМЯ = """..."""` из `instructions.txt` |
| `instructions.txt` | `TEXT_EXTRACTION`, `system_prompt`, `PROMPT_IMPROVER` |

Глобальный **`SESSION`** в коде зарезервирован под будущее состояние; история переписки в нём не ведётся.
