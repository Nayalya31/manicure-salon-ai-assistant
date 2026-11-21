from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Iterator


SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations (
  id TEXT PRIMARY KEY,
  state_json TEXT NOT NULL DEFAULT '{}',
  awaiting_confirmation INTEGER NOT NULL DEFAULT 0,
  submitted_lead_id TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  conversation_id TEXT NOT NULL,
  role TEXT NOT NULL CHECK(role IN ('user','assistant')),
  content TEXT NOT NULL,
  created_at TEXT NOT NULL,
  FOREIGN KEY(conversation_id) REFERENCES conversations(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS ix_messages_conversation ON messages(conversation_id, id);
CREATE TABLE IF NOT EXISTS leads (
  id TEXT PRIMARY KEY,
  conversation_id TEXT NOT NULL UNIQUE,
  idempotency_key TEXT NOT NULL UNIQUE,
  payload_json TEXT NOT NULL,
  notification_status TEXT NOT NULL DEFAULT 'pending',
  notification_attempts INTEGER NOT NULL DEFAULT 0,
  notification_last_error TEXT,
  notification_next_attempt_at TEXT,
  telegram_message_id TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY(conversation_id) REFERENCES conversations(id)
);
CREATE INDEX IF NOT EXISTS ix_leads_notification ON leads(notification_status, notification_next_attempt_at);
CREATE TABLE IF NOT EXISTS rate_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  bucket TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_rate_events_bucket ON rate_events(bucket, created_at);
"""


def iso_now() -> str:
    return datetime.now(UTC).isoformat()


class Storage:
    def __init__(self, path: str) -> None:
        self.path = path
        self._init_lock = threading.Lock()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=15, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=15000")
        try:
            yield conn
        finally:
            conn.close()

    def init(self) -> None:
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self._init_lock, self.connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.executescript(SCHEMA)

    def create_conversation(self) -> str:
        conversation_id = str(uuid.uuid4())
        now = iso_now()
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO conversations(id, created_at, updated_at) VALUES(?,?,?)",
                (conversation_id, now, now),
            )
        return conversation_id

    def get_conversation(self, conversation_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM conversations WHERE id=?", (conversation_id,)).fetchone()
        if not row:
            return None
        data = dict(row)
        data["state"] = json.loads(data.pop("state_json"))
        data["awaiting_confirmation"] = bool(data["awaiting_confirmation"])
        return data

    def save_turn(
        self,
        conversation_id: str,
        user_message: str,
        assistant_message: str,
        state: dict[str, Any],
        awaiting_confirmation: bool,
    ) -> None:
        now = iso_now()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "INSERT INTO messages(conversation_id, role, content, created_at) VALUES(?,?,?,?)",
                (conversation_id, "user", user_message, now),
            )
            conn.execute(
                "INSERT INTO messages(conversation_id, role, content, created_at) VALUES(?,?,?,?)",
                (conversation_id, "assistant", assistant_message, now),
            )
            conn.execute(
                "UPDATE conversations SET state_json=?, awaiting_confirmation=?, updated_at=? WHERE id=?",
                (json.dumps(state, ensure_ascii=False), int(awaiting_confirmation), now, conversation_id),
            )
            conn.execute("COMMIT")

    def history(self, conversation_id: str, limit: int) -> list[dict[str, str]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT role, content FROM messages WHERE conversation_id=? ORDER BY id DESC LIMIT ?",
                (conversation_id, limit),
            ).fetchall()
        return [dict(row) for row in reversed(rows)]

    def submit_lead(self, conversation_id: str, idempotency_key: str) -> tuple[dict[str, Any], bool]:
        now = iso_now()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conversation = conn.execute(
                "SELECT * FROM conversations WHERE id=?", (conversation_id,)
            ).fetchone()
            if not conversation:
                conn.execute("ROLLBACK")
                raise ValueError("conversation_not_found")
            existing = conn.execute(
                "SELECT * FROM leads WHERE conversation_id=? OR idempotency_key=?",
                (conversation_id, idempotency_key),
            ).fetchone()
            if existing:
                conn.execute("COMMIT")
                return self._lead_dict(existing), False
            lead_id = str(uuid.uuid4())
            payload = conversation["state_json"]
            conn.execute(
                """INSERT INTO leads(
                    id, conversation_id, idempotency_key, payload_json,
                    notification_status, notification_next_attempt_at, created_at, updated_at
                ) VALUES(?,?,?,?,?,?,?,?)""",
                (lead_id, conversation_id, idempotency_key, payload, "pending", now, now, now),
            )
            conn.execute(
                "UPDATE conversations SET submitted_lead_id=?, updated_at=? WHERE id=?",
                (lead_id, now, conversation_id),
            )
            row = conn.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone()
            conn.execute("COMMIT")
        assert row is not None
        return self._lead_dict(row), True

    @staticmethod
    def _lead_dict(row: sqlite3.Row) -> dict[str, Any]:
        data = dict(row)
        data["payload"] = json.loads(data.pop("payload_json"))
        return data

    def claim_due_notifications(self, limit: int = 10) -> list[dict[str, Any]]:
        now = iso_now()
        stale = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """UPDATE leads SET notification_status='retry', notification_next_attempt_at=?
                WHERE notification_status='sending' AND updated_at < ?""",
                (now, stale),
            )
            rows = conn.execute(
                """SELECT * FROM leads
                WHERE notification_status IN ('pending','retry')
                  AND (notification_next_attempt_at IS NULL OR notification_next_attempt_at <= ?)
                ORDER BY created_at LIMIT ?""",
                (now, limit),
            ).fetchall()
            ids = [row["id"] for row in rows]
            if ids:
                placeholders = ",".join("?" for _ in ids)
                conn.execute(
                    f"UPDATE leads SET notification_status='sending', updated_at=? WHERE id IN ({placeholders})",
                    (now, *ids),
                )
            conn.execute("COMMIT")
        return [self._lead_dict(row) for row in rows]

    def mark_notification_sent(self, lead_id: str, telegram_message_id: str) -> None:
        with self.connect() as conn:
            conn.execute(
                """UPDATE leads SET notification_status='sent', telegram_message_id=?,
                notification_attempts=notification_attempts+1, notification_last_error=NULL,
                updated_at=? WHERE id=? AND notification_status!='sent'""",
                (telegram_message_id, iso_now(), lead_id),
            )

    def mark_notification_failed(self, lead_id: str, error: str, max_attempts: int) -> None:
        with self.connect() as conn:
            row = conn.execute("SELECT notification_attempts FROM leads WHERE id=?", (lead_id,)).fetchone()
            if not row:
                return
            attempts = int(row[0]) + 1
            status = "failed" if attempts >= max_attempts else "retry"
            next_attempt = (datetime.now(UTC) + timedelta(seconds=min(3600, 15 * 2 ** attempts))).isoformat()
            conn.execute(
                """UPDATE leads SET notification_status=?, notification_attempts=?,
                notification_last_error=?, notification_next_attempt_at=?, updated_at=? WHERE id=?""",
                (status, attempts, error[:500], next_attempt, iso_now(), lead_id),
            )

    def list_leads(self, limit: int = 200) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM leads ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        return [self._lead_dict(row) for row in rows]

    def allow_rate(self, bucket: str, limit: int) -> bool:
        now = datetime.now(UTC)
        cutoff = (now - timedelta(minutes=1)).isoformat()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("DELETE FROM rate_events WHERE created_at < ?", (cutoff,))
            count = conn.execute(
                "SELECT COUNT(*) FROM rate_events WHERE bucket=? AND created_at>=?", (bucket, cutoff)
            ).fetchone()[0]
            if count >= limit:
                conn.execute("COMMIT")
                return False
            conn.execute("INSERT INTO rate_events(bucket, created_at) VALUES(?,?)", (bucket, now.isoformat()))
            conn.execute("COMMIT")
        return True
