"""Task management system: create, list, complete, and delete tasks."""

import sqlite3
from datetime import datetime
from pathlib import Path

from ares.config import get_db_path
from ares.tools.dates import now_local_iso, parse_user_datetime
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
        _ensure_column(self.conn, "tasks", "auto_executable", "TEXT DEFAULT 'no'")
        _ensure_column(self.conn, "tasks", "execution_notes", "TEXT")
        _ensure_column(self.conn, "tasks", "executed_at", "TEXT")
        _ensure_column(self.conn, "tasks", "max_turns", "INTEGER DEFAULT 10")
        _ensure_column(self.conn, "tasks", "retry_count", "INTEGER DEFAULT 0")
        self.conn.commit()
        self._migrate_v2()

    def _migrate_v2(self):
        """Add v2 columns for task execution engine (state, plan, steps, retry, events, artifacts)."""
        _ensure_column(self.conn, "tasks", "state", "TEXT DEFAULT 'pending'")
        _ensure_column(self.conn, "tasks", "plan", "TEXT")
        _ensure_column(self.conn, "tasks", "current_step", "INTEGER DEFAULT 0")
        _ensure_column(self.conn, "tasks", "total_steps", "INTEGER DEFAULT 0")
        _ensure_column(self.conn, "tasks", "completed_steps", "TEXT")
        _ensure_column(self.conn, "tasks", "attempt", "INTEGER DEFAULT 1")
        _ensure_column(self.conn, "tasks", "max_attempts", "INTEGER DEFAULT 3")
        _ensure_column(self.conn, "tasks", "retry_reason", "TEXT")
        _ensure_column(self.conn, "tasks", "completion_report", "TEXT")

        # Migrate existing status values to new state column
        self.conn.execute("UPDATE tasks SET state = 'completed' WHERE status = 'done' AND (state IS NULL OR state = 'pending')")
        self.conn.execute("UPDATE tasks SET state = 'failed' WHERE status = 'partial' AND (state IS NULL OR state = 'pending')")
        self.conn.execute("UPDATE tasks SET state = 'cancelled' WHERE status = 'cancelled' AND (state IS NULL OR state = 'pending')")
        self.conn.execute("UPDATE tasks SET state = 'running' WHERE status = 'in_progress' AND (state IS NULL OR state = 'pending')")

        # New tables
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS task_events (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id    INTEGER NOT NULL,
                timestamp  TEXT DEFAULT (datetime('now')),
                level      TEXT DEFAULT 'info',
                step       INTEGER,
                message    TEXT NOT NULL,
                FOREIGN KEY (task_id) REFERENCES tasks(id)
            )
        """)
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS task_artifacts (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id       INTEGER NOT NULL,
                step          INTEGER,
                path          TEXT NOT NULL,
                artifact_type TEXT NOT NULL,
                size_bytes    INTEGER DEFAULT 0,
                size_human    TEXT DEFAULT '0 B',
                line_count    INTEGER,
                description   TEXT,
                created_at    TEXT DEFAULT (datetime('now')),
                FOREIGN KEY (task_id) REFERENCES tasks(id)
            )
        """)
        self.conn.commit()

    def create(
        self,
        title: str,
        description: str | None = None,
        due: str | None = None,
        priority: str = "medium",
        reminder_at: str | None = None,
        auto_executable: str = "no",
        max_turns: int = 10,
    ) -> int:
        """Create a new task. Returns the task id."""
        normalized_due = parse_user_datetime(due)
        normalized_reminder = parse_user_datetime(reminder_at) if reminder_at else normalized_due
        cursor = self.conn.execute(
            """INSERT INTO tasks (title, description, due, priority, reminder_at,
               original_due_text, updated_at, auto_executable, max_turns)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                title,
                description,
                normalized_due,
                priority,
                normalized_reminder,
                due if due != normalized_due else None,
                now_local_iso(),
                auto_executable,
                max_turns,
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

    def update(self, task_id: int, **kwargs) -> bool:
        """Update arbitrary fields on a task. Returns True if successful."""
        allowed = {
            "title", "description", "due", "priority", "status",
            "execution_notes", "executed_at", "max_turns", "retry_count",
            "auto_executable", "reminder_at",
            # v2 fields
            "state", "plan", "current_step", "total_steps", "completed_steps",
            "attempt", "max_attempts", "retry_reason", "completion_report",
        }
        updates = {k: v for k, v in kwargs.items() if k in allowed}
        if not updates:
            return False
        updates["updated_at"] = now_local_iso()
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        values = list(updates.values()) + [task_id]
        cursor = self.conn.execute(
            f"UPDATE tasks SET {set_clause} WHERE id = ?",
            values,
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

    # ── v2: Events ─────────────────────────────────────────────

    def add_event(self, task_id: int, level: str, step: int | None, message: str) -> int:
        """Insert a task event. Returns event ID."""
        cursor = self.conn.execute(
            "INSERT INTO task_events (task_id, level, step, message) VALUES (?, ?, ?, ?)",
            (task_id, level, step, message),
        )
        self.conn.commit()
        return cursor.lastrowid

    def get_events(self, task_id: int, limit: int = 50) -> list[dict]:
        """Get events for a task, oldest first."""
        rows = self.conn.execute(
            "SELECT * FROM task_events WHERE task_id = ? ORDER BY id ASC LIMIT ?",
            (task_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    # ── v2: Artifacts ──────────────────────────────────────────

    def add_artifact(self, task_id: int, artifact: dict) -> int:
        """Insert a task artifact. Returns artifact ID."""
        cursor = self.conn.execute(
            """INSERT INTO task_artifacts
               (task_id, step, path, artifact_type, size_bytes, size_human, line_count, description)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                task_id,
                artifact.get("step"),
                artifact.get("path", ""),
                artifact.get("artifact_type", "unknown"),
                artifact.get("size_bytes", 0),
                artifact.get("size_human", "0 B"),
                artifact.get("line_count"),
                artifact.get("description"),
            ),
        )
        self.conn.commit()
        return cursor.lastrowid

    def get_artifacts(self, task_id: int) -> list[dict]:
        """Get all artifacts for a task, oldest first."""
        rows = self.conn.execute(
            "SELECT * FROM task_artifacts WHERE task_id = ? ORDER BY id ASC",
            (task_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    # ── v2: State management ───────────────────────────────────

    def set_state(self, task_id: int, state: str) -> bool:
        """Update task state and updated_at timestamp."""
        now = now_local_iso()
        cursor = self.conn.execute(
            "UPDATE tasks SET state = ?, updated_at = ? WHERE id = ?",
            (state, now, task_id),
        )
        self.conn.commit()
        return cursor.rowcount > 0

    def get_tasks_by_state(self, state: str, limit: int = 50) -> list[dict]:
        """Get tasks filtered by state."""
        rows = self.conn.execute(
            "SELECT * FROM tasks WHERE state = ? ORDER BY created_at DESC LIMIT ?",
            (state, limit),
        ).fetchall()
        return [dict(r) for r in rows]

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

    def get_auto_executable(self) -> list[dict]:
        """Get pending tasks marked as auto_executable."""
        rows = self.conn.execute(
            """SELECT * FROM tasks
               WHERE status = 'pending' AND auto_executable = 'yes'
               ORDER BY due IS NULL, due ASC"""
        ).fetchall()
        return [dict(r) for r in rows]

    def get_recently_executed(self, limit: int = 10) -> list[dict]:
        """Get tasks that were auto-executed (done or partial), ordered by most recent."""
        rows = self.conn.execute(
            """SELECT * FROM tasks
               WHERE executed_at IS NOT NULL
               ORDER BY executed_at DESC
               LIMIT ?""",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

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
