"""Persistent conversation storage and compact session summaries."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from ares.config import get_db_path
from ares.dates import now_local_iso
from ares.sqlite_utils import connect_sqlite


class ConversationStore:
    """Stores chat sessions, messages, and compact summaries in SQLite."""

    def __init__(self, db_path: Path | None = None):
        self.db_path = db_path or get_db_path()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = connect_sqlite(self.db_path)
        self._init_db()

    def _init_db(self) -> None:
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS conversations (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                started_at    TEXT NOT NULL,
                ended_at      TEXT,
                summary       TEXT,
                summarized_at TEXT
            )
        """)
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS conversation_messages (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                conversation_id INTEGER NOT NULL,
                role            TEXT NOT NULL,
                content         TEXT NOT NULL,
                created_at      TEXT NOT NULL,
                FOREIGN KEY(conversation_id) REFERENCES conversations(id)
            )
        """)
        self.conn.commit()

    def start_conversation(self) -> int:
        """Create and return a new conversation session ID."""
        cursor = self.conn.execute(
            "INSERT INTO conversations (started_at) VALUES (?)",
            (now_local_iso(),),
        )
        self.conn.commit()
        return cursor.lastrowid

    def end_conversation(self, conversation_id: int) -> None:
        """Mark a conversation as ended."""
        self.conn.execute(
            "UPDATE conversations SET ended_at = ? WHERE id = ?",
            (now_local_iso(), conversation_id),
        )
        self.conn.commit()

    def add_message(self, conversation_id: int, role: str, content: str) -> int:
        """Persist one chat message."""
        cursor = self.conn.execute(
            """INSERT INTO conversation_messages (conversation_id, role, content, created_at)
               VALUES (?, ?, ?, ?)""",
            (conversation_id, role, content, now_local_iso()),
        )
        self.conn.commit()
        return cursor.lastrowid

    def add_exchange(self, conversation_id: int, user_input: str, assistant_response: str) -> None:
        """Persist a user/assistant turn."""
        self.add_message(conversation_id, "user", user_input)
        if assistant_response.strip():
            self.add_message(conversation_id, "assistant", assistant_response)

    def get_recent_messages(self, limit: int = 20) -> list[dict]:
        """Return recent chat messages in chronological order."""
        rows = self.conn.execute(
            """SELECT role, content FROM conversation_messages
               ORDER BY id DESC LIMIT ?""",
            (limit,),
        ).fetchall()
        return [dict(r) for r in reversed(rows)]

    def get_messages(self, conversation_id: int) -> list[dict]:
        """Return all messages for one conversation."""
        rows = self.conn.execute(
            """SELECT * FROM conversation_messages
               WHERE conversation_id = ?
               ORDER BY id ASC""",
            (conversation_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def list_conversations(self) -> list[dict]:
        """Return stored conversations newest first."""
        rows = self.conn.execute(
            "SELECT * FROM conversations ORDER BY id DESC"
        ).fetchall()
        return [dict(r) for r in rows]

    def get_recent_summaries(self, limit: int = 5) -> list[str]:
        """Return recent non-empty session summaries."""
        rows = self.conn.execute(
            """SELECT summary FROM conversations
               WHERE summary IS NOT NULL AND summary != ''
               ORDER BY id DESC LIMIT ?""",
            (limit,),
        ).fetchall()
        return [r["summary"] for r in reversed(rows)]

    def summarize_conversation(self, conversation_id: int, max_chars: int = 1200) -> str | None:
        """Create a compact local summary from stored messages."""
        messages = self.get_messages(conversation_id)
        if not messages:
            return None

        user_lines = [m["content"].strip() for m in messages if m["role"] == "user"]
        assistant_lines = [m["content"].strip() for m in messages if m["role"] == "assistant"]
        parts = []
        if user_lines:
            parts.append("User topics: " + "; ".join(user_lines[:5]))
        if assistant_lines:
            parts.append("Assistant responses: " + "; ".join(assistant_lines[:3]))
        summary = " | ".join(parts)
        if len(summary) > max_chars:
            summary = summary[: max_chars - 3].rstrip() + "..."

        self.conn.execute(
            """UPDATE conversations
               SET summary = ?, summarized_at = ?
               WHERE id = ?""",
            (summary, now_local_iso(), conversation_id),
        )
        self.conn.commit()
        return summary

    def summarize_ended_without_summary(self, min_messages: int = 2) -> int:
        """Summarize ended sessions that do not already have summaries."""
        rows = self.conn.execute(
            """SELECT c.id, COUNT(m.id) AS message_count
               FROM conversations c
               JOIN conversation_messages m ON m.conversation_id = c.id
               WHERE c.ended_at IS NOT NULL
                 AND (c.summary IS NULL OR c.summary = '')
               GROUP BY c.id
               HAVING message_count >= ?""",
            (min_messages,),
        ).fetchall()
        count = 0
        for row in rows:
            if self.summarize_conversation(row["id"]):
                count += 1
        return count

    def import_conversations(self, conversations: list[dict], messages: list[dict]) -> int:
        """Import conversation rows and messages without overwriting local IDs."""
        imported = 0
        id_map: dict[int, int] = {}
        for conv in conversations:
            cursor = self.conn.execute(
                """INSERT INTO conversations (started_at, ended_at, summary, summarized_at)
                   VALUES (?, ?, ?, ?)""",
                (
                    conv.get("started_at") or now_local_iso(),
                    conv.get("ended_at"),
                    conv.get("summary"),
                    conv.get("summarized_at"),
                ),
            )
            if conv.get("id") is not None:
                id_map[int(conv["id"])] = cursor.lastrowid
            imported += 1

        for msg in messages:
            old_id = msg.get("conversation_id")
            new_id = id_map.get(int(old_id)) if old_id is not None else None
            if new_id is None:
                continue
            self.conn.execute(
                """INSERT INTO conversation_messages (conversation_id, role, content, created_at)
                   VALUES (?, ?, ?, ?)""",
                (
                    new_id,
                    msg.get("role", "user"),
                    msg.get("content", ""),
                    msg.get("created_at") or now_local_iso(),
                ),
            )
        self.conn.commit()
        return imported

    def list_messages(self) -> list[dict]:
        """Return every stored message for export."""
        rows = self.conn.execute(
            "SELECT * FROM conversation_messages ORDER BY id ASC"
        ).fetchall()
        return [dict(r) for r in rows]

    def delete_conversation(self, conversation_id: int) -> bool:
        """Delete a conversation and all its messages."""
        self.conn.execute(
            "DELETE FROM conversation_messages WHERE conversation_id = ?",
            (conversation_id,),
        )
        cursor = self.conn.execute(
            "DELETE FROM conversations WHERE id = ?",
            (conversation_id,),
        )
        self.conn.commit()
        return cursor.rowcount > 0

    def rename_conversation(self, conversation_id: int, title: str) -> bool:
        """Set a custom summary/title for a conversation."""
        cursor = self.conn.execute(
            "UPDATE conversations SET summary = ? WHERE id = ?",
            (title.strip(), conversation_id),
        )
        self.conn.commit()
        return cursor.rowcount > 0

    def close(self) -> None:
        """Close the database connection."""
        self.conn.close()
