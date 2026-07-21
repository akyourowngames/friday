"""Durable goal management backed by the shared local Ares database.

Goals express what and why. Durable ``Task`` records remain the execution
engine for how. This store links the two and maintains an append-only evidence
timeline so progress is inspectable rather than a context-free percentage.
"""

from __future__ import annotations

import json
import re
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator
from uuid import uuid4

from ares.config import get_db_path
from ares.infra.sqlite_utils import connect_sqlite


_UNSET = object()
_WHITESPACE = re.compile(r"\s+")
_STATUSES = {"active", "paused", "completed", "abandoned"}
_PRIORITIES = {"low", "normal", "high"}
_SOURCES = {"manual", "ares-suggested", "reflection", "import"}
_LINK_TYPES = {"task", "action", "watcher"}
_SIGNAL_RESOLUTIONS = {"dismissed", "reviewed", "goal_updated", "goal_completed"}


class GoalConflictError(ValueError):
    """A goal mutation used a stale revision or invalid hierarchy."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _clean(value: Any, *, field: str, maximum: int, required: bool = False) -> str:
    text = _WHITESPACE.sub(" ", str(value or "")).strip()
    if required and not text:
        raise ValueError(f"{field} is required")
    if len(text) > maximum:
        raise ValueError(f"{field} must be at most {maximum} characters")
    return text


def _target_date(value: Any) -> str | None:
    if value is None or str(value).strip() == "":
        return None
    text = str(value).strip()
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError as exc:
        raise ValueError("target_date must be an ISO date (YYYY-MM-DD)") from exc


def _confidence(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("confidence must be a number from 0 to 1") from exc
    if not 0 <= number <= 1:
        raise ValueError("confidence must be from 0 to 1")
    return number


def _progress(value: Any) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("progress_percent must be an integer from 0 to 100") from exc
    if not 0 <= number <= 100:
        raise ValueError("progress_percent must be from 0 to 100")
    return number


def goal_public_view(goal: dict[str, Any]) -> dict[str, Any]:
    """Return a stable model/UI view without internal database fields."""
    fields = (
        "goal_id", "title", "description", "status", "category", "priority",
        "progress_percent", "progress_mode", "target_date", "parent_goal_id",
        "created_at", "updated_at", "completed_at", "source", "confidence",
        "revision", "is_overdue", "days_remaining", "milestones", "next_action",
        "blockers", "last_activity_at", "last_reminder_at", "source_conversation_id",
    )
    return {key: goal.get(key) for key in fields}


class GoalStore:
    """Revisioned SQLite goal records, hierarchy, evidence links, and timeline."""

    _UPDATABLE = {
        "title", "description", "status", "category", "priority",
        "progress_percent", "target_date", "parent_goal_id", "source", "confidence",
        "next_action",
    }

    def __init__(
        self,
        db_path: str | Path | None = None,
        *,
        connection: sqlite3.Connection | None = None,
        task_store: Any | None = None,
    ) -> None:
        self.db_path = Path(db_path) if db_path is not None else get_db_path()
        self._owns_connection = connection is None
        if connection is None:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            self.conn = connect_sqlite(self.db_path)
        else:
            self.conn = connection
        self.task_store = task_store
        self.fts_enabled = False
        self._init_db()

    def _init_db(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS goals_meta (
                goal_id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'active',
                category TEXT NOT NULL DEFAULT 'general',
                priority TEXT NOT NULL DEFAULT 'normal',
                progress_percent INTEGER NOT NULL DEFAULT 0,
                progress_mode TEXT NOT NULL DEFAULT 'manual',
                target_date TEXT,
                parent_goal_id INTEGER,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                completed_at TEXT,
                source TEXT NOT NULL DEFAULT 'manual',
                confidence REAL NOT NULL DEFAULT 1.0,
                next_action TEXT NOT NULL DEFAULT '',
                last_activity_at TEXT,
                last_reminder_at TEXT,
                source_conversation_id TEXT,
                revision INTEGER NOT NULL DEFAULT 1,
                FOREIGN KEY (parent_goal_id) REFERENCES goals_meta(goal_id) ON DELETE SET NULL
            );
            CREATE TABLE IF NOT EXISTS goal_links (
                goal_id INTEGER NOT NULL,
                link_type TEXT NOT NULL,
                ref_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (goal_id, link_type, ref_id),
                FOREIGN KEY (goal_id) REFERENCES goals_meta(goal_id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS goal_events (
                event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                goal_id INTEGER NOT NULL,
                event_type TEXT NOT NULL,
                note TEXT NOT NULL DEFAULT '',
                progress_percent INTEGER,
                created_at TEXT NOT NULL,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                FOREIGN KEY (goal_id) REFERENCES goals_meta(goal_id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS goal_milestones (
                milestone_id INTEGER PRIMARY KEY AUTOINCREMENT,
                goal_id INTEGER NOT NULL,
                title TEXT NOT NULL,
                position INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                target_date TEXT,
                created_at TEXT NOT NULL,
                completed_at TEXT,
                FOREIGN KEY (goal_id) REFERENCES goals_meta(goal_id) ON DELETE CASCADE,
                UNIQUE(goal_id, position)
            );
            CREATE TABLE IF NOT EXISTS goal_blockers (
                blocker_id INTEGER PRIMARY KEY AUTOINCREMENT,
                goal_id INTEGER NOT NULL,
                description TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'active',
                created_at TEXT NOT NULL,
                resolved_at TEXT,
                FOREIGN KEY (goal_id) REFERENCES goals_meta(goal_id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS goal_watcher_signals (
                signal_id INTEGER PRIMARY KEY AUTOINCREMENT,
                goal_id INTEGER NOT NULL,
                watcher_id TEXT NOT NULL,
                source_event_id TEXT NOT NULL,
                event_type TEXT NOT NULL DEFAULT 'change',
                event_summary TEXT NOT NULL,
                old_value TEXT,
                new_value TEXT,
                severity TEXT NOT NULL DEFAULT 'info',
                created_at TEXT NOT NULL,
                acknowledged INTEGER NOT NULL DEFAULT 0,
                acknowledged_at TEXT,
                resolution TEXT,
                resolution_note TEXT NOT NULL DEFAULT '',
                surfaced_count INTEGER NOT NULL DEFAULT 0,
                last_surfaced_at TEXT,
                snoozed_until TEXT,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                FOREIGN KEY (goal_id) REFERENCES goals_meta(goal_id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_goals_status ON goals_meta(status, target_date);
            CREATE INDEX IF NOT EXISTS idx_goals_parent ON goals_meta(parent_goal_id);
            CREATE INDEX IF NOT EXISTS idx_goal_events_goal ON goal_events(goal_id, event_id DESC);
            CREATE INDEX IF NOT EXISTS idx_goal_milestones_goal ON goal_milestones(goal_id, position);
            CREATE INDEX IF NOT EXISTS idx_goal_blockers_goal ON goal_blockers(goal_id, status);
            CREATE INDEX IF NOT EXISTS idx_goal_signals_unack
                ON goal_watcher_signals(goal_id, acknowledged, created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_goal_signals_watcher
                ON goal_watcher_signals(watcher_id, created_at DESC);
            """
        )
        self._migrate_goal_schema()
        self._migrate_signal_schema()
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_goals_activity ON goals_meta(status, last_activity_at)"
        )
        self.conn.execute(
            """CREATE UNIQUE INDEX IF NOT EXISTS idx_goal_signal_source
               ON goal_watcher_signals(goal_id, source_event_id)
               WHERE source_event_id <> ''"""
        )
        try:
            self.conn.execute(
                """
                CREATE VIRTUAL TABLE IF NOT EXISTS goals_fts USING fts5(
                    title, description, content='goals_meta', content_rowid='goal_id'
                )
                """
            )
            self.fts_enabled = True
            count = self.conn.execute("SELECT COUNT(*) FROM goals_fts").fetchone()[0]
            meta_count = self.conn.execute("SELECT COUNT(*) FROM goals_meta").fetchone()[0]
            if count != meta_count:
                self.conn.execute("INSERT INTO goals_fts(goals_fts) VALUES('rebuild')")
        except sqlite3.DatabaseError:
            self.fts_enabled = False
        self.conn.commit()

    @staticmethod
    def _default_milestones(title: str) -> list[dict[str, Any]]:
        return [
            {"title": f"Define success criteria for {title}"},
            {"title": f"Complete the core work for {title}"},
            {"title": f"Review and close {title}"},
        ]

    def _migrate_goal_schema(self) -> None:
        """Add actionable fields and safely backfill existing local goals."""
        existing = {row["name"] for row in self.conn.execute("PRAGMA table_info(goals_meta)")}
        additions = {
            "next_action": "TEXT NOT NULL DEFAULT ''",
            "last_activity_at": "TEXT",
            "last_reminder_at": "TEXT",
            "source_conversation_id": "TEXT",
        }
        for name, definition in additions.items():
            if name not in existing:
                self.conn.execute(f"ALTER TABLE goals_meta ADD COLUMN {name} {definition}")
        self.conn.execute(
            """UPDATE goals_meta SET last_activity_at=COALESCE(last_activity_at, updated_at, created_at),
               next_action=CASE WHEN TRIM(COALESCE(next_action, ''))='' THEN
                 'Define the next concrete step for ' || title ELSE next_action END"""
        )
        rows = self.conn.execute(
            """SELECT goal_id, title FROM goals_meta WHERE NOT EXISTS (
                   SELECT 1 FROM goal_milestones m WHERE m.goal_id=goals_meta.goal_id
               )"""
        ).fetchall()
        now = utc_now()
        for row in rows:
            for position, item in enumerate(self._default_milestones(str(row["title"])), start=1):
                self.conn.execute(
                    """INSERT INTO goal_milestones
                       (goal_id, title, position, status, target_date, created_at, completed_at)
                       VALUES (?, ?, ?, 'pending', NULL, ?, NULL)""",
                    (int(row["goal_id"]), item["title"], position, now),
                )

    def _migrate_signal_schema(self) -> None:
        """Upgrade early goal-signal prototypes without discarding local evidence."""
        existing = {
            row["name"] for row in self.conn.execute("PRAGMA table_info(goal_watcher_signals)")
        }
        additions = {
            "source_event_id": "TEXT NOT NULL DEFAULT ''",
            "event_type": "TEXT NOT NULL DEFAULT 'change'",
            "old_value": "TEXT",
            "new_value": "TEXT",
            "severity": "TEXT NOT NULL DEFAULT 'info'",
            "acknowledged_at": "TEXT",
            "resolution": "TEXT",
            "resolution_note": "TEXT NOT NULL DEFAULT ''",
            "surfaced_count": "INTEGER NOT NULL DEFAULT 0",
            "last_surfaced_at": "TEXT",
            "snoozed_until": "TEXT",
            "metadata_json": "TEXT NOT NULL DEFAULT '{}'",
        }
        for name, definition in additions.items():
            if name not in existing:
                self.conn.execute(f"ALTER TABLE goal_watcher_signals ADD COLUMN {name} {definition}")

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self.conn.rollback()
            raise
        else:
            self.conn.commit()

    @staticmethod
    def _validate_choice(value: Any, choices: set[str], field: str) -> str:
        normalized = str(value or "").strip().casefold()
        if normalized not in choices:
            raise ValueError(f"{field} must be one of: {', '.join(sorted(choices))}")
        return normalized

    def _insert_fts(self, goal_id: int, title: str, description: str) -> None:
        if self.fts_enabled:
            self.conn.execute(
                "INSERT INTO goals_fts(rowid, title, description) VALUES (?, ?, ?)",
                (goal_id, title, description),
            )

    def _replace_fts(self, existing: dict[str, Any], title: str, description: str) -> None:
        if self.fts_enabled:
            self.conn.execute(
                "INSERT INTO goals_fts(goals_fts, rowid, title, description) VALUES('delete', ?, ?, ?)",
                (existing["goal_id"], existing["title"], existing["description"]),
            )
            self._insert_fts(int(existing["goal_id"]), title, description)

    def _event(
        self,
        goal_id: int,
        event_type: str,
        *,
        note: str = "",
        progress_percent: int | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        self.conn.execute(
            """INSERT INTO goal_events
               (goal_id, event_type, note, progress_percent, created_at, metadata_json)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                int(goal_id), event_type, _clean(note, field="note", maximum=2_000),
                progress_percent, utc_now(), json.dumps(metadata or {}, ensure_ascii=False, sort_keys=True),
            ),
        )

    def _row_to_goal(self, row: sqlite3.Row) -> dict[str, Any]:
        target = row["target_date"]
        remaining: int | None = None
        overdue = False
        if target:
            remaining = (date.fromisoformat(str(target)) - date.today()).days
            overdue = remaining < 0 and row["status"] == "active"
        goal_id = int(row["goal_id"])
        return {
            "goal_id": goal_id,
            "title": row["title"],
            "description": row["description"],
            "status": row["status"],
            "category": row["category"],
            "priority": row["priority"],
            "progress_percent": int(row["progress_percent"]),
            "progress_mode": row["progress_mode"],
            "target_date": target,
            "parent_goal_id": int(row["parent_goal_id"]) if row["parent_goal_id"] is not None else None,
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "completed_at": row["completed_at"],
            "source": row["source"],
            "confidence": float(row["confidence"]),
            "next_action": row["next_action"] or "",
            "last_activity_at": row["last_activity_at"] or row["updated_at"],
            "last_reminder_at": row["last_reminder_at"],
            "source_conversation_id": row["source_conversation_id"],
            "revision": int(row["revision"]),
            "is_overdue": overdue,
            "days_remaining": remaining,
            "milestones": self.list_milestones(goal_id),
            "blockers": self.list_blockers(goal_id),
        }

    def _normalize_milestones(
        self, milestones: list[dict[str, Any] | str] | None, title: str,
    ) -> list[dict[str, Any]]:
        values = milestones or self._default_milestones(title)
        if not isinstance(values, list) or not values:
            values = self._default_milestones(title)
        if len(values) > 20:
            raise ValueError("A goal may have at most 20 milestones")
        normalized: list[dict[str, Any]] = []
        for item in values:
            record = {"title": item} if isinstance(item, str) else item
            if not isinstance(record, dict):
                raise ValueError("Each milestone must be text or an object")
            normalized.append({
                "title": _clean(record.get("title"), field="milestone title", maximum=300, required=True),
                "target_date": _target_date(record.get("target_date")),
            })
        return normalized

    def _normalize_blockers(self, blockers: list[dict[str, Any] | str] | None) -> list[str]:
        if blockers is None:
            return []
        if not isinstance(blockers, list) or len(blockers) > 20:
            raise ValueError("blockers must be a list of at most 20 items")
        result: list[str] = []
        for item in blockers:
            value = item.get("description") if isinstance(item, dict) else item
            result.append(_clean(value, field="blocker", maximum=500, required=True))
        return result

    def _insert_goal_plan(
        self,
        goal_id: int,
        milestones: list[dict[str, Any]],
        blockers: list[str],
        *,
        created_at: str,
    ) -> None:
        for position, item in enumerate(milestones, start=1):
            self.conn.execute(
                """INSERT INTO goal_milestones
                   (goal_id, title, position, status, target_date, created_at, completed_at)
                   VALUES (?, ?, ?, 'pending', ?, ?, NULL)""",
                (goal_id, item["title"], position, item.get("target_date"), created_at),
            )
        for description in blockers:
            self.conn.execute(
                """INSERT INTO goal_blockers
                   (goal_id, description, status, created_at, resolved_at)
                   VALUES (?, ?, 'active', ?, NULL)""",
                (goal_id, description, created_at),
            )

    def create(
        self,
        title: str,
        *,
        description: str = "",
        category: str = "general",
        priority: str = "normal",
        target_date: str | None = None,
        parent_goal_id: int | None = None,
        source: str = "manual",
        confidence: float = 1.0,
        milestones: list[dict[str, Any] | str] | None = None,
        next_action: str = "",
        blockers: list[dict[str, Any] | str] | None = None,
        source_conversation_id: str | None = None,
    ) -> dict[str, Any]:
        clean_title = _clean(title, field="title", maximum=300, required=True)
        clean_description = _clean(description, field="description", maximum=6_000)
        clean_category = _clean(category or "general", field="category", maximum=80, required=True).casefold()
        clean_priority = self._validate_choice(priority, _PRIORITIES, "priority")
        clean_source = self._validate_choice(source, _SOURCES, "source")
        clean_milestones = self._normalize_milestones(milestones, clean_title)
        clean_next_action = _clean(next_action, field="next_action", maximum=1_000)
        if not clean_next_action:
            clean_next_action = f"Define success criteria for {clean_title}"
        clean_blockers = self._normalize_blockers(blockers)
        clean_source_conversation = _clean(
            source_conversation_id, field="source_conversation_id", maximum=180,
        ) or None
        clean_target = _target_date(target_date)
        parent_id = int(parent_goal_id) if parent_goal_id is not None else None
        if parent_id is not None and self.get(parent_id) is None:
            raise ValueError(f"Parent goal #{parent_id} was not found")
        now = utc_now()
        with self._transaction():
            cursor = self.conn.execute(
                """INSERT INTO goals_meta
                   (title, description, status, category, priority, progress_percent,
                    progress_mode, target_date, parent_goal_id, created_at, updated_at,
                    completed_at, source, confidence, next_action, last_activity_at,
                    last_reminder_at, source_conversation_id, revision)
                   VALUES (?, ?, 'active', ?, ?, 0, 'manual', ?, ?, ?, ?, NULL, ?, ?, ?, ?, NULL, ?, 1)""",
                (
                    clean_title, clean_description, clean_category, clean_priority,
                    clean_target, parent_id, now, now, clean_source, _confidence(confidence),
                    clean_next_action, now, clean_source_conversation,
                ),
            )
            goal_id = int(cursor.lastrowid)
            self._insert_goal_plan(goal_id, clean_milestones, clean_blockers, created_at=now)
            self._insert_fts(goal_id, clean_title, clean_description)
            self._event(goal_id, "created", metadata={"source": clean_source})
        return self.get(goal_id) or {}

    def get(self, goal_id: int) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM goals_meta WHERE goal_id = ?", (int(goal_id),)).fetchone()
        return self._row_to_goal(row) if row else None

    def list_milestones(self, goal_id: int) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM goal_milestones WHERE goal_id=? ORDER BY position, milestone_id",
            (int(goal_id),),
        ).fetchall()
        return [dict(row) for row in rows]

    def list_blockers(self, goal_id: int, *, include_resolved: bool = False) -> list[dict[str, Any]]:
        where = "" if include_resolved else " AND status='active'"
        rows = self.conn.execute(
            f"SELECT * FROM goal_blockers WHERE goal_id=?{where} ORDER BY blocker_id",
            (int(goal_id),),
        ).fetchall()
        return [dict(row) for row in rows]

    def _assert_revision(self, existing: dict[str, Any], expected_revision: int | None) -> None:
        if expected_revision is not None and existing["revision"] != int(expected_revision):
            raise GoalConflictError(
                f"Goal #{existing['goal_id']} changed since revision {expected_revision}; current revision is {existing['revision']}."
            )

    def _assert_parent(self, goal_id: int, parent_goal_id: int | None) -> None:
        if parent_goal_id is None:
            return
        if parent_goal_id == goal_id:
            raise GoalConflictError("A goal cannot be its own parent")
        current = self.get(parent_goal_id)
        if current is None:
            raise ValueError(f"Parent goal #{parent_goal_id} was not found")
        seen = {goal_id}
        while current is not None:
            current_id = int(current["goal_id"])
            if current_id in seen:
                raise GoalConflictError("Goal hierarchy would contain a cycle")
            seen.add(current_id)
            parent = current.get("parent_goal_id")
            current = self.get(int(parent)) if parent is not None else None

    def update(
        self,
        goal_id: int,
        *,
        expected_revision: int | None = None,
        resolves_signal_id: int | None = None,
        signal_resolution: str = "goal_updated",
        **fields: Any,
    ) -> dict[str, Any] | None:
        unknown = set(fields).difference(self._UPDATABLE)
        if unknown:
            raise ValueError(f"Unknown or immutable goal field(s): {', '.join(sorted(unknown))}")
        existing = self.get(int(goal_id))
        if existing is None:
            return None
        self._assert_revision(existing, expected_revision)
        signal = None
        if resolves_signal_id is not None:
            signal = self.get_watcher_signal(int(resolves_signal_id))
            if signal is None:
                raise ValueError(f"Goal watcher signal #{resolves_signal_id} was not found")
            if int(signal["goal_id"]) != int(goal_id):
                raise ValueError(f"Signal #{resolves_signal_id} does not belong to goal #{goal_id}")
            signal_resolution = self._validate_choice(signal_resolution, _SIGNAL_RESOLUTIONS, "signal_resolution")
        values = dict(existing)
        if "title" in fields:
            values["title"] = _clean(fields["title"], field="title", maximum=300, required=True)
        if "description" in fields:
            values["description"] = _clean(fields["description"], field="description", maximum=6_000)
        if "status" in fields:
            values["status"] = self._validate_choice(fields["status"], _STATUSES, "status")
        if "category" in fields:
            values["category"] = _clean(fields["category"], field="category", maximum=80, required=True).casefold()
        if "priority" in fields:
            values["priority"] = self._validate_choice(fields["priority"], _PRIORITIES, "priority")
        if "progress_percent" in fields:
            values["progress_percent"] = _progress(fields["progress_percent"])
            values["progress_mode"] = "manual"
        if "target_date" in fields:
            values["target_date"] = _target_date(fields["target_date"])
        if "parent_goal_id" in fields:
            parent = fields["parent_goal_id"]
            values["parent_goal_id"] = int(parent) if parent is not None else None
            self._assert_parent(int(goal_id), values["parent_goal_id"])
        if "source" in fields:
            values["source"] = self._validate_choice(fields["source"], _SOURCES, "source")
        if "confidence" in fields:
            values["confidence"] = _confidence(fields["confidence"])
        if "next_action" in fields:
            values["next_action"] = _clean(
                fields["next_action"], field="next_action", maximum=1_000, required=True,
            )
        if values["status"] == "completed":
            values["progress_percent"] = 100
            values["completed_at"] = existing.get("completed_at") or utc_now()
        elif existing["status"] == "completed":
            values["completed_at"] = None
        now = utc_now()
        with self._transaction():
            self.conn.execute(
                """UPDATE goals_meta SET title=?, description=?, status=?, category=?, priority=?,
                   progress_percent=?, progress_mode=?, target_date=?, parent_goal_id=?, updated_at=?,
                   completed_at=?, source=?, confidence=?, next_action=?, last_activity_at=?,
                   revision=revision+1 WHERE goal_id=?""",
                (
                    values["title"], values["description"], values["status"], values["category"], values["priority"],
                    values["progress_percent"], values["progress_mode"], values["target_date"], values["parent_goal_id"],
                    now, values["completed_at"], values["source"], values["confidence"],
                    values["next_action"], now, int(goal_id),
                ),
            )
            self._replace_fts(existing, values["title"], values["description"])
            self._event(int(goal_id), "updated", progress_percent=values["progress_percent"], metadata={"fields": sorted(fields)})
            if signal is not None and not signal["acknowledged"]:
                self._acknowledge_signal_in_transaction(
                    signal,
                    resolution=signal_resolution,
                    note=f"Resolved while updating goal #{goal_id}",
                )
        return self.get(int(goal_id))

    def _set_status(
        self,
        goal_id: int,
        status: str,
        expected_revision: int | None = None,
        resolves_signal_id: int | None = None,
    ) -> dict[str, Any] | None:
        goal = self.update(
            goal_id,
            expected_revision=expected_revision,
            resolves_signal_id=resolves_signal_id,
            signal_resolution="goal_completed" if status == "completed" else "goal_updated",
            status=status,
        )
        if goal:
            with self._transaction():
                self._event(goal_id, status, progress_percent=goal["progress_percent"])
        return self.get(goal_id) if goal else None

    def complete(
        self,
        goal_id: int,
        *,
        expected_revision: int | None = None,
        resolves_signal_id: int | None = None,
    ) -> dict[str, Any] | None:
        return self._set_status(goal_id, "completed", expected_revision, resolves_signal_id)

    def pause(self, goal_id: int, *, expected_revision: int | None = None) -> dict[str, Any] | None:
        return self._set_status(goal_id, "paused", expected_revision)

    def abandon(self, goal_id: int, *, expected_revision: int | None = None) -> dict[str, Any] | None:
        return self._set_status(goal_id, "abandoned", expected_revision)

    def delete(self, goal_id: int, *, expected_revision: int | None = None) -> bool:
        existing = self.get(int(goal_id))
        if existing is None:
            return False
        self._assert_revision(existing, expected_revision)
        with self._transaction():
            if self.fts_enabled:
                self.conn.execute(
                    "INSERT INTO goals_fts(goals_fts, rowid, title, description) VALUES('delete', ?, ?, ?)",
                    (int(goal_id), existing["title"], existing["description"]),
                )
            self.conn.execute("DELETE FROM goals_meta WHERE goal_id = ?", (int(goal_id),))
        return True

    def decompose(self, goal_id: int, subgoals: list[dict[str, Any]]) -> list[dict[str, Any]]:
        parent = self.get(int(goal_id))
        if parent is None:
            raise ValueError(f"Goal #{goal_id} was not found")
        if not isinstance(subgoals, list) or not subgoals:
            raise ValueError("subgoals must be a non-empty list")
        if len(subgoals) > 50:
            raise ValueError("A goal may be decomposed into at most 50 children per call")
        validated: list[dict[str, Any]] = []
        for item in subgoals:
            if not isinstance(item, dict):
                raise ValueError("Each subgoal must be an object")
            subgoal_title = _clean(item.get("title"), field="subgoal title", maximum=300, required=True)
            validated.append({
                "title": subgoal_title,
                "description": _clean(item.get("description", ""), field="subgoal description", maximum=6_000),
                "category": _clean(item.get("category", parent["category"]), field="category", maximum=80, required=True).casefold(),
                "priority": self._validate_choice(item.get("priority", parent["priority"]), _PRIORITIES, "priority"),
                "target_date": _target_date(item.get("target_date")),
                "next_action": _clean(item.get("next_action"), field="next_action", maximum=1_000)
                or f"Define success criteria for {subgoal_title}",
                "milestones": self._normalize_milestones(item.get("milestones"), subgoal_title),
                "blockers": self._normalize_blockers(item.get("blockers")),
            })
        created_ids: list[int] = []
        with self._transaction():
            for item in validated:
                now = utc_now()
                cursor = self.conn.execute(
                    """INSERT INTO goals_meta
                       (title, description, status, category, priority, progress_percent,
                        progress_mode, target_date, parent_goal_id, created_at, updated_at,
                        source, confidence, next_action, last_activity_at, last_reminder_at,
                        source_conversation_id, revision)
                       VALUES (?, ?, 'active', ?, ?, 0, 'manual', ?, ?, ?, ?, 'manual', 1.0,
                               ?, ?, NULL, ?, 1)""",
                    (
                        item["title"], item["description"], item["category"], item["priority"],
                        item["target_date"], int(goal_id), now, now, item["next_action"], now,
                        parent.get("source_conversation_id"),
                    ),
                )
                child_id = int(cursor.lastrowid)
                created_ids.append(child_id)
                self._insert_goal_plan(
                    child_id, item["milestones"], item["blockers"], created_at=now,
                )
                self._insert_fts(child_id, item["title"], item["description"])
                self._event(child_id, "created", metadata={"parent_goal_id": int(goal_id)})
            self.conn.execute(
                "UPDATE goals_meta SET updated_at=?, last_activity_at=?, revision=revision+1 WHERE goal_id=?",
                (utc_now(), utc_now(), int(goal_id)),
            )
            self._event(int(goal_id), "decomposed", metadata={"child_goal_ids": created_ids})
        return [goal for child_id in created_ids if (goal := self.get(child_id)) is not None]

    def link(self, goal_id: int, *, link_type: str, ref_id: str) -> None:
        if self.get(int(goal_id)) is None:
            raise ValueError(f"Goal #{goal_id} was not found")
        kind = self._validate_choice(link_type, _LINK_TYPES, "link_type")
        reference = _clean(ref_id, field="ref_id", maximum=180, required=True)
        with self._transaction():
            self.conn.execute(
                "INSERT OR IGNORE INTO goal_links(goal_id, link_type, ref_id, created_at) VALUES (?, ?, ?, ?)",
                (int(goal_id), kind, reference, utc_now()),
            )
            now = utc_now()
            self.conn.execute(
                "UPDATE goals_meta SET updated_at=?, last_activity_at=?, revision=revision+1 WHERE goal_id=?",
                (now, now, int(goal_id)),
            )
            self._event(int(goal_id), "linked", metadata={"link_type": kind, "ref_id": reference})

    def unlink(self, goal_id: int, *, link_type: str, ref_id: str) -> None:
        kind = self._validate_choice(link_type, _LINK_TYPES, "link_type")
        with self._transaction():
            cursor = self.conn.execute("DELETE FROM goal_links WHERE goal_id=? AND link_type=? AND ref_id=?", (int(goal_id), kind, str(ref_id)))
            if cursor.rowcount:
                now = utc_now()
                self.conn.execute(
                    "UPDATE goals_meta SET updated_at=?, last_activity_at=?, revision=revision+1 WHERE goal_id=?",
                    (now, now, int(goal_id)),
                )
                self._event(int(goal_id), "unlinked", metadata={"link_type": kind, "ref_id": str(ref_id)})

    def linked_refs(self, goal_id: int) -> dict[str, list[str]]:
        result = {"tasks": [], "actions": [], "watchers": []}
        rows = self.conn.execute("SELECT link_type, ref_id FROM goal_links WHERE goal_id=? ORDER BY created_at", (int(goal_id),)).fetchall()
        for row in rows:
            result[{"task": "tasks", "action": "actions", "watcher": "watchers"}[row["link_type"]]].append(str(row["ref_id"]))
        return result

    def linked_goals(self, *, link_type: str, ref_id: str) -> list[dict[str, Any]]:
        """Return every goal interested in one durable task/action/watcher reference."""
        kind = self._validate_choice(link_type, _LINK_TYPES, "link_type")
        rows = self.conn.execute(
            """SELECT g.* FROM goal_links l JOIN goals_meta g ON g.goal_id=l.goal_id
               WHERE l.link_type=? AND l.ref_id=?
               ORDER BY CASE g.priority WHEN 'high' THEN 0 WHEN 'normal' THEN 1 ELSE 2 END,
                        g.updated_at DESC""",
            (kind, _clean(ref_id, field="ref_id", maximum=180, required=True)),
        ).fetchall()
        return [self._row_to_goal(row) for row in rows]

    def unlink_reference(self, *, link_type: str, ref_id: str) -> list[int]:
        """Remove a deleted external reference from all goals and retain timeline evidence."""
        goals = self.linked_goals(link_type=link_type, ref_id=ref_id)
        for goal in goals:
            self.unlink(int(goal["goal_id"]), link_type=link_type, ref_id=ref_id)
        return [int(goal["goal_id"]) for goal in goals]

    @staticmethod
    def _row_to_signal(row: sqlite3.Row) -> dict[str, Any]:
        try:
            metadata = json.loads(row["metadata_json"] or "{}")
        except (TypeError, json.JSONDecodeError):
            metadata = {}
        return {
            "signal_id": int(row["signal_id"]),
            "goal_id": int(row["goal_id"]),
            "watcher_id": str(row["watcher_id"]),
            "source_event_id": str(row["source_event_id"]),
            "event_type": str(row["event_type"]),
            "event_summary": str(row["event_summary"]),
            "old_value": row["old_value"],
            "new_value": row["new_value"],
            "severity": str(row["severity"]),
            "created_at": str(row["created_at"]),
            "acknowledged": bool(row["acknowledged"]),
            "acknowledged_at": row["acknowledged_at"],
            "resolution": row["resolution"],
            "resolution_note": str(row["resolution_note"] or ""),
            "surfaced_count": int(row["surfaced_count"] or 0),
            "last_surfaced_at": row["last_surfaced_at"],
            "snoozed_until": row["snoozed_until"],
            "metadata": metadata,
        }

    def get_watcher_signal(self, signal_id: int) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM goal_watcher_signals WHERE signal_id=?", (int(signal_id),)
        ).fetchone()
        return self._row_to_signal(row) if row else None

    def record_watcher_signal(
        self,
        goal_id: int,
        watcher_id: str,
        event_summary: str,
        *,
        source_event_id: str | None = None,
        event_type: str = "change",
        old_value: Any | None = None,
        new_value: Any | None = None,
        severity: str = "info",
        created_at: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Persist one idempotent watcher observation as goal evidence, never a goal mutation."""
        goal = self.get(int(goal_id))
        if goal is None:
            raise ValueError(f"Goal #{goal_id} was not found")
        watcher = _clean(watcher_id, field="watcher_id", maximum=180, required=True)
        summary = _clean(event_summary, field="event_summary", maximum=4_000, required=True)
        source = _clean(source_event_id or str(uuid4()), field="source_event_id", maximum=180, required=True)
        kind = _clean(event_type or "change", field="event_type", maximum=80, required=True).casefold()
        level = _clean(severity or "info", field="severity", maximum=32, required=True).casefold()
        stamp = str(created_at or utc_now())
        try:
            parsed_stamp = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
            parsed_stamp = parsed_stamp if parsed_stamp.tzinfo else parsed_stamp.replace(tzinfo=timezone.utc)
            stamp = parsed_stamp.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
        except ValueError as exc:
            raise ValueError("created_at must be an ISO timestamp") from exc
        with self._transaction():
            existing = self.conn.execute(
                "SELECT * FROM goal_watcher_signals WHERE goal_id=? AND source_event_id=?",
                (int(goal_id), source),
            ).fetchone()
            if existing is not None:
                signal = self._row_to_signal(existing)
                signal["created"] = False
                return signal
            cursor = self.conn.execute(
                """INSERT INTO goal_watcher_signals
                   (goal_id, watcher_id, source_event_id, event_type, event_summary,
                    old_value, new_value, severity, created_at, metadata_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    int(goal_id), watcher, source, kind, summary,
                    None if old_value is None else str(old_value)[:4_000],
                    None if new_value is None else str(new_value)[:4_000],
                    level, stamp,
                    json.dumps(metadata or {}, ensure_ascii=False, sort_keys=True, default=str),
                ),
            )
            signal_id = int(cursor.lastrowid)
            self._event(
                int(goal_id), "watcher_signal", note=summary,
                metadata={
                    "signal_id": signal_id, "watcher_id": watcher,
                    "source_event_id": source, "severity": level,
                },
            )
        signal = self.get_watcher_signal(signal_id) or {}
        signal["created"] = True
        return signal

    def list_watcher_signals(
        self,
        goal_id: int | None = None,
        *,
        include_acknowledged: bool = False,
        include_snoozed: bool = True,
        watcher_id: str | None = None,
        source_event_id: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if goal_id is not None:
            clauses.append("goal_id=?")
            params.append(int(goal_id))
        if not include_acknowledged:
            clauses.append("acknowledged=0")
        if not include_snoozed:
            clauses.append("(snoozed_until IS NULL OR snoozed_until<=?)")
            params.append(utc_now())
        if watcher_id:
            clauses.append("watcher_id=?")
            params.append(str(watcher_id))
        if source_event_id:
            clauses.append("source_event_id=?")
            params.append(str(source_event_id))
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self.conn.execute(
            f"SELECT * FROM goal_watcher_signals{where} ORDER BY created_at DESC, signal_id DESC LIMIT ?",
            (*params, max(1, min(int(limit), 500))),
        ).fetchall()
        return [self._row_to_signal(row) for row in rows]

    def pending_watcher_signals(
        self,
        goal_id: int | None = None,
        *,
        max_age_hours: int | None = None,
        max_surfaced: int | None = None,
        mark_surfaced: bool = False,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Return actionable signals; context callers can atomically count a surface."""
        clauses = ["acknowledged=0", "(snoozed_until IS NULL OR snoozed_until<=?)"]
        params: list[Any] = [utc_now()]
        if goal_id is not None:
            clauses.append("goal_id=?")
            params.append(int(goal_id))
        if max_age_hours is not None:
            cutoff = datetime.now(timezone.utc) - timedelta(hours=max(0, int(max_age_hours)))
            clauses.append("created_at>=?")
            params.append(cutoff.isoformat(timespec="seconds").replace("+00:00", "Z"))
        if max_surfaced is not None:
            clauses.append("surfaced_count<?")
            params.append(max(0, int(max_surfaced)))
        rows = self.conn.execute(
            f"""SELECT * FROM goal_watcher_signals WHERE {' AND '.join(clauses)}
                ORDER BY CASE severity WHEN 'critical' THEN 0 WHEN 'warning' THEN 1 ELSE 2 END,
                         created_at DESC, signal_id DESC LIMIT ?""",
            (*params, max(1, min(int(limit), 500))),
        ).fetchall()
        signals = [self._row_to_signal(row) for row in rows]
        if mark_surfaced and signals:
            stamp = utc_now()
            placeholders = ",".join("?" for _ in signals)
            with self._transaction():
                self.conn.execute(
                    f"""UPDATE goal_watcher_signals
                        SET surfaced_count=surfaced_count+1, last_surfaced_at=?
                        WHERE signal_id IN ({placeholders})""",
                    (stamp, *(signal["signal_id"] for signal in signals)),
                )
            for signal in signals:
                signal["surfaced_count"] += 1
                signal["last_surfaced_at"] = stamp
        return signals

    def contextualize_goals(
        self,
        goals: list[dict[str, Any]],
        *,
        max_age_hours: int = 48,
        max_surfaced: int = 3,
        per_goal: int = 3,
        mark_surfaced: bool = True,
    ) -> list[dict[str, Any]]:
        """Attach fresh pending signals to displayed goals and enforce anti-nag limits."""
        if not goals:
            return []
        goal_ids = {int(goal["goal_id"]) for goal in goals}
        signals: list[dict[str, Any]] = []
        for goal_id in goal_ids:
            signals.extend(self.pending_watcher_signals(
                goal_id,
                max_age_hours=max_age_hours,
                max_surfaced=max_surfaced,
                mark_surfaced=mark_surfaced,
                limit=per_goal,
            ))
        grouped: dict[int, list[dict[str, Any]]] = {goal_id: [] for goal_id in goal_ids}
        for signal in signals:
            bucket = grouped.get(int(signal["goal_id"]))
            if bucket is not None and len(bucket) < per_goal:
                bucket.append(signal)
        return [{**goal, "watcher_signals": grouped[int(goal["goal_id"])]} for goal in goals]

    def mark_watcher_signals_surfaced(self, signal_ids: list[int]) -> None:
        clean_ids = sorted({int(value) for value in signal_ids if int(value) > 0})
        if not clean_ids:
            return
        placeholders = ",".join("?" for _ in clean_ids)
        with self._transaction():
            self.conn.execute(
                f"""UPDATE goal_watcher_signals
                    SET surfaced_count=surfaced_count+1, last_surfaced_at=?
                    WHERE acknowledged=0 AND signal_id IN ({placeholders})""",
                (utc_now(), *clean_ids),
            )

    def _acknowledge_signal_in_transaction(
        self,
        signal: dict[str, Any],
        *,
        resolution: str,
        note: str = "",
    ) -> None:
        stamp = utc_now()
        self.conn.execute(
            """UPDATE goal_watcher_signals
               SET acknowledged=1, acknowledged_at=?, resolution=?, resolution_note=?, snoozed_until=NULL
               WHERE signal_id=?""",
            (stamp, resolution, _clean(note, field="note", maximum=2_000), int(signal["signal_id"])),
        )
        self._event(
            int(signal["goal_id"]), "watcher_signal_acknowledged", note=note,
            metadata={
                "signal_id": int(signal["signal_id"]), "resolution": resolution,
                "source_event_id": signal["source_event_id"],
            },
        )

    def acknowledge_watcher_signal(
        self,
        signal_id: int,
        *,
        resolution: str = "reviewed",
        note: str = "",
    ) -> dict[str, Any] | None:
        signal = self.get_watcher_signal(int(signal_id))
        if signal is None:
            return None
        normalized = self._validate_choice(resolution, _SIGNAL_RESOLUTIONS, "resolution")
        if not signal["acknowledged"]:
            with self._transaction():
                self._acknowledge_signal_in_transaction(signal, resolution=normalized, note=note)
        return self.get_watcher_signal(int(signal_id))

    def snooze_watcher_signal(
        self,
        signal_id: int,
        *,
        hours: int = 24,
        until: str | None = None,
        note: str = "",
    ) -> dict[str, Any] | None:
        signal = self.get_watcher_signal(int(signal_id))
        if signal is None:
            return None
        if signal["acknowledged"]:
            raise ValueError("An acknowledged watcher signal cannot be snoozed")
        if until:
            try:
                parsed = datetime.fromisoformat(str(until).replace("Z", "+00:00"))
            except ValueError as exc:
                raise ValueError("until must be an ISO timestamp") from exc
            parsed = parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        else:
            bounded_hours = max(1, min(int(hours), 24 * 365))
            parsed = datetime.now(timezone.utc) + timedelta(hours=bounded_hours)
        if parsed <= datetime.now(timezone.utc):
            raise ValueError("snoozed_until must be in the future")
        stamp = parsed.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
        with self._transaction():
            self.conn.execute(
                "UPDATE goal_watcher_signals SET snoozed_until=? WHERE signal_id=?",
                (stamp, int(signal_id)),
            )
            self._event(
                int(signal["goal_id"]), "watcher_signal_snoozed", note=note,
                metadata={"signal_id": int(signal_id), "snoozed_until": stamp},
            )
        return self.get_watcher_signal(int(signal_id))

    def list_events(self, goal_id: int, *, limit: int = 50) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM goal_events WHERE goal_id=? ORDER BY event_id DESC LIMIT ?",
            (int(goal_id), max(1, min(int(limit), 200))),
        ).fetchall()
        events: list[dict[str, Any]] = []
        for row in rows:
            try:
                metadata = json.loads(row["metadata_json"] or "{}")
            except (TypeError, json.JSONDecodeError):
                metadata = {}
            events.append({
                "event_id": int(row["event_id"]), "event_type": row["event_type"],
                "note": row["note"], "progress_percent": row["progress_percent"],
                "created_at": row["created_at"], "metadata": metadata,
            })
        return events

    def record_progress(
        self,
        goal_id: int,
        *,
        note: str,
        progress_percent: int | None = None,
        expected_revision: int | None = None,
    ) -> dict[str, Any] | None:
        existing = self.get(int(goal_id))
        if existing is None:
            return None
        self._assert_revision(existing, expected_revision)
        clean_note = _clean(note, field="note", maximum=2_000, required=True)
        percent = existing["progress_percent"] if progress_percent is None else _progress(progress_percent)
        with self._transaction():
            now = utc_now()
            self.conn.execute(
                """UPDATE goals_meta SET progress_percent=?, progress_mode='manual',
                   updated_at=?, last_activity_at=?, revision=revision+1 WHERE goal_id=?""",
                (percent, now, now, int(goal_id)),
            )
            self._event(int(goal_id), "progress", note=clean_note, progress_percent=percent)
        return self.get(int(goal_id))

    def set_milestone_status(
        self,
        goal_id: int,
        milestone_id: int,
        *,
        status: str,
        note: str = "",
    ) -> dict[str, Any] | None:
        normalized = self._validate_choice(status, {"pending", "completed", "skipped"}, "status")
        existing = self.conn.execute(
            "SELECT * FROM goal_milestones WHERE milestone_id=? AND goal_id=?",
            (int(milestone_id), int(goal_id)),
        ).fetchone()
        if existing is None:
            return None
        now = utc_now()
        with self._transaction():
            self.conn.execute(
                """UPDATE goal_milestones SET status=?, completed_at=?
                   WHERE milestone_id=? AND goal_id=?""",
                (
                    normalized,
                    now if normalized == "completed" else None,
                    int(milestone_id),
                    int(goal_id),
                ),
            )
            self.conn.execute(
                """UPDATE goals_meta SET updated_at=?, last_activity_at=?, revision=revision+1
                   WHERE goal_id=?""",
                (now, now, int(goal_id)),
            )
            self._event(
                int(goal_id), "milestone_updated", note=note,
                metadata={"milestone_id": int(milestone_id), "status": normalized},
            )
        return self.get(int(goal_id))

    def add_blocker(self, goal_id: int, description: str) -> dict[str, Any] | None:
        if self.get(int(goal_id)) is None:
            return None
        clean_description = _clean(description, field="blocker", maximum=500, required=True)
        now = utc_now()
        with self._transaction():
            cursor = self.conn.execute(
                """INSERT INTO goal_blockers
                   (goal_id, description, status, created_at, resolved_at)
                   VALUES (?, ?, 'active', ?, NULL)""",
                (int(goal_id), clean_description, now),
            )
            self.conn.execute(
                "UPDATE goals_meta SET updated_at=?, last_activity_at=?, revision=revision+1 WHERE goal_id=?",
                (now, now, int(goal_id)),
            )
            self._event(
                int(goal_id), "blocker_added", note=clean_description,
                metadata={"blocker_id": int(cursor.lastrowid)},
            )
        return self.get(int(goal_id))

    def resolve_blocker(self, goal_id: int, blocker_id: int) -> dict[str, Any] | None:
        now = utc_now()
        with self._transaction():
            cursor = self.conn.execute(
                """UPDATE goal_blockers SET status='resolved', resolved_at=?
                   WHERE blocker_id=? AND goal_id=? AND status='active'""",
                (now, int(blocker_id), int(goal_id)),
            )
            if not cursor.rowcount:
                return None
            self.conn.execute(
                "UPDATE goals_meta SET updated_at=?, last_activity_at=?, revision=revision+1 WHERE goal_id=?",
                (now, now, int(goal_id)),
            )
            self._event(
                int(goal_id), "blocker_resolved", metadata={"blocker_id": int(blocker_id)},
            )
        return self.get(int(goal_id))

    def inactive(self, *, before: str, limit: int = 50) -> list[dict[str, Any]]:
        try:
            parsed = datetime.fromisoformat(str(before).replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("before must be an ISO timestamp") from exc
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        cutoff = parsed.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
        rows = self.conn.execute(
            """SELECT * FROM goals_meta WHERE status='active' AND last_activity_at <= ?
               ORDER BY CASE priority WHEN 'high' THEN 0 WHEN 'normal' THEN 1 ELSE 2 END,
                        last_activity_at, target_date LIMIT ?""",
            (cutoff, max(1, min(int(limit), 200))),
        ).fetchall()
        return [self._row_to_goal(row) for row in rows]

    def mark_reminded(self, goal_id: int, *, when: str | None = None) -> dict[str, Any] | None:
        if self.get(int(goal_id)) is None:
            return None
        stamp = when or utc_now()
        try:
            parsed = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("when must be an ISO timestamp") from exc
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        stamp = parsed.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
        with self._transaction():
            self.conn.execute(
                "UPDATE goals_meta SET last_reminder_at=?, updated_at=? WHERE goal_id=?",
                (stamp, stamp, int(goal_id)),
            )
            self._event(int(goal_id), "reminded", metadata={"reminded_at": stamp})
        return self.get(int(goal_id))

    def list_all(
        self,
        *,
        statuses: list[str] | None = None,
        category: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if statuses:
            clean_statuses = [self._validate_choice(value, _STATUSES, "status") for value in statuses]
            clauses.append("status IN (" + ",".join("?" for _ in clean_statuses) + ")")
            params.extend(clean_statuses)
        if category:
            clauses.append("category = ? COLLATE NOCASE")
            params.append(str(category).strip())
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        rows = self.conn.execute(
            f"""SELECT * FROM goals_meta{where}
                ORDER BY CASE priority WHEN 'high' THEN 0 WHEN 'normal' THEN 1 ELSE 2 END,
                         CASE WHEN target_date IS NULL THEN 1 ELSE 0 END, target_date, updated_at DESC
                LIMIT ?""",
            [*params, max(1, min(int(limit), 500))],
        ).fetchall()
        return [self._row_to_goal(row) for row in rows]

    @staticmethod
    def _fts_query(query: str) -> str:
        tokens = re.findall(r"[\w]+", str(query), flags=re.UNICODE)
        return " AND ".join(f'"{token}"' for token in tokens)

    def search(self, query: str, *, limit: int = 10) -> list[dict[str, Any]]:
        clean_query = _clean(query, field="query", maximum=300, required=True)
        bounded = max(1, min(int(limit), 100))
        rows: list[sqlite3.Row] = []
        if self.fts_enabled and self._fts_query(clean_query):
            try:
                rows = self.conn.execute(
                    """SELECT g.* FROM goals_fts JOIN goals_meta g ON g.goal_id=goals_fts.rowid
                       WHERE goals_fts MATCH ? ORDER BY goals_fts.rank LIMIT ?""",
                    (self._fts_query(clean_query), bounded),
                ).fetchall()
            except sqlite3.DatabaseError:
                rows = []
        if not rows:
            like = f"%{clean_query}%"
            rows = self.conn.execute(
                """SELECT * FROM goals_meta WHERE title LIKE ? COLLATE NOCASE
                   OR description LIKE ? COLLATE NOCASE OR category LIKE ? COLLATE NOCASE
                   ORDER BY updated_at DESC LIMIT ?""",
                (like, like, like, bounded),
            ).fetchall()
        return [self._row_to_goal(row) for row in rows]

    def tree(self, goal_id: int) -> dict[str, Any]:
        root = self.get(int(goal_id))
        if root is None:
            raise ValueError(f"Goal #{goal_id} was not found")

        def build(goal: dict[str, Any], seen: set[int]) -> dict[str, Any]:
            current_id = int(goal["goal_id"])
            if current_id in seen:
                return {**goal_public_view(goal), "children": [], "cycle_detected": True}
            child_rows = self.conn.execute(
                "SELECT * FROM goals_meta WHERE parent_goal_id=? ORDER BY created_at, goal_id",
                (current_id,),
            ).fetchall()
            next_seen = {*seen, current_id}
            return {
                **goal_public_view(goal),
                "links": self.linked_refs(current_id),
                "children": [build(self._row_to_goal(row), next_seen) for row in child_rows],
            }

        return build(root, set())

    def due_soon(self, *, within_days: int = 7) -> list[dict[str, Any]]:
        today = date.today()
        upper = today + timedelta(days=max(0, min(int(within_days), 3650)))
        rows = self.conn.execute(
            """SELECT * FROM goals_meta WHERE status='active' AND target_date IS NOT NULL
               AND target_date >= ? AND target_date <= ? ORDER BY target_date, priority DESC""",
            (today.isoformat(), upper.isoformat()),
        ).fetchall()
        return [self._row_to_goal(row) for row in rows]

    def overdue(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """SELECT * FROM goals_meta WHERE status='active' AND target_date IS NOT NULL
               AND target_date < ? ORDER BY target_date, priority DESC""",
            (date.today().isoformat(),),
        ).fetchall()
        return [self._row_to_goal(row) for row in rows]

    def recalculate_progress(self, goal_id: int) -> dict[str, Any]:
        goal = self.get(int(goal_id))
        if goal is None:
            raise ValueError(f"Goal #{goal_id} was not found")
        children = self.conn.execute("SELECT status FROM goals_meta WHERE parent_goal_id=?", (int(goal_id),)).fetchall()
        refs = self.linked_refs(int(goal_id))
        completed = sum(1 for row in children if row["status"] == "completed")
        evidence = len(children)
        task_states: dict[str, str] = {}
        for task_id in refs["tasks"]:
            task = self.task_store.get_task(task_id) if self.task_store is not None else None
            state = str(task.get("status")) if task else "missing"
            task_states[task_id] = state
            evidence += 1
            if state == "completed":
                completed += 1
        # An explicitly linked action is completed historical evidence.
        evidence += len(refs["actions"])
        completed += len(refs["actions"])
        if evidence == 0:
            return goal
        percent = round(completed * 100 / evidence)
        with self._transaction():
            now = utc_now()
            self.conn.execute(
                """UPDATE goals_meta SET progress_percent=?, progress_mode='derived',
                   updated_at=?, last_activity_at=?, revision=revision+1 WHERE goal_id=?""",
                (percent, now, now, int(goal_id)),
            )
            self._event(
                int(goal_id), "progress_synced", progress_percent=percent,
                metadata={"completed_evidence": completed, "total_evidence": evidence, "task_states": task_states},
            )
        return self.get(int(goal_id)) or goal

    def list_all_for_export(self) -> list[dict[str, Any]]:
        return [
            {
                **goal,
                "blockers": self.list_blockers(goal["goal_id"], include_resolved=True),
                "links": self.linked_refs(goal["goal_id"]),
                "watcher_signals": list(reversed(self.list_watcher_signals(
                    goal["goal_id"], include_acknowledged=True, limit=500,
                ))),
                "events": list(reversed(self.list_events(goal["goal_id"], limit=200))),
            }
            for goal in self.list_all(limit=500)
        ]

    def import_goals(self, goals: list[dict[str, Any]]) -> int:
        if not isinstance(goals, list):
            return 0
        imported = 0
        id_map: dict[int, int] = {}
        imported_items: list[tuple[dict[str, Any], int]] = []
        # First create every record detached. Export order is intentionally a
        # display order and may place a child before its parent.
        for item in goals:
            if not isinstance(item, dict) or not str(item.get("title") or "").strip():
                continue
            old_id = int(item.get("goal_id", 0) or 0)
            created = self.create(
                item["title"], description=item.get("description", ""), category=item.get("category", "general"),
                priority=item.get("priority", "normal"), target_date=item.get("target_date"), parent_goal_id=None,
                source="import", confidence=item.get("confidence", 1.0),
                milestones=item.get("milestones"), next_action=item.get("next_action", ""),
                blockers=item.get("blockers"), source_conversation_id=item.get("source_conversation_id"),
            )
            new_id = int(created["goal_id"])
            if old_id:
                id_map[old_id] = new_id
            imported_items.append((item, new_id))
            imported += 1

        # Then restore hierarchy, state, links, and the exported evidence
        # timeline after all old ids have a deterministic new-id mapping.
        for item, new_id in imported_items:
            restored_milestones = self.list_milestones(new_id)
            for exported, restored in zip(item.get("milestones") or [], restored_milestones):
                status = str(exported.get("status") or "pending") if isinstance(exported, dict) else "pending"
                if status not in {"pending", "completed", "skipped"}:
                    status = "pending"
                with self._transaction():
                    self.conn.execute(
                        """UPDATE goal_milestones SET status=?, completed_at=?
                           WHERE milestone_id=?""",
                        (
                            status,
                            exported.get("completed_at") if status == "completed" else None,
                            int(restored["milestone_id"]),
                        ),
                    )
            restored_blockers = self.list_blockers(new_id, include_resolved=True)
            for exported, restored in zip(item.get("blockers") or [], restored_blockers):
                if not isinstance(exported, dict) or exported.get("status") != "resolved":
                    continue
                with self._transaction():
                    self.conn.execute(
                        """UPDATE goal_blockers SET status='resolved', resolved_at=?
                           WHERE blocker_id=?""",
                        (exported.get("resolved_at") or utc_now(), int(restored["blocker_id"])),
                    )
            updates: dict[str, Any] = {
                field: item[field]
                for field in ("status", "progress_percent")
                if field in item
            }
            old_parent = item.get("parent_goal_id")
            if old_parent is not None and int(old_parent) in id_map:
                updates["parent_goal_id"] = id_map[int(old_parent)]
            if updates:
                self.update(new_id, **updates)
            if item.get("progress_mode") == "derived":
                with self._transaction():
                    self.conn.execute("UPDATE goals_meta SET progress_mode='derived' WHERE goal_id=?", (new_id,))
            with self._transaction():
                self.conn.execute(
                    """UPDATE goals_meta SET last_activity_at=COALESCE(?, last_activity_at),
                       last_reminder_at=COALESCE(?, last_reminder_at) WHERE goal_id=?""",
                    (item.get("last_activity_at"), item.get("last_reminder_at"), new_id),
                )
            for task_id in (item.get("links") or {}).get("tasks", []):
                self.link(new_id, link_type="task", ref_id=str(task_id))
            for action_id in (item.get("links") or {}).get("actions", []):
                self.link(new_id, link_type="action", ref_id=str(action_id))
            for watcher_id in (item.get("links") or {}).get("watchers", []):
                self.link(new_id, link_type="watcher", ref_id=str(watcher_id))
            for signal in item.get("watcher_signals") or []:
                if not isinstance(signal, dict):
                    continue
                restored = self.record_watcher_signal(
                    new_id,
                    str(signal.get("watcher_id") or "imported-watcher"),
                    str(signal.get("event_summary") or "Imported watcher signal"),
                    source_event_id=str(signal.get("source_event_id") or uuid4()),
                    event_type=str(signal.get("event_type") or "change"),
                    old_value=signal.get("old_value"),
                    new_value=signal.get("new_value"),
                    severity=str(signal.get("severity") or "info"),
                    created_at=str(signal.get("created_at") or utc_now()),
                    metadata=signal.get("metadata") if isinstance(signal.get("metadata"), dict) else {},
                )
                with self._transaction():
                    self.conn.execute(
                        """UPDATE goal_watcher_signals SET acknowledged=?, acknowledged_at=?,
                           resolution=?, resolution_note=?, surfaced_count=?, last_surfaced_at=?, snoozed_until=?
                           WHERE signal_id=?""",
                        (
                            int(bool(signal.get("acknowledged"))), signal.get("acknowledged_at"),
                            signal.get("resolution"), str(signal.get("resolution_note") or ""),
                            max(0, int(signal.get("surfaced_count", 0) or 0)), signal.get("last_surfaced_at"),
                            signal.get("snoozed_until"), int(restored["signal_id"]),
                        ),
                    )
            events = item.get("events")
            if isinstance(events, list) and events:
                with self._transaction():
                    self.conn.execute("DELETE FROM goal_events WHERE goal_id=?", (new_id,))
                    for event in events[-200:]:
                        if not isinstance(event, dict):
                            continue
                        self.conn.execute(
                            """INSERT INTO goal_events
                               (goal_id, event_type, note, progress_percent, created_at, metadata_json)
                               VALUES (?, ?, ?, ?, ?, ?)""",
                            (
                                new_id,
                                _clean(event.get("event_type") or "imported", field="event_type", maximum=80, required=True),
                                _clean(event.get("note", ""), field="note", maximum=2_000),
                                event.get("progress_percent"),
                                str(event.get("created_at") or utc_now()),
                                json.dumps(event.get("metadata") if isinstance(event.get("metadata"), dict) else {}, ensure_ascii=False, sort_keys=True),
                            ),
                        )
        return imported

    def count(self) -> int:
        row = self.conn.execute("SELECT COUNT(*) AS count FROM goals_meta").fetchone()
        return int(row["count"]) if row else 0

    def close(self) -> None:
        if self._owns_connection:
            self.conn.close()


class GoalToolHandlers:
    """JSON adapters for the model-facing goal tool family."""

    def __init__(
        self,
        store: GoalStore,
        task_store: Any | None = None,
        action_ledger: Any | None = None,
        watcher_db_provider: Any | None = None,
    ) -> None:
        self.store = store
        self.task_store = task_store
        self.action_ledger = action_ledger
        self.watcher_db_provider = watcher_db_provider

    @staticmethod
    def _json(payload: dict[str, Any]) -> str:
        return json.dumps(payload, ensure_ascii=False, indent=2)

    def _mutate(self, operation: Any) -> str:
        try:
            goal = operation()
        except (ValueError, GoalConflictError) as exc:
            return self._json({"ok": False, "error": str(exc)})
        if goal is None:
            return self._json({"ok": False, "error": "Goal not found."})
        return self._json({"ok": True, "goal": goal_public_view(goal)})

    def _watcher_db(self) -> Any | None:
        try:
            return self.watcher_db_provider() if self.watcher_db_provider is not None else None
        except Exception:
            return None

    def _acknowledge_source_event_if_resolved(self, signal: dict[str, Any]) -> bool:
        remaining = self.store.list_watcher_signals(
            include_acknowledged=False,
            source_event_id=str(signal.get("source_event_id") or ""),
            limit=2,
        )
        if remaining:
            return False
        db = self._watcher_db()
        return bool(db and db.acknowledge_event(str(signal.get("source_event_id") or "")))

    def _mutate_with_signal(self, operation: Any, signal_id: Any) -> str:
        result = json.loads(self._mutate(operation))
        if result.get("ok") and signal_id is not None:
            signal = self.store.get_watcher_signal(int(signal_id))
            if signal is not None:
                result["resolved_signal"] = signal
                result["source_watcher_event_acknowledged"] = self._acknowledge_source_event_if_resolved(signal)
        return self._json(result)

    def create_goal(self, args: dict[str, Any]) -> str:
        return self._mutate(lambda: self.store.create(
            args.get("title", ""), description=args.get("description", ""), category=args.get("category", "general"),
            priority=args.get("priority", "normal"), target_date=args.get("target_date"),
            parent_goal_id=args.get("parent_goal_id"), source=args.get("source", "manual"), confidence=args.get("confidence", 1.0),
            milestones=args.get("milestones"), next_action=args.get("next_action", ""),
            blockers=args.get("blockers"), source_conversation_id=args.get("source_conversation_id"),
        ))

    def update_goal(self, args: dict[str, Any]) -> str:
        fields = {key: args[key] for key in GoalStore._UPDATABLE if key in args}
        signal_id = args.get("resolves_signal_id")
        return self._mutate_with_signal(
            lambda: self.store.update(
                int(args.get("goal_id", 0)), expected_revision=args.get("expected_revision"),
                resolves_signal_id=signal_id, signal_resolution="goal_updated", **fields,
            ),
            signal_id,
        )

    def list_goals(self, args: dict[str, Any]) -> str:
        statuses = args.get("statuses") or []
        if isinstance(statuses, str):
            statuses = [statuses]
        try:
            goals = self.store.list_all(statuses=statuses, category=args.get("category"), limit=int(args.get("limit", 50)))
            if args.get("query"):
                goals = self.store.search(str(args["query"]), limit=int(args.get("limit", 50)))
            due = self.store.due_soon(within_days=int(args.get("within_days", 7))) if bool(args.get("include_due", False)) else []
            overdue = self.store.overdue() if bool(args.get("include_due", False)) else []
        except ValueError as exc:
            return self._json({"ok": False, "error": str(exc)})
        return self._json({"ok": True, "goals": [goal_public_view(goal) for goal in goals], "due_soon": [goal_public_view(goal) for goal in due], "overdue": [goal_public_view(goal) for goal in overdue]})

    def get_goal_status(self, args: dict[str, Any]) -> str:
        goal_id = int(args.get("goal_id", 0))
        goal = self.store.get(goal_id)
        if goal is None:
            return self._json({"ok": False, "error": "Goal not found."})
        return self._json({
            "ok": True, "goal": goal_public_view(goal), "tree": self.store.tree(goal_id),
            "links": self.store.linked_refs(goal_id),
            "watcher_signals": self.store.list_watcher_signals(
                goal_id, include_acknowledged=bool(args.get("include_resolved_signals", False)), limit=100,
            ),
            "timeline": self.store.list_events(goal_id, limit=int(args.get("timeline_limit", 20))),
        })

    def complete_goal(self, args: dict[str, Any]) -> str:
        signal_id = args.get("resolves_signal_id")
        return self._mutate_with_signal(
            lambda: self.store.complete(
                int(args.get("goal_id", 0)), expected_revision=args.get("expected_revision"),
                resolves_signal_id=signal_id,
            ),
            signal_id,
        )

    def pause_goal(self, args: dict[str, Any]) -> str:
        return self._mutate(lambda: self.store.pause(int(args.get("goal_id", 0)), expected_revision=args.get("expected_revision")))

    def abandon_goal(self, args: dict[str, Any]) -> str:
        return self._mutate(lambda: self.store.abandon(int(args.get("goal_id", 0)), expected_revision=args.get("expected_revision")))

    def decompose_goal(self, args: dict[str, Any]) -> str:
        try:
            children = self.store.decompose(int(args.get("goal_id", 0)), args.get("subgoals") or [])
        except (ValueError, GoalConflictError) as exc:
            return self._json({"ok": False, "error": str(exc)})
        return self._json({"ok": True, "children": [goal_public_view(goal) for goal in children]})

    def link_goal_task(self, args: dict[str, Any]) -> str:
        task_id = str(args.get("task_id") or "")
        if self.task_store is not None and self.task_store.get_task(task_id) is None:
            return self._json({"ok": False, "error": f"Task '{task_id}' was not found."})
        try:
            self.store.link(int(args.get("goal_id", 0)), link_type="task", ref_id=task_id)
        except ValueError as exc:
            return self._json({"ok": False, "error": str(exc)})
        return self.get_goal_status({"goal_id": args.get("goal_id")})

    def link_goal_action(self, args: dict[str, Any]) -> str:
        action_id = int(args.get("action_id", 0))
        if self.action_ledger is not None and not any(
            int(action.get("action_id", 0)) == action_id
            for action in self.action_ledger.list_all()
        ):
            return self._json({"ok": False, "error": f"Action #{action_id} was not found."})
        try:
            self.store.link(int(args.get("goal_id", 0)), link_type="action", ref_id=str(action_id))
        except ValueError as exc:
            return self._json({"ok": False, "error": str(exc)})
        return self.get_goal_status({"goal_id": args.get("goal_id")})

    def link_goal_watcher(self, args: dict[str, Any]) -> str:
        watcher_id = str(args.get("watcher_id") or "")
        db = self._watcher_db()
        if db is None:
            return self._json({"ok": False, "error": "Watcher storage is unavailable."})
        if db.get_monitor(watcher_id) is None:
            return self._json({"ok": False, "error": f"Watcher '{watcher_id}' was not found."})
        try:
            self.store.link(int(args.get("goal_id", 0)), link_type="watcher", ref_id=watcher_id)
        except ValueError as exc:
            return self._json({"ok": False, "error": str(exc)})
        return self.get_goal_status({"goal_id": args.get("goal_id")})

    def unlink_goal_watcher(self, args: dict[str, Any]) -> str:
        try:
            self.store.unlink(
                int(args.get("goal_id", 0)), link_type="watcher", ref_id=str(args.get("watcher_id") or ""),
            )
        except ValueError as exc:
            return self._json({"ok": False, "error": str(exc)})
        return self.get_goal_status({"goal_id": args.get("goal_id")})

    def get_goal_signals(self, args: dict[str, Any]) -> str:
        try:
            goal_id = int(args["goal_id"]) if args.get("goal_id") is not None else None
            signals = self.store.list_watcher_signals(
                goal_id,
                include_acknowledged=bool(args.get("include_acknowledged", False)),
                include_snoozed=bool(args.get("include_snoozed", True)),
                limit=int(args.get("limit", 100)),
            )
        except (TypeError, ValueError) as exc:
            return self._json({"ok": False, "error": str(exc)})
        return self._json({"ok": True, "count": len(signals), "signals": signals})

    def acknowledge_goal_signal(self, args: dict[str, Any]) -> str:
        try:
            signal = self.store.acknowledge_watcher_signal(
                int(args.get("signal_id", 0)), resolution=args.get("resolution", "reviewed"),
                note=args.get("note", ""),
            )
        except ValueError as exc:
            return self._json({"ok": False, "error": str(exc)})
        if signal is None:
            return self._json({"ok": False, "error": "Goal watcher signal not found."})
        return self._json({
            "ok": True,
            "signal": signal,
            "source_watcher_event_acknowledged": self._acknowledge_source_event_if_resolved(signal),
        })

    def snooze_goal_signal(self, args: dict[str, Any]) -> str:
        try:
            signal = self.store.snooze_watcher_signal(
                int(args.get("signal_id", 0)), hours=int(args.get("hours", 24)),
                until=args.get("until"), note=args.get("note", ""),
            )
        except (TypeError, ValueError) as exc:
            return self._json({"ok": False, "error": str(exc)})
        if signal is None:
            return self._json({"ok": False, "error": "Goal watcher signal not found."})
        return self._json({"ok": True, "signal": signal})

    def sync_goal_progress(self, args: dict[str, Any]) -> str:
        return self._mutate(lambda: self.store.recalculate_progress(int(args.get("goal_id", 0))))

    def record_goal_progress(self, args: dict[str, Any]) -> str:
        return self._mutate(lambda: self.store.record_progress(
            int(args.get("goal_id", 0)), note=args.get("note", ""), progress_percent=args.get("progress_percent"),
            expected_revision=args.get("expected_revision"),
        ))

    def delete_goal(self, args: dict[str, Any]) -> str:
        # Guardrails removed: goal deletion is always allowed.
        try:
            deleted = self.store.delete(int(args.get("goal_id", 0)), expected_revision=args.get("expected_revision"))
        except (ValueError, GoalConflictError) as exc:
            return self._json({"ok": False, "error": str(exc)})
        return self._json({"ok": deleted, "action": "deleted" if deleted else "not_found"})
