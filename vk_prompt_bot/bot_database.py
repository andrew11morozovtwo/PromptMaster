"""
SQLite-хранилище: значимые события бота, учёт платных единиц, поля под подписку и оплату.

Путь к файлу: переменная окружения VK_BOT_DB_PATH (по умолчанию ./data/prompt_bot.sqlite рядом с vk_bot.py).
"""

from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

SCHEMA_VERSION = 1


class EventKind:
    """Типы строк в bot_events.kind (произвольная строка тоже допустима)."""

    SESSION_START = "session_start"
    SESSION_END = "session_end"
    BUTTON = "button"
    USER_TEXT = "user_text"
    AI_TEXT = "ai_text"
    COMMAND = "command"
    ERROR = "error"
    IDLE_TIMEOUT = "idle_timeout"

_DEFAULT_DB_REL = Path(__file__).resolve().parent / "data" / "prompt_bot.sqlite"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _json_dumps_limited(obj: Any, max_chars: int = 48_000) -> str:
    s = json.dumps(obj, ensure_ascii=False, default=str)
    if len(s) <= max_chars:
        return s
    return s[: max_chars - 20] + '…"[truncated]"'


def default_db_path() -> str:
    _DEFAULT_DB_REL.parent.mkdir(parents=True, exist_ok=True)
    return str(_DEFAULT_DB_REL)


@dataclass(frozen=True)
class SubscriptionState:
    """Снимок строки users для проверки доступа (подписка / квота)."""

    vk_user_id: int
    subscription_tier: str
    subscription_status: str
    subscription_valid_until: str | None
    prepaid_units_remaining: int | None
    payment_provider: str | None
    payment_external_id: str | None


class PromptBotDatabase:
    """Потокобезопасные записи: короткое соединение на операцию + блокировка."""

    def __init__(self, db_path: str | None = None) -> None:
        self._path = db_path or default_db_path()
        Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path, timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _init_schema(self) -> None:
        with self._lock:
            conn = self._connect()
            try:
                conn.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS schema_meta (
                        key TEXT PRIMARY KEY,
                        value TEXT NOT NULL
                    );

                    CREATE TABLE IF NOT EXISTS users (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        vk_user_id INTEGER NOT NULL UNIQUE,
                        peer_id INTEGER,
                        display_name TEXT,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        subscription_tier TEXT NOT NULL DEFAULT 'free',
                        subscription_status TEXT NOT NULL DEFAULT 'active',
                        subscription_valid_until TEXT,
                        prepaid_units_remaining INTEGER,
                        payment_provider TEXT,
                        payment_external_id TEXT,
                        last_payment_at TEXT,
                        last_payment_note TEXT,
                        admin_notes TEXT
                    );

                    CREATE INDEX IF NOT EXISTS idx_users_peer ON users(peer_id);

                    CREATE TABLE IF NOT EXISTS bot_events (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        created_at TEXT NOT NULL,
                        vk_user_id INTEGER NOT NULL,
                        peer_id INTEGER NOT NULL,
                        kind TEXT NOT NULL,
                        summary TEXT NOT NULL,
                        payload_json TEXT,
                        FOREIGN KEY (vk_user_id) REFERENCES users(vk_user_id)
                    );

                    CREATE INDEX IF NOT EXISTS idx_bot_events_user_time
                        ON bot_events(vk_user_id, created_at DESC);
                    CREATE INDEX IF NOT EXISTS idx_bot_events_kind ON bot_events(kind);

                    CREATE TABLE IF NOT EXISTS paid_request_units (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        created_at TEXT NOT NULL,
                        vk_user_id INTEGER NOT NULL,
                        peer_id INTEGER NOT NULL,
                        unit_type TEXT NOT NULL,
                        bot_event_id INTEGER,
                        settled INTEGER NOT NULL DEFAULT 1,
                        metadata_json TEXT,
                        FOREIGN KEY (vk_user_id) REFERENCES users(vk_user_id),
                        FOREIGN KEY (bot_event_id) REFERENCES bot_events(id)
                    );

                    CREATE INDEX IF NOT EXISTS idx_paid_units_user_time
                        ON paid_request_units(vk_user_id, created_at DESC);

                    CREATE TABLE IF NOT EXISTS subscription_ledger (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        created_at TEXT NOT NULL,
                        vk_user_id INTEGER NOT NULL,
                        provider TEXT,
                        external_id TEXT,
                        amount_minor INTEGER,
                        currency TEXT,
                        status TEXT NOT NULL DEFAULT 'pending',
                        plan_code TEXT,
                        valid_until TEXT,
                        raw_json TEXT,
                        FOREIGN KEY (vk_user_id) REFERENCES users(vk_user_id)
                    );

                    CREATE INDEX IF NOT EXISTS idx_ledger_user ON subscription_ledger(vk_user_id, created_at DESC);
                    CREATE UNIQUE INDEX IF NOT EXISTS idx_ledger_external
                        ON subscription_ledger(provider, external_id)
                        WHERE external_id IS NOT NULL AND provider IS NOT NULL;
                    """
                )
                row = conn.execute(
                    "SELECT value FROM schema_meta WHERE key = 'version'"
                ).fetchone()
                if row is None:
                    conn.execute(
                        "INSERT INTO schema_meta(key, value) VALUES ('version', ?)",
                        (str(SCHEMA_VERSION),),
                    )
                conn.commit()
            finally:
                conn.close()

    def ensure_user(self, vk_user_id: int, peer_id: int) -> None:
        now = _utc_now_iso()
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    """
                    INSERT INTO users (vk_user_id, peer_id, created_at, updated_at)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(vk_user_id) DO UPDATE SET
                        peer_id = excluded.peer_id,
                        updated_at = excluded.updated_at
                    """,
                    (vk_user_id, peer_id, now, now),
                )
                conn.commit()
            finally:
                conn.close()

    def log_event(
        self,
        *,
        vk_user_id: int,
        peer_id: int,
        kind: str,
        summary: str,
        payload: Mapping[str, Any] | None = None,
    ) -> int | None:
        """Возвращает id строки bot_events (для связи с paid_request_units)."""
        now = _utc_now_iso()
        pj = _json_dumps_limited(dict(payload)) if payload else None
        with self._lock:
            conn = self._connect()
            try:
                cur = conn.execute(
                    """
                    INSERT INTO bot_events (created_at, vk_user_id, peer_id, kind, summary, payload_json)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (now, vk_user_id, peer_id, kind, summary, pj),
                )
                conn.commit()
                return int(cur.lastrowid) if cur.lastrowid is not None else None
            finally:
                conn.close()

    def record_paid_unit(
        self,
        *,
        vk_user_id: int,
        peer_id: int,
        unit_type: str,
        bot_event_id: int | None = None,
        settled: bool = True,
        metadata: Mapping[str, Any] | None = None,
    ) -> None:
        now = _utc_now_iso()
        meta = _json_dumps_limited(dict(metadata)) if metadata else None
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    """
                    INSERT INTO paid_request_units (
                        created_at, vk_user_id, peer_id, unit_type, bot_event_id, settled, metadata_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        now,
                        vk_user_id,
                        peer_id,
                        unit_type,
                        bot_event_id,
                        1 if settled else 0,
                        meta,
                    ),
                )
                conn.commit()
            finally:
                conn.close()

    def get_subscription_state(self, vk_user_id: int) -> SubscriptionState | None:
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    """
                    SELECT vk_user_id, subscription_tier, subscription_status,
                           subscription_valid_until, prepaid_units_remaining,
                           payment_provider, payment_external_id
                    FROM users WHERE vk_user_id = ?
                    """,
                    (vk_user_id,),
                ).fetchone()
            finally:
                conn.close()
        if row is None:
            return None
        return SubscriptionState(
            vk_user_id=int(row["vk_user_id"]),
            subscription_tier=str(row["subscription_tier"]),
            subscription_status=str(row["subscription_status"]),
            subscription_valid_until=row["subscription_valid_until"],
            prepaid_units_remaining=row["prepaid_units_remaining"],
            payment_provider=row["payment_provider"],
            payment_external_id=row["payment_external_id"],
        )

    def apply_subscription_update(
        self,
        vk_user_id: int,
        *,
        tier: str | None = None,
        status: str | None = None,
        valid_until: str | None = None,
        prepaid_units_remaining: int | None = None,
        payment_provider: str | None = None,
        payment_external_id: str | None = None,
        last_payment_at: str | None = None,
        last_payment_note: str | None = None,
    ) -> None:
        """Обновление подписки (вебхук оплаты / админка). Частичное: передавайте только нужные поля."""
        fields: list[str] = []
        values: list[Any] = []
        if tier is not None:
            fields.append("subscription_tier = ?")
            values.append(tier)
        if status is not None:
            fields.append("subscription_status = ?")
            values.append(status)
        if valid_until is not None:
            fields.append("subscription_valid_until = ?")
            values.append(valid_until)
        if prepaid_units_remaining is not None:
            fields.append("prepaid_units_remaining = ?")
            values.append(prepaid_units_remaining)
        if payment_provider is not None:
            fields.append("payment_provider = ?")
            values.append(payment_provider)
        if payment_external_id is not None:
            fields.append("payment_external_id = ?")
            values.append(payment_external_id)
        if last_payment_at is not None:
            fields.append("last_payment_at = ?")
            values.append(last_payment_at)
        if last_payment_note is not None:
            fields.append("last_payment_note = ?")
            values.append(last_payment_note)
        if not fields:
            return
        fields.append("updated_at = ?")
        values.append(_utc_now_iso())
        values.append(vk_user_id)
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    f"UPDATE users SET {', '.join(fields)} WHERE vk_user_id = ?",
                    values,
                )
                conn.commit()
            finally:
                conn.close()

    def append_ledger_entry(
        self,
        vk_user_id: int,
        *,
        provider: str | None,
        external_id: str | None,
        status: str,
        amount_minor: int | None = None,
        currency: str | None = None,
        plan_code: str | None = None,
        valid_until: str | None = None,
        raw: Mapping[str, Any] | None = None,
    ) -> None:
        now = _utc_now_iso()
        raw_j = _json_dumps_limited(dict(raw)) if raw else None
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    """
                    INSERT INTO subscription_ledger (
                        created_at, vk_user_id, provider, external_id,
                        amount_minor, currency, status, plan_code, valid_until, raw_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        now,
                        vk_user_id,
                        provider,
                        external_id,
                        amount_minor,
                        currency,
                        status,
                        plan_code,
                        valid_until,
                        raw_j,
                    ),
                )
                conn.commit()
            except sqlite3.IntegrityError:
                conn.rollback()

    def consume_prepaid_unit(self, vk_user_id: int) -> bool:
        """Уменьшить prepaid_units_remaining на 1, если > 0. Возвращает True, если списание прошло."""
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT prepaid_units_remaining FROM users WHERE vk_user_id = ?",
                    (vk_user_id,),
                ).fetchone()
                if row is None or row["prepaid_units_remaining"] is None:
                    return False
                n = int(row["prepaid_units_remaining"])
                if n <= 0:
                    return False
                conn.execute(
                    "UPDATE users SET prepaid_units_remaining = ?, updated_at = ? WHERE vk_user_id = ?",
                    (n - 1, _utc_now_iso(), vk_user_id),
                )
                conn.commit()
                return True
            finally:
                conn.close()
