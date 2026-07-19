"""Persistent conversation storage and compact session summaries."""

from __future__ import annotations

import sqlite3
import re
from datetime import datetime, timezone
from pathlib import Path

from ares.config import get_db_path
from ares.tools.dates import now_local_iso
from ares.sqlite_utils import connect_sqlite


class ConversationStore:
    """Stores chat sessions, messages, and compact summaries in SQLite."""

    def __init__(
        self,
        db_path: Path | None = None,
        *,
        connection: sqlite3.Connection | None = None,
    ):
        self.db_path = Path(db_path) if db_path is not None else get_db_path()
        if connection is None:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            self._owns_connection = True
            self.conn = connect_sqlite(self.db_path)
        else:
            self._owns_connection = False
            self.conn = connection
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
                tool_calls      TEXT,
                FOREIGN KEY(conversation_id) REFERENCES conversations(id)
            )
        """)
        self.conn.commit()
        self._ensure_tool_calls_column()
        self._init_recall_index()

    def _init_recall_index(self) -> None:
        """Create a local full-text index for durable conversation recall.

        The index deliberately stays inside the same local SQLite database as
        the conversation rows.  Rebuilding at startup also indexes sessions
        written by older Ares versions that predate this table.
        """
        self.recall_enabled = False
        try:
            self.conn.execute(
                """
                CREATE VIRTUAL TABLE IF NOT EXISTS conversation_recall USING fts5(
                    content,
                    role UNINDEXED,
                    conversation_id UNINDEXED,
                    created_at UNINDEXED
                )
                """
            )
            self.conn.execute("DELETE FROM conversation_recall")
            self.conn.execute(
                """
                INSERT INTO conversation_recall (rowid, content, role, conversation_id, created_at)
                SELECT id, content, role, conversation_id, created_at
                FROM conversation_messages
                """
            )
            self.conn.commit()
            self.recall_enabled = True
        except sqlite3.DatabaseError:
            # SQLite builds without FTS5 still get the deterministic LIKE
            # fallback in search_recall.
            self.recall_enabled = False

    def _index_message(self, message_id: int, conversation_id: int, role: str, content: str, created_at: str) -> None:
        if not self.recall_enabled:
            return
        try:
            self.conn.execute(
                """INSERT OR REPLACE INTO conversation_recall
                   (rowid, content, role, conversation_id, created_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (message_id, content, role, str(conversation_id), created_at),
            )
        except sqlite3.DatabaseError:
            self.recall_enabled = False

    def _ensure_tool_calls_column(self) -> None:
        """Add tool_calls column if missing (migration for existing DBs)."""
        try:
            self.conn.execute("SELECT tool_calls FROM conversation_messages LIMIT 1")
        except sqlite3.OperationalError:
            self.conn.execute("ALTER TABLE conversation_messages ADD COLUMN tool_calls TEXT")
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

    def add_message(self, conversation_id: int, role: str, content: str, tool_calls: str | None = None) -> int:
        """Persist one chat message."""
        created_at = now_local_iso()
        cursor = self.conn.execute(
            """INSERT INTO conversation_messages (conversation_id, role, content, created_at, tool_calls)
               VALUES (?, ?, ?, ?, ?)""",
            (conversation_id, role, content, created_at, tool_calls),
        )
        self._index_message(cursor.lastrowid, conversation_id, role, content, created_at)
        self.conn.commit()
        return cursor.lastrowid

    def add_exchange(self, conversation_id: int, user_input: str, assistant_response: str, tool_calls: str | None = None) -> None:
        """Persist a user/assistant turn."""
        self.add_message(conversation_id, "user", user_input)
        if assistant_response.strip() or tool_calls:
            self.add_message(conversation_id, "assistant", assistant_response, tool_calls)

    def get_recent_messages(self, limit: int = 20) -> list[dict]:
        """Return recent chat messages in chronological order."""
        rows = self.conn.execute(
            """SELECT role, content FROM conversation_messages
               ORDER BY id DESC LIMIT ?""",
            (limit,),
        ).fetchall()
        return [dict(r) for r in reversed(rows)]

    def get_recent_context_messages(
        self,
        *,
        limit: int = 15,
        exclude_conversation_id: int | None = None,
        ended_only: bool = False,
    ) -> list[dict]:
        """Return bounded recent messages with provenance for context injection.

        Immediate messages from the active conversation are already supplied as
        normal chat history.  Callers can therefore exclude that conversation,
        preventing the retrieval layer from echoing the current turn back into
        the prompt.
        """
        clauses: list[str] = []
        params: list[object] = []
        if exclude_conversation_id is not None:
            clauses.append("m.conversation_id != ?")
            params.append(int(exclude_conversation_id))
        if ended_only:
            clauses.append("c.ended_at IS NOT NULL")
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        rows = self.conn.execute(
            f"""SELECT m.id, m.conversation_id, m.role, m.content, m.created_at
                FROM conversation_messages AS m
                JOIN conversations AS c ON c.id=m.conversation_id
                {where}
                ORDER BY m.created_at DESC, m.id DESC LIMIT ?""",
            [*params, max(1, min(int(limit), 100))],
        ).fetchall()
        return [dict(row) for row in reversed(rows)]

    def get_messages_for_model(
        self,
        conversation_id: int,
        limit: int = 20,
        *,
        max_content_chars: int = 50_000,
    ) -> list[dict[str, str]]:
        """Return bounded, model-safe direct history for exactly one chat.

        This intentionally excludes stored system/tool roles, database metadata,
        and legacy tool-call scaffolding.  Cross-conversation recall belongs in
        the bounded recall/context layer and must never be presented as ordinary
        preceding user/assistant messages in a newly created conversation.
        """
        bounded = max(0, min(int(limit), 200))
        if bounded == 0:
            return []
        content_limit = max(1, min(int(max_content_chars), 200_000))
        rows = self.conn.execute(
            """SELECT role, content FROM conversation_messages
               WHERE conversation_id = ? AND role IN ('user', 'assistant')
               ORDER BY id DESC LIMIT ?""",
            (int(conversation_id), bounded),
        ).fetchall()
        messages: list[dict[str, str]] = []
        for row in reversed(rows):
            content = str(row["content"] or "").replace("\x00", "")[:content_limit]
            messages.append({"role": str(row["role"]), "content": content})
        return messages

    def get_messages(self, conversation_id: int) -> list[dict]:
        """Return all messages for one conversation."""
        rows = self.conn.execute(
            """SELECT * FROM conversation_messages
               WHERE conversation_id = ?
               ORDER BY id ASC""",
            (conversation_id,),
        ).fetchall()
        result = []
        for row in rows:
            msg = dict(row)
            if msg.get("tool_calls"):
                import json
                try:
                    msg["tool_calls"] = json.loads(msg["tool_calls"])
                except (json.JSONDecodeError, TypeError):
                    msg["tool_calls"] = None
            result.append(msg)
        return result

    def list_conversations(self) -> list[dict]:
        """Return stored conversations newest first."""
        rows = self.conn.execute(
            "SELECT * FROM conversations ORDER BY id DESC"
        ).fetchall()
        return [dict(r) for r in rows]

    def list_resumable_conversations(self, limit: int = 50) -> list[dict]:
        """Return only conversations that can be restored into chat context.

        A CLI startup creates an empty active row before the user sends a
        message. Listing that row first made the resume picker appear to
        restore a conversation while actually selecting the brand-new blank
        chat. Joining against the direct chat messages also hides abandoned
        empty rows left behind by older launches.
        """
        bounded = max(1, min(int(limit), 200))
        rows = self.conn.execute(
            """
            SELECT c.*, COUNT(m.id) AS message_count
            FROM conversations AS c
            JOIN conversation_messages AS m ON m.conversation_id = c.id
            WHERE m.role IN ('user', 'assistant')
            GROUP BY c.id
            HAVING COUNT(m.id) > 0
            ORDER BY c.id DESC
            LIMIT ?
            """,
            (bounded,),
        ).fetchall()
        return [dict(row) for row in rows]

    def get_recent_summaries(self, limit: int = 5) -> list[str]:
        """Return recent non-empty session summaries."""
        rows = self.conn.execute(
            """SELECT summary FROM conversations
               WHERE summary IS NOT NULL AND summary != ''
               ORDER BY id DESC LIMIT ?""",
            (limit,),
        ).fetchall()
        return [r["summary"] for r in reversed(rows)]

    @staticmethod
    def _since_timestamp(since: str | None) -> str | None:
        if not since or not str(since).strip():
            return None
        text = str(since).strip()
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            try:
                import dateparser

                parsed = dateparser.parse(
                    text,
                    settings={
                        "PREFER_DATES_FROM": "past",
                        "RELATIVE_BASE": datetime.now().astimezone(),
                        "RETURN_AS_TIMEZONE_AWARE": True,
                    },
                )
            except Exception:
                parsed = None
        if parsed is None:
            raise ValueError(f"Could not parse 'since' value: {since}")
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=datetime.now().astimezone().tzinfo)
        return parsed.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")

    @staticmethod
    def _fts_query(query: str) -> str:
        tokens = re.findall(r"[\w]+", query, flags=re.UNICODE)
        return " OR ".join(f'"{token.replace(chr(34), chr(34) * 2)}"' for token in tokens)

    def search_recall(
        self,
        query: str = "",
        *,
        since: str | None = None,
        limit: int = 6,
        exclude_conversation_id: int | None = None,
    ) -> list[dict]:
        """Return bounded local conversation evidence for explicit recall requests.

        This API intentionally returns raw local records.  The local recall
        context preserves stored values and adds stable provenance IDs.
        """
        bounded = max(1, min(int(limit), 30))
        query_text = str(query or "").strip()[:300]
        clauses: list[str] = []
        params: list[object] = []
        since_timestamp = self._since_timestamp(since)
        if since_timestamp:
            clauses.append("m.created_at >= ?")
            params.append(since_timestamp)
        if exclude_conversation_id is not None:
            clauses.append("m.conversation_id != ?")
            params.append(int(exclude_conversation_id))
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""

        rows: list[sqlite3.Row] = []
        fts_query = self._fts_query(query_text)
        if query_text and self.recall_enabled and fts_query:
            try:
                fts_clauses = ["conversation_recall MATCH ?", *clauses]
                rows = self.conn.execute(
                    f"""
                    SELECT m.* FROM conversation_recall
                    JOIN conversation_messages AS m ON m.id = conversation_recall.rowid
                    WHERE {' AND '.join(fts_clauses)}
                    ORDER BY conversation_recall.rank, m.created_at DESC, m.id DESC
                    LIMIT ?
                    """,
                    [fts_query, *params, bounded],
                ).fetchall()
            except sqlite3.DatabaseError:
                rows = []
        if not rows:
            if query_text:
                like = f"%{query_text}%"
                extra = " AND " if clauses else " WHERE "
                rows = self.conn.execute(
                    f"""SELECT m.* FROM conversation_messages AS m{where}{extra}
                       m.content LIKE ? COLLATE NOCASE
                       ORDER BY m.created_at DESC, m.id DESC LIMIT ?""",
                    [*params, like, bounded],
                ).fetchall()
            else:
                rows = self.conn.execute(
                    f"SELECT m.* FROM conversation_messages AS m{where} "
                    "ORDER BY m.created_at DESC, m.id DESC LIMIT ?",
                    [*params, bounded],
                ).fetchall()
        return [dict(row) for row in rows]

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

        self.save_structured_summary(conversation_id, summary)
        return summary

    def save_structured_summary(self, conversation_id: int, summary: str) -> None:
        """Save a structured LLM-generated summary."""
        self.conn.execute(
            """UPDATE conversations
               SET summary = ?, summarized_at = ?
               WHERE id = ?""",
            (summary, now_local_iso(), conversation_id),
        )
        self.conn.commit()

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
        self._init_recall_index()
        return imported

    def list_messages(self) -> list[dict]:
        """Return every stored message for export."""
        rows = self.conn.execute(
            "SELECT * FROM conversation_messages ORDER BY id ASC"
        ).fetchall()
        return [dict(r) for r in rows]

    def delete_conversation(self, conversation_id: int) -> bool:
        """Delete a conversation and all its messages."""
        if self.recall_enabled:
            try:
                self.conn.execute("DELETE FROM conversation_recall WHERE conversation_id = ?", (str(conversation_id),))
            except sqlite3.DatabaseError:
                self.recall_enabled = False
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

    def delete_empty_conversations(self) -> int:
        """Delete conversations that have no messages. Returns count deleted."""
        cursor = self.conn.execute(
            """DELETE FROM conversations
               WHERE id NOT IN (
                   SELECT DISTINCT conversation_id FROM conversation_messages
               )"""
        )
        self.conn.commit()
        return cursor.rowcount

    def close(self) -> None:
        """Close the database connection."""
        self.conn.close()
