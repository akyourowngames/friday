from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from .config import Settings


JsonObject = dict[str, Any]

TASK_STATUSES = {"pending", "in_progress", "blocked", "done", "archived"}
PROJECT_STATUSES = TASK_STATUSES
PRIORITIES = {"low", "normal", "high", "urgent"}


def project_db_path(settings: Settings, workspace_root: Path | None = None) -> Path:
    raw = str(settings.project_db or "storage/projects.sqlite3").strip() or "storage/projects.sqlite3"
    path = Path(raw)
    if not path.is_absolute():
        path = (workspace_root or Path.cwd()) / path
    return path.resolve()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def normalize_status(value: object, default: str = "pending") -> str:
    status = str(value or default).strip().lower().replace("-", "_").replace(" ", "_")
    if status == "complete":
        status = "done"
    if status not in TASK_STATUSES:
        raise ValueError(f"status must be one of: {', '.join(sorted(TASK_STATUSES))}")
    return status


def normalize_priority(value: object, default: str = "normal") -> str:
    priority = str(value or default).strip().lower()
    if priority not in PRIORITIES:
        raise ValueError(f"priority must be one of: {', '.join(sorted(PRIORITIES))}")
    return priority


def normalize_tags(value: object) -> list[str]:
    if value is None or value == "":
        return []
    raw_tags: list[object]
    if isinstance(value, list):
        raw_tags = value
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        if text.startswith("["):
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError:
                parsed = None
            raw_tags = parsed if isinstance(parsed, list) else [text]
        else:
            raw_tags = text.split(",")
    else:
        raw_tags = [value]
    tags: list[str] = []
    seen: set[str] = set()
    for item in raw_tags:
        tag = str(item).strip()
        if not tag:
            continue
        key = tag.lower()
        if key in seen:
            continue
        seen.add(key)
        tags.append(tag)
    return tags


def _object_text(value: object, keys: tuple[str, ...]) -> str:
    if isinstance(value, dict):
        for key in keys:
            nested = value.get(key)
            if nested is not None and not isinstance(nested, (dict, list)):
                return str(nested).strip()
        return ""
    if isinstance(value, list):
        return ""
    return str(value or "").strip()


def normalize_due(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    lowered = text.lower()
    today = date.today()
    named_dates = {
        "today": today,
        "tonight": today,
        "tomorrow": today + timedelta(days=1),
        "next week": today + timedelta(days=7),
    }
    if lowered in named_dates:
        return named_dates[lowered].isoformat()
    try:
        import dateparser

        parsed = dateparser.parse(
            text,
            settings={
                "PREFER_DATES_FROM": "future",
                "RETURN_AS_TIMEZONE_AWARE": True,
            },
        )
        if parsed is None:
            from dateparser.search import search_dates

            matches = search_dates(
                text,
                settings={
                    "PREFER_DATES_FROM": "future",
                    "RETURN_AS_TIMEZONE_AWARE": True,
                },
            )
            parsed = matches[-1][1] if matches else None
        if parsed is not None:
            return parsed.isoformat(timespec="minutes")
    except ImportError:
        pass
    try:
        return datetime.fromisoformat(text).isoformat(timespec="seconds")
    except ValueError:
        pass
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError:
        return text


class ProjectStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _ensure_schema(self) -> None:
        with self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS projects (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    description TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'in_progress',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    archived_at TEXT
                );

                CREATE UNIQUE INDEX IF NOT EXISTS idx_projects_name_unique
                    ON projects(lower(name));

                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    description TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'pending',
                    priority TEXT NOT NULL DEFAULT 'normal',
                    due_at TEXT NOT NULL DEFAULT '',
                    tags_json TEXT NOT NULL DEFAULT '[]',
                    recurrence TEXT NOT NULL DEFAULT '',
                    parent_task_id TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    completed_at TEXT,
                    FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE,
                    FOREIGN KEY(parent_task_id) REFERENCES tasks(id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_tasks_project_status
                    ON tasks(project_id, status, updated_at);
                CREATE INDEX IF NOT EXISTS idx_tasks_parent
                    ON tasks(parent_task_id);

                CREATE TABLE IF NOT EXISTS task_notes (
                    id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    note TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(task_id) REFERENCES tasks(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS task_events (
                    id TEXT PRIMARY KEY,
                    project_id TEXT,
                    task_id TEXT,
                    event_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE SET NULL,
                    FOREIGN KEY(task_id) REFERENCES tasks(id) ON DELETE SET NULL
                );
                """
            )
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(tasks)").fetchall()}
            if "recurrence" not in columns:
                conn.execute("ALTER TABLE tasks ADD COLUMN recurrence TEXT NOT NULL DEFAULT ''")

    def create_project(self, name: str, description: str = "", status: str = "in_progress") -> JsonObject:
        project_name = str(name or "").strip()
        if not project_name:
            raise ValueError("name is required")
        clean_status = normalize_status(status, default="in_progress")
        existing = self.find_project(project_name, allow_archived=True)
        if existing:
            if existing["status"] == "archived":
                self.update_project(existing["id"], status=clean_status, archived_at="")
                existing = self.get_project(existing["id"])
            return {"project": existing, "created": False}
        timestamp = now_iso()
        project_id = new_id("proj")
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO projects(id, name, description, status, created_at, updated_at, archived_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (project_id, project_name, str(description or "").strip(), clean_status, timestamp, timestamp, None),
            )
            self._event(conn, project_id, None, "project_create", {"name": project_name, "status": clean_status})
        return {"project": self.get_project(project_id), "created": True}

    def list_projects(self, status: str = "", include_archived: bool = False, limit: int = 20) -> list[JsonObject]:
        clauses: list[str] = []
        values: list[object] = []
        if status:
            clauses.append("p.status = ?")
            values.append(normalize_status(status))
        elif not include_archived:
            clauses.append("p.status != 'archived'")
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        values.append(max(1, min(100, int(limit or 20))))
        with self.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT p.*,
                    SUM(CASE WHEN t.status != 'done' AND t.status != 'archived' THEN 1 ELSE 0 END) AS open_tasks,
                    SUM(CASE WHEN t.status = 'done' THEN 1 ELSE 0 END) AS done_tasks
                FROM projects p
                LEFT JOIN tasks t ON t.project_id = p.id
                {where}
                GROUP BY p.id
                ORDER BY p.updated_at DESC
                LIMIT ?
                """,
                values,
            ).fetchall()
        return [self._project(row) for row in rows]

    def update_project(self, project: str, **changes: object) -> JsonObject:
        current = self.resolve_project(project)
        allowed = {"name", "description", "status", "archived_at"}
        assignments: list[str] = []
        values: list[object] = []
        payload: JsonObject = {}
        for key, value in changes.items():
            if key not in allowed or value is None:
                continue
            if key == "status":
                value = normalize_status(value, default=current["status"])
            if key == "name" and not str(value or "").strip():
                continue
            assignments.append(f"{key} = ?")
            values.append(str(value).strip() if key != "archived_at" else (str(value).strip() or None))
            payload[key] = value
        if not assignments:
            return current
        timestamp = now_iso()
        assignments.append("updated_at = ?")
        values.append(timestamp)
        values.append(current["id"])
        with self.connect() as conn:
            conn.execute(f"UPDATE projects SET {', '.join(assignments)} WHERE id = ?", values)
            self._event(conn, current["id"], None, "project_update", payload)
        return self.get_project(current["id"])

    def archive_project(self, project: str) -> JsonObject:
        current = self.resolve_project(project)
        timestamp = now_iso()
        with self.connect() as conn:
            conn.execute(
                "UPDATE projects SET status = 'archived', archived_at = ?, updated_at = ? WHERE id = ?",
                (timestamp, timestamp, current["id"]),
            )
            self._event(conn, current["id"], None, "project_archive", {})
        return self.get_project(current["id"])

    def create_task(
        self,
        project: str,
        title: str,
        description: str = "",
        status: str = "pending",
        priority: str = "normal",
        due: object = "",
        tags: object = None,
        parent_task: str = "",
        recurrence: str = "",
    ) -> JsonObject:
        project_row = self.ensure_project(project)
        task_title = str(title or "").strip()
        if not task_title:
            raise ValueError("title is required")
        parent_id = ""
        if parent_task:
            parent_id = self.resolve_task(str(parent_task), project=project_row["id"])["id"]
        existing = self.find_open_task(project_row["id"], task_title, parent_id or None)
        if existing:
            return {"task": existing, "project": project_row, "created": False}
        task_id = new_id("task")
        timestamp = now_iso()
        clean_status = normalize_status(status)
        completed_at = timestamp if clean_status == "done" else None
        clean_priority = normalize_priority(priority)
        tags_json = json.dumps(normalize_tags(tags), ensure_ascii=False)
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO tasks(
                    id, project_id, title, description, status, priority, due_at,
                    tags_json, recurrence, parent_task_id, created_at, updated_at, completed_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task_id,
                    project_row["id"],
                    task_title,
                    str(description or "").strip(),
                    clean_status,
                    clean_priority,
                    normalize_due(due),
                    tags_json,
                    str(recurrence or "").strip(),
                    parent_id or None,
                    timestamp,
                    timestamp,
                    completed_at,
                ),
            )
            conn.execute("UPDATE projects SET updated_at = ? WHERE id = ?", (timestamp, project_row["id"]))
            self._event(conn, project_row["id"], task_id, "task_create", {"title": task_title, "status": clean_status})
        return {"task": self.get_task(task_id), "project": self.get_project(project_row["id"]), "created": True}

    def create_tasks(self, project: str, tasks: list[JsonObject]) -> JsonObject:
        project_row = self.ensure_project(project)
        if not tasks:
            raise ValueError("tasks must contain at least one task")
        created_tasks: list[JsonObject] = []
        existing_tasks: list[JsonObject] = []
        for item in tasks:
            title = _object_text(item.get("title"), ("title", "name", "text", "value"))
            if not title:
                raise ValueError("every task requires a title")
            result = self.create_task(
                project=project_row["id"],
                title=title,
                description=_object_text(item.get("description"), ("description", "text", "value")),
                status=_object_text(item.get("status"), ("status", "value")) or "pending",
                priority=_object_text(item.get("priority"), ("priority", "value")) or "normal",
                due=item.get("due", ""),
                tags=item.get("tags", []),
                recurrence=_object_text(item.get("recurrence"), ("recurrence", "value", "text")),
            )
            if result["created"]:
                created_tasks.append(result["task"])
            else:
                existing_tasks.append(result["task"])
        return {
            "project": self.get_project(project_row["id"]),
            "created_tasks": created_tasks,
            "existing_tasks": existing_tasks,
            "created_count": len(created_tasks),
            "existing_count": len(existing_tasks),
        }

    def list_tasks(
        self,
        project: str = "",
        status: str = "",
        include_archived: bool = False,
        include_done: bool = True,
        limit: int = 30,
    ) -> list[JsonObject]:
        clauses: list[str] = []
        values: list[object] = []
        if project:
            clauses.append("t.project_id = ?")
            values.append(self.resolve_project(project)["id"])
        if status:
            clauses.append("t.status = ?")
            values.append(normalize_status(status))
        else:
            if not include_archived:
                clauses.append("t.status != 'archived'")
            if not include_done:
                clauses.append("t.status != 'done'")
        values.append(max(1, min(200, int(limit or 30))))
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT t.*, p.name AS project_name
                FROM tasks t
                JOIN projects p ON p.id = t.project_id
                {where}
                ORDER BY
                    CASE t.priority WHEN 'urgent' THEN 0 WHEN 'high' THEN 1 WHEN 'normal' THEN 2 ELSE 3 END,
                    CASE WHEN t.due_at = '' THEN 1 ELSE 0 END,
                    t.due_at ASC,
                    t.updated_at DESC
                LIMIT ?
                """,
                values,
            ).fetchall()
        return [self._task(row) for row in rows]

    def update_task(self, task: str, project: str = "", **changes: object) -> JsonObject:
        current = self.resolve_task(task, project=project)
        allowed = {"title", "description", "status", "priority", "due_at", "tags_json", "recurrence"}
        assignments: list[str] = []
        values: list[object] = []
        payload: JsonObject = {}
        clean_status = str(current["status"])
        for key, value in changes.items():
            if key not in allowed or value is None:
                continue
            if key == "status":
                value = normalize_status(value, default=current["status"])
                clean_status = str(value)
            elif key == "priority":
                value = normalize_priority(value, default=current["priority"])
            elif key == "due_at":
                value = normalize_due(value)
            elif key == "tags_json":
                value = json.dumps(normalize_tags(value), ensure_ascii=False)
            elif key == "recurrence":
                value = str(value or "").strip()
            elif key == "title" and not str(value or "").strip():
                continue
            assignments.append(f"{key} = ?")
            values.append(str(value).strip())
            payload[key] = value
        if "status" in payload:
            assignments.append("completed_at = ?")
            values.append(now_iso() if clean_status == "done" else None)
        if not assignments:
            return current
        timestamp = now_iso()
        assignments.append("updated_at = ?")
        values.append(timestamp)
        values.append(current["id"])
        with self.connect() as conn:
            conn.execute(f"UPDATE tasks SET {', '.join(assignments)} WHERE id = ?", values)
            conn.execute("UPDATE projects SET updated_at = ? WHERE id = ?", (timestamp, current["project_id"]))
            self._event(conn, current["project_id"], current["id"], "task_update", payload)
        return self.get_task(current["id"])

    def bulk_update_tasks(
        self,
        project: str = "",
        task_refs: list[str] | None = None,
        match_status: str = "",
        status: object = None,
        priority: object = None,
        due: object = None,
        tags: object = None,
        recurrence: object = None,
        include_done: bool = False,
    ) -> JsonObject:
        refs = [str(ref).strip() for ref in (task_refs or []) if str(ref).strip()]
        if not project and not refs:
            raise ValueError("project or task_ids is required for a bulk update")
        changes: JsonObject = {}
        if status not in (None, ""):
            changes["status"] = status
        if priority not in (None, ""):
            changes["priority"] = priority
        if due not in (None, ""):
            changes["due_at"] = due
        if tags not in (None, ""):
            changes["tags_json"] = tags
        if recurrence not in (None, ""):
            changes["recurrence"] = recurrence
        if not changes:
            raise ValueError("at least one task field must be provided for a bulk update")

        selected: list[JsonObject] = []
        if refs:
            for ref in refs:
                task = self.resolve_task(ref, project=project)
                if task["id"] not in {item["id"] for item in selected}:
                    selected.append(task)
        else:
            selected = self.list_tasks(
                project=project,
                status=match_status,
                include_archived=False,
                include_done=include_done or match_status == "done",
                limit=200,
            )
        updated = [self.update_task(task["id"], **changes) for task in selected]
        project_ids = sorted({task["project_id"] for task in updated})
        return {
            "updated_tasks": updated,
            "updated_count": len(updated),
            "projects": [self.get_project(project_id) for project_id in project_ids],
            "changes": changes,
        }

    def set_task_status(self, task: str, status: str, project: str = "") -> JsonObject:
        current = self.resolve_task(task, project=project)
        clean_status = normalize_status(status, default=current["status"])
        if current["status"] == clean_status:
            return current
        return self.update_task(current["id"], status=clean_status)

    def set_tasks_status(
        self,
        project: str = "",
        status: str = "done",
        include_archived: bool = False,
    ) -> JsonObject:
        clean_status = normalize_status(status)
        project_filter = str(project or "").strip()
        project_ids: list[str] = []
        if project_filter:
            project_ids = [self.resolve_project(project_filter)["id"]]
        else:
            project_ids = [item["id"] for item in self.list_projects(include_archived=False, limit=100)]
        if not project_ids:
            return {"updated_tasks": [], "updated_count": 0, "matched_count": 0, "projects": []}

        task_clauses = ["t.project_id IN (" + ",".join("?" for _ in project_ids) + ")", "t.status != ?"]
        values: list[object] = [*project_ids, clean_status]
        if not include_archived:
            task_clauses.append("t.status != 'archived'")
        if clean_status == "done":
            task_clauses.append("t.status != 'done'")
        where = " AND ".join(task_clauses)
        timestamp = now_iso()
        with self.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT t.*, p.name AS project_name
                FROM tasks t
                JOIN projects p ON p.id = t.project_id
                WHERE {where}
                ORDER BY t.updated_at DESC
                """,
                values,
            ).fetchall()
            before_tasks = [self._task(row) for row in rows]
            task_ids = [task["id"] for task in before_tasks]
            if task_ids:
                placeholders = ",".join("?" for _ in task_ids)
                completed_at = timestamp if clean_status == "done" else None
                conn.execute(
                    f"""
                    UPDATE tasks
                    SET status = ?, updated_at = ?, completed_at = ?
                    WHERE id IN ({placeholders})
                    """,
                    [clean_status, timestamp, completed_at, *task_ids],
                )
                touched_projects = sorted({task["project_id"] for task in before_tasks})
                project_placeholders = ",".join("?" for _ in touched_projects)
                conn.execute(
                    f"UPDATE projects SET updated_at = ? WHERE id IN ({project_placeholders})",
                    [timestamp, *touched_projects],
                )
                for task in before_tasks:
                    self._event(
                        conn,
                        task["project_id"],
                        task["id"],
                        "task_update",
                        {"status": clean_status, "bulk": True},
                    )
        updated_tasks = [self.get_task(task_id) for task_id in task_ids]
        projects = [self.get_project(project_id) for project_id in project_ids]
        return {
            "updated_tasks": updated_tasks,
            "updated_count": len(updated_tasks),
            "matched_count": len(before_tasks),
            "projects": projects,
            "status": clean_status,
        }

    def delete_task(self, task: str, project: str = "") -> JsonObject:
        current = self.resolve_task(task, project=project)
        timestamp = now_iso()
        with self.connect() as conn:
            conn.execute("DELETE FROM tasks WHERE id = ?", (current["id"],))
            conn.execute("UPDATE projects SET updated_at = ? WHERE id = ?", (timestamp, current["project_id"]))
            self._event(conn, current["project_id"], None, "task_delete", {"task": current})
        return current

    def delete_tasks(
        self,
        project: str = "",
        task_refs: list[str] | None = None,
        delete_all: bool = False,
        match_status: str = "",
    ) -> JsonObject:
        refs = [str(ref).strip() for ref in (task_refs or []) if str(ref).strip()]
        if not refs and not (delete_all and project):
            raise ValueError("task_ids are required unless all=true and project is provided")
        selected: list[JsonObject] = []
        if refs:
            for ref in refs:
                task = self.resolve_task(ref, project=project)
                if task["id"] not in {item["id"] for item in selected}:
                    selected.append(task)
        else:
            selected = self.list_tasks(
                project=project,
                status=match_status,
                include_archived=True,
                include_done=True,
                limit=200,
            )
        deleted = [self.delete_task(task["id"]) for task in selected]
        return {"deleted_tasks": deleted, "deleted_count": len(deleted)}

    def add_subtask(
        self,
        parent_task: str,
        title: str,
        project: str = "",
        description: str = "",
        priority: str = "normal",
        due: object = "",
        tags: object = None,
        recurrence: str = "",
    ) -> JsonObject:
        parent = self.resolve_task(parent_task, project=project)
        return self.create_task(
            parent["project_id"],
            title,
            description=description,
            priority=priority,
            due=due,
            tags=tags,
            parent_task=parent["id"],
            recurrence=recurrence,
        )

    def add_note(self, task: str, note: str, project: str = "") -> JsonObject:
        clean_note = str(note or "").strip()
        if not clean_note:
            raise ValueError("note is required")
        current = self.resolve_task(task, project=project)
        note_id = new_id("note")
        timestamp = now_iso()
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO task_notes(id, task_id, note, created_at) VALUES (?, ?, ?, ?)",
                (note_id, current["id"], clean_note, timestamp),
            )
            conn.execute("UPDATE tasks SET updated_at = ? WHERE id = ?", (timestamp, current["id"]))
            conn.execute("UPDATE projects SET updated_at = ? WHERE id = ?", (timestamp, current["project_id"]))
            self._event(conn, current["project_id"], current["id"], "note_add", {"note": clean_note})
        return {"note": self.get_note(note_id), "task": self.get_task(current["id"])}

    def activity(self, project: str = "", task: str = "", limit: int = 20) -> list[JsonObject]:
        clauses: list[str] = []
        values: list[object] = []
        if project:
            clauses.append("project_id = ?")
            values.append(self.resolve_project(project)["id"])
        if task:
            clauses.append("task_id = ?")
            values.append(self.resolve_task(task, project=project)["id"])
        values.append(max(1, min(100, int(limit or 20))))
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT *
                FROM task_events
                {where}
                ORDER BY created_at DESC
                LIMIT ?
                """,
                values,
            ).fetchall()
        return [self._event_row(row) for row in rows]

    def summary(self, project: str = "", limit_projects: int = 5, limit_tasks: int = 12) -> JsonObject:
        projects = [self.get_project(self.resolve_project(project)["id"])] if project else self.list_projects(limit=limit_projects)
        project_ids = [item["id"] for item in projects]
        tasks: list[JsonObject] = []
        if project_ids:
            placeholders = ",".join("?" for _ in project_ids)
            with self.connect() as conn:
                rows = conn.execute(
                    f"""
                    SELECT t.*, p.name AS project_name
                    FROM tasks t
                    JOIN projects p ON p.id = t.project_id
                    WHERE t.project_id IN ({placeholders})
                        AND t.status != 'done'
                        AND t.status != 'archived'
                    ORDER BY
                        CASE t.priority WHEN 'urgent' THEN 0 WHEN 'high' THEN 1 WHEN 'normal' THEN 2 ELSE 3 END,
                        CASE WHEN t.due_at = '' THEN 1 ELSE 0 END,
                        t.due_at ASC,
                        t.updated_at DESC
                    LIMIT ?
                    """,
                    [*project_ids, max(1, min(50, int(limit_tasks or 12)))],
                ).fetchall()
            tasks = [self._task(row) for row in rows]
        return {"projects": projects, "open_tasks": tasks, "db_path": str(self.path)}

    def ensure_project(self, project: str) -> JsonObject:
        text = str(project or "").strip()
        if not text:
            raise ValueError("project is required")
        existing = self.find_project(text)
        if existing:
            return existing
        return self.create_project(text)["project"]

    def resolve_project(self, project: str) -> JsonObject:
        found = self.find_project(project, allow_archived=True)
        if found:
            return found
        raise ValueError(f"Project not found: {project}")

    def find_project(self, project: str, allow_archived: bool = False) -> JsonObject | None:
        text = str(project or "").strip()
        if not text:
            return None
        clauses = ["(id = ? OR lower(name) = lower(?))"]
        values: list[object] = [text, text]
        if not allow_archived:
            clauses.append("status != 'archived'")
        with self.connect() as conn:
            row = conn.execute(
                f"SELECT * FROM projects WHERE {' AND '.join(clauses)} ORDER BY updated_at DESC LIMIT 1",
                values,
            ).fetchone()
            if row:
                return self._project(row)
            fallback_clauses = [] if allow_archived else ["status != 'archived'"]
            fallback_where = f"WHERE {' AND '.join(fallback_clauses)}" if fallback_clauses else ""
            candidates = conn.execute(
                f"SELECT * FROM projects {fallback_where} ORDER BY updated_at DESC LIMIT 100"
            ).fetchall()
        matched = _unique_project_match(text, candidates)
        return self._project(matched) if matched else None

    def get_project(self, project_id: str) -> JsonObject:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT p.*,
                    SUM(CASE WHEN t.status != 'done' AND t.status != 'archived' THEN 1 ELSE 0 END) AS open_tasks,
                    SUM(CASE WHEN t.status = 'done' THEN 1 ELSE 0 END) AS done_tasks
                FROM projects p
                LEFT JOIN tasks t ON t.project_id = p.id
                WHERE p.id = ?
                GROUP BY p.id
                """,
                (project_id,),
            ).fetchone()
        if not row:
            raise ValueError(f"Project not found: {project_id}")
        return self._project(row)

    def find_open_task(self, project_id: str, title: str, parent_task_id: str | None = None) -> JsonObject | None:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT t.*, p.name AS project_name
                FROM tasks t
                JOIN projects p ON p.id = t.project_id
                WHERE t.project_id = ?
                    AND lower(t.title) = lower(?)
                    AND t.status != 'done'
                    AND t.status != 'archived'
                    AND COALESCE(t.parent_task_id, '') = COALESCE(?, '')
                ORDER BY t.updated_at DESC
                LIMIT 1
                """,
                (project_id, title, parent_task_id or ""),
            ).fetchone()
        return self._task(row) if row else None

    def resolve_task(self, task: str, project: str = "") -> JsonObject:
        text = str(task or "").strip()
        if not text:
            raise ValueError("task_id or task title is required")
        project_id = ""
        if project:
            project_id = self.resolve_project(project)["id"]
        clauses = ["(t.id = ? OR lower(t.title) = lower(?))"]
        values: list[object] = [text, text]
        if project_id:
            clauses.append("t.project_id = ?")
            values.append(project_id)
        with self.connect() as conn:
            exact_rows = conn.execute(
                f"""
                SELECT t.*, p.name AS project_name
                FROM tasks t
                JOIN projects p ON p.id = t.project_id
                WHERE {' AND '.join(clauses)}
                ORDER BY t.updated_at DESC
                LIMIT 3
                """,
                values,
            ).fetchall()
            rows = exact_rows
            if not rows:
                like_clauses = ["lower(t.title) LIKE lower(?)"]
                like_values: list[object] = [f"%{text}%"]
                if project_id:
                    like_clauses.append("t.project_id = ?")
                    like_values.append(project_id)
                rows = conn.execute(
                    f"""
                    SELECT t.*, p.name AS project_name
                    FROM tasks t
                    JOIN projects p ON p.id = t.project_id
                    WHERE {' AND '.join(like_clauses)}
                    ORDER BY
                        CASE WHEN t.status = 'done' THEN 1 ELSE 0 END,
                        t.updated_at DESC
                    LIMIT 3
                    """,
                    like_values,
                ).fetchall()
        if not rows:
            raise ValueError(f"Task not found: {task}")
        if len(rows) > 1 and not text.startswith("task_"):
            choices = ", ".join(f"{row['id']} ({row['title']})" for row in rows)
            raise ValueError(f"Multiple tasks matched. Use task_id. Matches: {choices}")
        return self._task(rows[0])

    def get_task(self, task_id: str) -> JsonObject:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT t.*, p.name AS project_name
                FROM tasks t
                JOIN projects p ON p.id = t.project_id
                WHERE t.id = ?
                """,
                (task_id,),
            ).fetchone()
        if not row:
            raise ValueError(f"Task not found: {task_id}")
        return self._task(row)

    def get_note(self, note_id: str) -> JsonObject:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM task_notes WHERE id = ?", (note_id,)).fetchone()
        if not row:
            raise ValueError(f"Note not found: {note_id}")
        return self._note(row)

    def _event(
        self,
        conn: sqlite3.Connection,
        project_id: str | None,
        task_id: str | None,
        event_type: str,
        payload: JsonObject,
    ) -> None:
        conn.execute(
            """
            INSERT INTO task_events(id, project_id, task_id, event_type, payload_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (new_id("evt"), project_id, task_id, event_type, json.dumps(payload, ensure_ascii=False), now_iso()),
        )

    def _project(self, row: sqlite3.Row) -> JsonObject:
        return {
            "id": row["id"],
            "name": row["name"],
            "description": row["description"],
            "status": row["status"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "archived_at": row["archived_at"] or "",
            "open_tasks": int(row["open_tasks"] or 0) if "open_tasks" in row.keys() else 0,
            "done_tasks": int(row["done_tasks"] or 0) if "done_tasks" in row.keys() else 0,
        }

    def _task(self, row: sqlite3.Row) -> JsonObject:
        try:
            tags = json.loads(row["tags_json"] or "[]")
        except json.JSONDecodeError:
            tags = []
        return {
            "id": row["id"],
            "project_id": row["project_id"],
            "project_name": row["project_name"] if "project_name" in row.keys() else "",
            "title": row["title"],
            "description": row["description"],
            "status": row["status"],
            "priority": row["priority"],
            "due_at": row["due_at"] or "",
            "tags": tags if isinstance(tags, list) else [],
            "recurrence": row["recurrence"] if "recurrence" in row.keys() else "",
            "parent_task_id": row["parent_task_id"] or "",
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "completed_at": row["completed_at"] or "",
        }

    def _note(self, row: sqlite3.Row) -> JsonObject:
        return {"id": row["id"], "task_id": row["task_id"], "note": row["note"], "created_at": row["created_at"]}

    def _event_row(self, row: sqlite3.Row) -> JsonObject:
        try:
            payload = json.loads(row["payload_json"] or "{}")
        except json.JSONDecodeError:
            payload = {}
        return {
            "id": row["id"],
            "project_id": row["project_id"] or "",
            "task_id": row["task_id"] or "",
            "event_type": row["event_type"],
            "payload": payload if isinstance(payload, dict) else {},
            "created_at": row["created_at"],
        }


def _entity_words(value: object) -> list[str]:
    cleaned = "".join(char.lower() if char.isalnum() else " " for char in str(value or ""))
    return [word for word in cleaned.split() if word]


def _project_match_score(query: str, name: str) -> float:
    query_words = _entity_words(query)
    name_words = _entity_words(name)
    if not query_words or not name_words:
        return 0.0
    query_text = " ".join(query_words)
    name_text = " ".join(name_words)
    score = SequenceMatcher(None, query_text, name_text).ratio()
    query_set = set(query_words)
    name_set = set(name_words)
    if name_set.issubset(query_set) or query_set.issubset(name_set):
        score = max(score, 0.86)
    return score


def _unique_project_match(query: str, rows: list[sqlite3.Row]) -> sqlite3.Row | None:
    ranked = sorted(
        ((_project_match_score(query, str(row["name"])), row) for row in rows),
        key=lambda item: item[0],
        reverse=True,
    )
    if not ranked or ranked[0][0] < 0.76:
        return None
    if len(ranked) > 1 and ranked[0][0] - ranked[1][0] < 0.08:
        return None
    return ranked[0][1]
