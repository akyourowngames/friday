"""SQLite state shared by remote Ares channels.

Channel cursors are deliberately persisted separately from conversation text:
after a process restart, a provider update can be acknowledged exactly once
without duplicating the associated Ares turn.
"""

from __future__ import annotations

from pathlib import Path

from ares.infra.sqlite_utils import connect_sqlite
from ares.tools.dates import now_local_iso


class ChannelStore:
    """Persist provider cursors and the conversation bound to each remote chat."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = connect_sqlite(self.db_path)
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS channel_cursors (
                channel TEXT PRIMARY KEY,
                next_offset INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL
            )
            """
        )
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS channel_conversation_history (
                channel TEXT NOT NULL,
                external_chat_id TEXT NOT NULL,
                conversation_id INTEGER NOT NULL,
                last_selected_at TEXT NOT NULL,
                PRIMARY KEY (channel, external_chat_id, conversation_id)
            )
            """
        )
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS channel_conversations (
                channel TEXT NOT NULL,
                external_chat_id TEXT NOT NULL,
                conversation_id INTEGER NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (channel, external_chat_id)
            )
            """
        )
        self.conn.commit()

    def get_offset(self, channel: str) -> int:
        row = self.conn.execute(
            "SELECT next_offset FROM channel_cursors WHERE channel = ?", (channel,)
        ).fetchone()
        return int(row["next_offset"]) if row else 0

    def advance_offset(self, channel: str, next_offset: int) -> None:
        """Record the next provider offset without moving it backwards."""
        self.conn.execute(
            """
            INSERT INTO channel_cursors (channel, next_offset, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(channel) DO UPDATE SET
                next_offset = MAX(channel_cursors.next_offset, excluded.next_offset),
                updated_at = excluded.updated_at
            """,
            (channel, int(next_offset), now_local_iso()),
        )
        self.conn.commit()

    def get_conversation_id(self, channel: str, chat_id: int | str) -> int | None:
        row = self.conn.execute(
            """
            SELECT conversation_id FROM channel_conversations
            WHERE channel = ? AND external_chat_id = ?
            """,
            (channel, str(chat_id)),
        ).fetchone()
        return int(row["conversation_id"]) if row else None

    def set_conversation_id(self, channel: str, chat_id: int | str, conversation_id: int) -> None:
        now = now_local_iso()
        self.conn.execute(
            """
            INSERT INTO channel_conversations (channel, external_chat_id, conversation_id, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(channel, external_chat_id) DO UPDATE SET
                conversation_id = excluded.conversation_id,
                updated_at = excluded.updated_at
            """,
            (channel, str(chat_id), int(conversation_id), now),
        )
        self.conn.execute(
            """INSERT INTO channel_conversation_history
               (channel, external_chat_id, conversation_id, last_selected_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(channel, external_chat_id, conversation_id) DO UPDATE SET
                   last_selected_at = excluded.last_selected_at""",
            (channel, str(chat_id), int(conversation_id), now),
        )
        self.conn.commit()

    def list_conversation_ids(self, channel: str, chat_id: int | str, limit: int = 12) -> list[int]:
        rows = self.conn.execute(
            """SELECT conversation_id FROM channel_conversation_history
               WHERE channel = ? AND external_chat_id = ?
               ORDER BY last_selected_at DESC LIMIT ?""",
            (channel, str(chat_id), max(1, min(int(limit), 50))),
        ).fetchall()
        return [int(row["conversation_id"]) for row in rows]

    def close(self) -> None:
        self.conn.close()
