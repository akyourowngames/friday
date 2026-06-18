"""Task management system: create, list, complete, and delete tasks."""

import sqlite3
from datetime import datetime
from pathlib import Path

from ares.config import get_db_path
from ares.dates import now_local_iso, parse_user_datetime
from ares.sqlite_utils import connect_sqlite


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


class TaskStore:
    """Manages the task database: CRUD operations for tasks."""

    def __init__(self, db_path: Path | None = None):
        self.db_path = db_path or get_db_path()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = connect_sqlite(self.db_path)
        self._init_db()

    def _init_db(self):
        """Initialize tasks table if it doesn't exist."""
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS tasks (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                title        TEXT NOT NULL,
                description  TEXT,
                due          TEXT,
                priority     TEXT DEFAULT 'medium',
                status       TEXT DEFAULT 'pending',
                created_at   TEXT DEFAULT (datetime('now')),
                updated_at   TEXT DEFAULT (datetime('now')),
                completed_at TEXT,
                reminder_at  TEXT,
                reminder_sent_at TEXT,
                original_due_text TEXT
            )
        """)
        _ensure_column(self.conn, "tasks", "updated_at", "TEXT")
        _ensure_column(self.conn, "tasks", "reminder_sent_at", "TEXT")
        _ensure_column(self.conn, "tasks", "original_due_text", "TEXT")
        self.conn.commit()

    def create(
        self,
        title: str,
        description: str | None = None,
        due: str | None = None,
        priority: str = "medium",
        reminder_at: str | None = None,
    ) -> int:
        """Create a new task. Returns the task id."""
        normalized_due = parse_user_datetime(due)
        normalized_reminder = parse_user_datetime(reminder_at) if reminder_at else normalized_due
        cursor = self.conn.execute(
            """INSERT INTO tasks (title, description, due, priority, reminder_at, original_due_text, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                title,
                description,
                normalized_due,
                priority,
                normalized_reminder,
                due if due != normalized_due else None,
                now_local_iso(),
            ),
        )
        self.conn.commit()
        return cursor.lastrowid

    def get(self, task_id: int) -> dict | None:
        """Get a task by ID."""
        row = self.conn.execute(
            "SELECT * FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        return dict(row) if row else None

    def list_pending(self, limit: int = 50) -> list[dict]:
        """List all pending tasks, sorted by due date then priority."""
        priority_order = "CASE priority WHEN 'high' THEN 1 WHEN 'medium' THEN 2 WHEN 'low' THEN 3 END"
        rows = self.conn.execute(
            f"""SELECT * FROM tasks WHERE status = 'pending'
                ORDER BY due IS NULL, due ASC, {priority_order} LIMIT ?""",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    def list_all(self, include_done: bool = False) -> list[dict]:
        """List all tasks."""
        if include_done:
            rows = self.conn.execute(
                "SELECT * FROM tasks ORDER BY created_at DESC"
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM tasks WHERE status != 'cancelled' ORDER BY created_at DESC"
            ).fetchall()
        return [dict(r) for r in rows]

    def import_tasks(self, tasks: list[dict]) -> int:
        """Import tasks, skipping exact title/due/status duplicates."""
        existing = {
            (t["title"], t.get("due"), t.get("status"))
            for t in self.list_all(include_done=True)
        }
        imported = 0
        for task in tasks:
            title = task.get("title")
            if not title:
                continue
            due = task.get("due")
            status = task.get("status", "pending")
            key = (title, due, status)
            if key in existing:
                continue
            cursor = self.conn.execute(
                """INSERT INTO tasks (
                       title, description, due, priority, status, created_at,
                       updated_at, completed_at, reminder_at, reminder_sent_at,
                       original_due_text
                   )
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    title,
                    task.get("description"),
                    due,
                    task.get("priority", "medium"),
                    status,
                    task.get("created_at") or now_local_iso(),
                    task.get("updated_at") or now_local_iso(),
                    task.get("completed_at"),
                    task.get("reminder_at"),
                    task.get("reminder_sent_at"),
                    task.get("original_due_text"),
                ),
            )
            if cursor.rowcount:
                imported += 1
                existing.add(key)
        self.conn.commit()
        return imported

    def complete(self, task_id: int) -> bool:
        """Mark a task as done. Returns True if successful."""
        now = now_local_iso()
        cursor = self.conn.execute(
            """UPDATE tasks
               SET status = 'done', completed_at = ?, updated_at = ?
               WHERE id = ? AND status = 'pending'""",
            (now, now, task_id),
        )
        self.conn.commit()
        return cursor.rowcount > 0

    def cancel(self, task_id: int) -> bool:
        """Cancel a task. Returns True if successful."""
        cursor = self.conn.execute(
            "UPDATE tasks SET status = 'cancelled', updated_at = ? WHERE id = ?",
            (now_local_iso(), task_id),
        )
        self.conn.commit()
        return cursor.rowcount > 0

    def delete(self, task_id: int) -> bool:
        """Delete a task permanently. Returns True if deleted."""
        cursor = self.conn.execute(
            "DELETE FROM tasks WHERE id = ?", (task_id,)
        )
        self.conn.commit()
        return cursor.rowcount > 0

    def get_due_soon(self, hours: int = 24) -> list[dict]:
        """Get tasks due within the next N hours."""
        from datetime import timedelta

        now = datetime.now().astimezone()
        soon = now + timedelta(hours=hours)
        rows = self.conn.execute(
            """SELECT * FROM tasks
               WHERE status = 'pending' AND due IS NOT NULL AND due <= ? AND due >= ?""",
            (soon.isoformat(timespec="seconds"), now.isoformat(timespec="seconds")),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_due_reminders(self, now: str | None = None, limit: int = 20) -> list[dict]:
        """Return pending reminder notifications that should fire now."""
        now_value = now or now_local_iso()
        rows = self.conn.execute(
            """SELECT * FROM tasks
               WHERE status = 'pending'
                 AND reminder_at IS NOT NULL
                 AND reminder_sent_at IS NULL
                 AND reminder_at <= ?
               ORDER BY reminder_at ASC
               LIMIT ?""",
            (now_value, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def mark_reminded(self, task_id: int, reminded_at: str | None = None) -> bool:
        """Mark a task reminder as sent."""
        timestamp = reminded_at or now_local_iso()
        cursor = self.conn.execute(
            "UPDATE tasks SET reminder_sent_at = ?, updated_at = ? WHERE id = ?",
            (timestamp, timestamp, task_id),
        )
        self.conn.commit()
        return cursor.rowcount > 0

    def search(self, query: str, limit: int = 10, include_done: bool = False) -> list[dict]:
        """Search tasks by title or description."""
        like = f"%{query}%"
        status_clause = "" if include_done else "AND status = 'pending'"
        rows = self.conn.execute(
            f"""SELECT * FROM tasks
                WHERE (title LIKE ? OR description LIKE ?) {status_clause}
                ORDER BY due IS NULL, due ASC, created_at DESC
                LIMIT ?""",
            (like, like, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def close(self):
        """Close the database connection."""
        self.conn.close()
