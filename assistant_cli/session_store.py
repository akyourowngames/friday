from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .config import AssistantSettings


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


@dataclass(frozen=True)
class DbHit:
    role: str
    content: str
    session_id: str
    at: str


class SessionStore:
    def __init__(self, settings: AssistantSettings, session_id: str | None = None) -> None:
        self.settings = settings
        self.session_id = session_id or datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
        self.jsonl_path = settings.sessions_dir / f"{self.session_id}.jsonl"
        self._init_db()
        self._start_session()

    def _connect(self) -> sqlite3.Connection:
        self.settings.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.settings.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY,
                    started_at TEXT NOT NULL,
                    ended_at TEXT,
                    jsonl_path TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS memory_facts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    bucket TEXT NOT NULL,
                    fact TEXT NOT NULL UNIQUE,
                    source_message_id INTEGER,
                    at TEXT NOT NULL
                )
                """
            )
            try:
                conn.execute(
                    """
                    CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts
                    USING fts5(content, role, session_id, message_id UNINDEXED)
                    """
                )
            except sqlite3.OperationalError:
                pass

    def _start_session(self) -> None:
        self.settings.sessions_dir.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO sessions(id, started_at, jsonl_path)
                VALUES (?, ?, ?)
                """,
                (self.session_id, _now(), str(self.jsonl_path)),
            )

    def end(self) -> None:
        with self._connect() as conn:
            conn.execute("UPDATE sessions SET ended_at = ? WHERE id = ?", (_now(), self.session_id))

    def append_message(self, role: str, content: str) -> int:
        content = str(content or "").strip()
        if not content:
            return -1
        at = _now()
        self.jsonl_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO messages(session_id, role, content, at) VALUES (?, ?, ?, ?)",
                (self.session_id, role, content, at),
            )
            message_id = int(cur.lastrowid)
            try:
                conn.execute(
                    "INSERT INTO messages_fts(content, role, session_id, message_id) VALUES (?, ?, ?, ?)",
                    (content, role, self.session_id, message_id),
                )
            except sqlite3.OperationalError:
                pass

        event = {
            "type": "message",
            "message_id": message_id,
            "session_id": self.session_id,
            "role": role,
            "content": content,
            "at": at,
        }
        with self.jsonl_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False) + "\n")
        return message_id

    def append_fact(self, bucket: str, fact: str, source_message_id: int | None = None) -> bool:
        fact = str(fact or "").strip()
        if not fact:
            return False
        at = _now()
        inserted = False
        with self._connect() as conn:
            try:
                conn.execute(
                    "INSERT INTO memory_facts(bucket, fact, source_message_id, at) VALUES (?, ?, ?, ?)",
                    (bucket, fact, source_message_id, at),
                )
                inserted = True
            except sqlite3.IntegrityError:
                inserted = False
        if inserted:
            with self.jsonl_path.open("a", encoding="utf-8") as handle:
                handle.write(
                    json.dumps(
                        {
                            "type": "memory_fact",
                            "session_id": self.session_id,
                            "bucket": bucket,
                            "fact": fact,
                            "source_message_id": source_message_id,
                            "at": at,
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )
        return inserted

    def recent_messages(self, limit: int = 20) -> list[dict[str, str]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT role, content
                FROM messages
                WHERE session_id = ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (self.session_id, max(0, int(limit))),
            ).fetchall()
        return [{"role": row["role"], "content": row["content"]} for row in reversed(rows)]

    def recent_text(self, limit: int = 20) -> str:
        return "\n".join(f"{m['role']}: {m['content']}" for m in self.recent_messages(limit))

    def search_messages(self, query: str, limit: int = 5) -> list[DbHit]:
        query = str(query or "").strip()
        if not query:
            return []
        fts_query = " ".join(part.replace('"', "") for part in query.split()[:8])
        rows = []
        with self._connect() as conn:
            try:
                rows = conn.execute(
                    """
                    SELECT m.role, m.content, m.session_id, m.at
                    FROM messages_fts f
                    JOIN messages m ON m.id = f.message_id
                    WHERE messages_fts MATCH ?
                    ORDER BY rank
                    LIMIT ?
                    """,
                    (fts_query, max(1, int(limit))),
                ).fetchall()
            except sqlite3.OperationalError:
                pattern = f"%{query}%"
                rows = conn.execute(
                    """
                    SELECT role, content, session_id, at
                    FROM messages
                    WHERE content LIKE ?
                    ORDER BY id DESC
                    LIMIT ?
                    """,
                    (pattern, max(1, int(limit))),
                ).fetchall()
        return [DbHit(row["role"], row["content"], row["session_id"], row["at"]) for row in rows]
