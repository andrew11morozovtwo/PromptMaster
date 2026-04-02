# Technical Specification — PromptMaster (MVP)

Дата: 2026-04-02  
Версия: 1.0 (MVP)  
Цель: дипломный проект — разработка Telegram-бота для генерации промптов LLM.

## 1. Обзор продукта

`PromptMaster` — сервис, который по входным данным пользователя собирает “идеальный” промпт для LLM.

MVP включает:
- `Telegram-бот` (production-версия) с мастер-сценарием (в MVP: последовательность “3 вопроса”);
- `AI module` (генерация финального промпта через GPT 4o mini);
- `Payments module` (freemium: разблокировка премиума через ЮKassa inline payments);
- `DB module` (пользователи, промпты, платежи).

Дополнительно из ТЗ: CLI-прототип (`Prompt Generator Core`) используется для 95% reuse кода между CLI и Bot.

## 2. Архитектурный подход

Для MVP используется **монолитный подход** (один деплой/процесс), но с внутренней модульностью по ответственности:
- `Telegram bot` — обработка апдейтов и сценариев;
- `AI module` — построение финального промпта и вызов LLM;
- `Payments module` — создание платежей и обработка статусов;
- `DB module` — доступ к данным через репозитории/DAO.

## 3. Функциональные требования

### 3.1 Telegram-бот (MVP)

Основной сценарий:
1. Пользователь запускает бота: `/start`
2. Бот предлагает пройти “мастер промптов” (MVP: 3 вопроса).
3. После ответов:
   - если доступ free: выполняется генерация и отправляется готовый результат;
   - если требуется premium: инициируется платеж, после успешной оплаты — выполняется генерация.

Поддерживаемые команды/действия (минимум для MVP):
- `/start` — старт и регистрация пользователя (если требуется).
- `/help` — краткая инструкция по возможностям.

Поддержка расширений:
- планы MVP-расширений под `image/audio/video` (в следующих релизах).

### 3.2 AI module (PromptBuilder / prompt engine)

Роль: генерация финального промпта на основе входных данных пользователя.

Требования к логике:
- формирование системной роли (system prompt) и правил формата итогового ответа;
- сбор контекста и уточнений из “мастера” (3 вопроса);
- генерация промпта через LLM: **GPT-4o mini**;
- (опционально в MVP) self-refine loop: ограниченное число повторных прогонов для повышения качества.

Интерфейс (логический):
- `generate_prompt(user_input, user_profile, options) -> PromptResult`

`PromptResult` включает:
- `prompt_text` (финальный промпт);
- `meta` (опционально: модель, версия схемы промпта, шаги refine).

### 3.3 Payments module (ЮKassa)

Freemium-модель:
- часть функций доступна бесплатно;
- premium открывает генерацию “полного” результата или расширенные сценарии.

Требования:
- создание payment по запросу пользователя (inline payments);
- обработка статуса оплаты:
  - webhook/колбэк ЮKassa (выбранный режим зависит от деплоя);
  - обновление статуса в БД;
  - разблокировка premium-доступа.

Сущности:
- связывание `telegram_user_id` и `payment_id`/`yookassa_payment_id`;
- хранение статуса (`created`, `pending`, `succeeded`, `canceled`, `failed`, и т.п.).

Логические интерфейсы:
- `create_payment(user_id, amount, product) -> PaymentLink/PaymentId`
- `handle_payment_event(event_payload) -> PaymentUpdateResult`

### 3.4 DB module

Хранилища (минимально для MVP):
- `users`
- `prompts`
- `payments`

Обязательные операции:
- регистрация пользователя по `telegram_user_id`;
- создание записи промпта после генерации;
- создание и обновление платежей при событиях ЮKassa;
- проверка права доступа (`free/premium`) при каждом сценарном переходе.

## 4. Схема БД (логическая)

Ниже поля соответствуют схеме из `ARCHITECTURE.md` и ERD в `docs/promptmaster_er.drawio`.

### 4.1 `users`
- `telegram_user_id` (PK)
- `name/locale`
- `access_level` (например: `free`, `premium`)
- `created_at`
- `updated_at`

### 4.2 `prompts`
- `prompt_id` (PK)
- `telegram_user_id` (FK -> `users.telegram_user_id`)
- `input_payload`
- `prompt_text`
- `model`
- `created_at`

### 4.3 `payments`
- `payment_id` (PK)
- `telegram_user_id` (FK -> `users.telegram_user_id`)
- `yookassa_payment_id`
- `amount`
- `status`
- `created_at`
- `updated_at`

## 5. Нефункциональные требования

### 5.1 Производительность
- целевое время ответа для MVP:
  - генерация промпта — зависит от LLM и сети;
  - остальная логика (БД, маршрутизация, проверки доступа) — должна быть асинхронной.

### 5.2 Надежность
- транзакционность при переключении доступа `premium` после успешного платежа;
- идемпотентность обработчика событий ЮKassa (минимизация повторов).

### 5.3 Безопасность
- хранение секретов (токены Telegram/OpenAI/YooKassa) через env/per-env manager;
- валидировать входные данные webhook’ов и защищать endpoint’ы.

## 6. Технический стек (по ТЗ)

- Backend language/runtime: `Python 3.12`
- Telegram framework: `aiogram 3.x` + `asyncio`
- LLM: `OpenAI API` (GPT-4o mini)
- Speech (из ТЗ): `yandex-speechkit` (актуально для будущих сценариев голос/аудио)
- DB: `SQLite/PostgreSQL`
- Cache/limits: `Redis` (опционально для MVP)
- Payments: `ЮKassa inline`
- Tests: `pytest` (ориентир: `80% coverage` на core функций)
- Deploy: `Docker` / `Docker Compose`, `nginx`, VPS
- Monitoring: `Sentry` (в ТЗ отмечено TODO)

## 7. Потоки (sequence-level)

### 7.1 Бесплатная генерация (free)
1. `Telegram bot` принимает сообщение/состояние “мастер 3 вопроса”.
2. Проверяет доступ через `DB module`.
3. `AI module` строит финальный промпт и вызывает LLM.
4. Результат сохраняется в `DB module` (таблица `prompts`).
5. `Telegram bot` отправляет пользователю готовый промпт.

### 7.2 Генерация после оплаты (premium)
1. `Telegram bot` понимает, что доступ premium нужен.
2. `Payments module` создаёт inline payment в ЮKassa и фиксирует запись в `payments`.
3. Пользователь оплачивает.
4. `Payments module` получает webhook/колбэк и обновляет `payments.status`.
5. `DB module` переводит пользователя в `access_level=premium`.
6. После подтверждения пользователю разрешается генерация (либо инициируется повторный сценарный шаг).

## 8. Развертывание (MVP)

- Сервис: один процесс приложения (монолит).
- Контейнеризация: `Docker Compose`.
- Публичный доступ: `nginx` (терминация TLS, проксирование).
- Переменные окружения: токены и настройки интеграций.

## 9. План тестирования

### 9.1 Unit tests
- `AI module`:
  - сборка системного промпта;
  - корректность формата результата;
  - нормализация/валидация `PromptResult`.
- `DB module`:
  - репозитории (создание/чтение/статусы).
- `Payments module`:
  - преобразование payload’ов и обновление статусов (без реального API — через моки).

### 9.2 Integration / E2E
- сценарий “голос→платёж” (из ТЗ) — как расширение, когда появится соответствующая ветка UI;
- сценарий “оплата→unlock→генерация” (валидировать связку Telegram ↔ Payments ↔ DB ↔ AI).

## 10. Приемка (критерии из ТЗ)

- `CLI + Bot` live;
- `95% success rate`;
- `Code review passed`;
- Revenue demo: тестовая оплата.

