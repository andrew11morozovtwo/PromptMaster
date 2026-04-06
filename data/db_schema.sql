-- PromptMaster: схема БД для PostgreSQL
-- Файл: data/db_schema.sql
-- Запуск из корня проекта:
--   psql -U postgres -h localhost -d promptmaster -f data/db_schema.sql

BEGIN;

CREATE TABLE users (
    id SERIAL PRIMARY KEY,
    telegram_id BIGINT NOT NULL UNIQUE,
    subscription_type VARCHAR(20) NOT NULL DEFAULT 'freemium',
    created_at TIMESTAMP NOT NULL DEFAULT NOW()
);

CREATE TABLE prompts (
    id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users (id),
    input_text TEXT,
    generated_prompt TEXT,
    created_at TIMESTAMP NOT NULL DEFAULT NOW()
);

CREATE TABLE payments (
    id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users (id),
    amount DECIMAL(10, 2) NOT NULL,
    status VARCHAR(20) NOT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT NOW()
);

-- Индексы: user_id во вложенных таблицах (JOIN / выборки по пользователю).
-- Для telegram_id индекс уже создаётся автоматически из ограничения UNIQUE.
CREATE INDEX idx_prompts_user_id ON prompts (user_id);
CREATE INDEX idx_payments_user_id ON payments (user_id);

COMMIT;
