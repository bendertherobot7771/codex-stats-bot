from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any


SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS events (
    event_id TEXT PRIMARY KEY,
    event_type TEXT NOT NULL,
    task_id TEXT NOT NULL,
    received_at REAL NOT NULL,
    payload_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tasks (
    task_id TEXT PRIMARY KEY,
    account_fingerprint TEXT NOT NULL,
    user_name TEXT NOT NULL,
    machine_id TEXT NOT NULL,
    machine_name TEXT NOT NULL,
    turn_id TEXT,
    session_id TEXT,
    thread_id TEXT,
    cwd TEXT,
    started_at REAL NOT NULL,
    finished_at REAL,
    status TEXT NOT NULL,
    start_used_percent REAL,
    start_reset_at INTEGER,
    start_quota_source TEXT,
    current_used_percent REAL,
    current_reset_at INTEGER,
    end_used_percent REAL,
    end_reset_at INTEGER,
    end_quota_source TEXT,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    cached_input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    reasoning_output_tokens INTEGER NOT NULL DEFAULT 0,
    total_tokens INTEGER NOT NULL DEFAULT 0,
    first_seen_at REAL NOT NULL,
    last_seen_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_tasks_started_at ON tasks(started_at);
CREATE INDEX IF NOT EXISTS idx_tasks_account_window ON tasks(account_fingerprint, start_reset_at);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);

CREATE TABLE IF NOT EXISTS bot_chats (
    chat_id INTEGER PRIMARY KEY,
    registered_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


class StatsDatabase:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        with self._lock:
            self._connection.executescript(SCHEMA)
            self._connection.commit()

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def apply_event(self, event: dict[str, Any]) -> bool:
        event_id = _required_text(event, "event_id", 300)
        event_type = _required_text(event, "event_type", 40)
        if event_type not in {"task_started", "task_heartbeat", "task_completed"}:
            raise ValueError("Неизвестный event_type")
        task_id = _required_text(event, "task_id", 300)
        now = time.time()

        with self._lock, self._connection:
            inserted = self._connection.execute(
                "INSERT OR IGNORE INTO events(event_id,event_type,task_id,received_at,payload_json) VALUES(?,?,?,?,?)",
                (event_id, event_type, task_id, now, json.dumps(event, ensure_ascii=False, separators=(",", ":"))),
            ).rowcount
            if not inserted:
                return False
            if event_type == "task_started":
                self._start_task(event, now)
            elif event_type == "task_heartbeat":
                self._heartbeat_task(event, now)
            else:
                self._complete_task(event, now)
        return True

    def tasks_since(self, since: float) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM tasks WHERE started_at >= ? ORDER BY started_at",
                (since,),
            ).fetchall()
        return [dict(row) for row in rows]

    def active_tasks(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM tasks WHERE status='active' ORDER BY started_at"
            ).fetchall()
        return [dict(row) for row in rows]

    def recent_completed(self, limit: int = 10) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM tasks WHERE status='completed' ORDER BY finished_at DESC LIMIT ?",
                (max(1, min(limit, 100)),),
            ).fetchall()
        return [dict(row) for row in rows]

    def register_chat(self, chat_id: int) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                "INSERT OR IGNORE INTO bot_chats(chat_id,registered_at) VALUES(?,?)",
                (chat_id, time.time()),
            )

    def registered_chats(self) -> list[int]:
        with self._lock:
            rows = self._connection.execute("SELECT chat_id FROM bot_chats ORDER BY chat_id").fetchall()
        return [int(row[0]) for row in rows]

    def get_setting(self, key: str, default: str = "") -> str:
        with self._lock:
            row = self._connection.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return str(row[0]) if row else default

    def set_setting(self, key: str, value: str) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                "INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )

    def _start_task(self, event: dict[str, Any], now: float) -> None:
        quota = _quota(event.get("start_quota") or event.get("quota"))
        values = _base_values(event)
        self._connection.execute(
            """
            INSERT INTO tasks(
                task_id,account_fingerprint,user_name,machine_id,machine_name,turn_id,session_id,thread_id,cwd,
                started_at,status,start_used_percent,start_reset_at,start_quota_source,current_used_percent,
                current_reset_at,first_seen_at,last_seen_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(task_id) DO UPDATE SET
                account_fingerprint=excluded.account_fingerprint,user_name=excluded.user_name,
                machine_id=excluded.machine_id,machine_name=excluded.machine_name,last_seen_at=excluded.last_seen_at
            """,
            (*values, _number(event.get("started_at"), now), "active", *quota, quota[0], quota[1], now, now),
        )

    def _heartbeat_task(self, event: dict[str, Any], now: float) -> None:
        self._ensure_task(event, now)
        quota = _quota(event.get("quota"))
        tokens = _tokens(event.get("tokens"))
        self._connection.execute(
            """
            UPDATE tasks SET current_used_percent=?,current_reset_at=?,input_tokens=?,cached_input_tokens=?,
                output_tokens=?,reasoning_output_tokens=?,total_tokens=?,last_seen_at=? WHERE task_id=?
            """,
            (quota[0], quota[1], *tokens, now, event["task_id"]),
        )

    def _complete_task(self, event: dict[str, Any], now: float) -> None:
        self._ensure_task(event, now)
        quota = _quota(event.get("quota"))
        tokens = _tokens(event.get("tokens"))
        self._connection.execute(
            """
            UPDATE tasks SET finished_at=?,status='completed',current_used_percent=?,current_reset_at=?,
                end_used_percent=?,end_reset_at=?,end_quota_source=?,input_tokens=?,cached_input_tokens=?,
                output_tokens=?,reasoning_output_tokens=?,total_tokens=?,last_seen_at=? WHERE task_id=?
            """,
            (
                _number(event.get("finished_at"), now),
                quota[0],
                quota[1],
                *quota,
                *tokens,
                now,
                event["task_id"],
            ),
        )

    def _ensure_task(self, event: dict[str, Any], now: float) -> None:
        row = self._connection.execute("SELECT 1 FROM tasks WHERE task_id=?", (event["task_id"],)).fetchone()
        if row:
            return
        synthetic = dict(event)
        synthetic["start_quota"] = event.get("start_quota") or event.get("quota")
        self._start_task(synthetic, now)


def _base_values(event: dict[str, Any]) -> tuple[Any, ...]:
    return (
        _required_text(event, "task_id", 300),
        _required_text(event, "account_fingerprint", 100),
        _required_text(event, "user_name", 200),
        _required_text(event, "machine_id", 100),
        _required_text(event, "machine_name", 200),
        _optional_text(event.get("turn_id"), 200),
        _optional_text(event.get("session_id"), 200),
        _optional_text(event.get("thread_id"), 200),
        _optional_text(event.get("cwd"), 1_000),
    )


def _quota(value: Any) -> tuple[float | None, int | None, str | None]:
    value = value if isinstance(value, dict) else {}
    return (
        _optional_number(value.get("used_percent")),
        _optional_integer(value.get("resets_at")),
        _optional_text(value.get("source"), 100),
    )


def _tokens(value: Any) -> tuple[int, int, int, int, int]:
    value = value if isinstance(value, dict) else {}
    return tuple(
        max(0, _integer(value.get(name)))
        for name in (
            "input_tokens",
            "cached_input_tokens",
            "output_tokens",
            "reasoning_output_tokens",
            "total_tokens",
        )
    )  # type: ignore[return-value]


def _required_text(value: dict[str, Any], name: str, maximum: int) -> str:
    result = _optional_text(value.get(name), maximum)
    if not result:
        raise ValueError(f"Поле {name} обязательно")
    return result


def _optional_text(value: Any, maximum: int) -> str | None:
    if value is None:
        return None
    return str(value)[:maximum]


def _number(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _optional_number(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _integer(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _optional_integer(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None

