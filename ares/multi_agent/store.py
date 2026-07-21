"""SQLite persistence for inspectable native multi-agent runs."""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from ares.infra.sqlite_utils import connect_sqlite


def _now() -> str:
    return datetime.now(UTC).isoformat()


class MultiAgentRunStore:
    def __init__(self, data_dir: str | Path) -> None:
        root = Path(data_dir).expanduser()
        root.mkdir(parents=True, exist_ok=True)
        self.db_path = root / "multi_agent.db"
        self.conn = connect_sqlite(self.db_path)
        self._lock = threading.RLock()
        self._create_schema()

    def _create_schema(self) -> None:
        with self._lock, self.conn:
            self.conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS agent_runs (
                    run_id TEXT PRIMARY KEY,
                    root_run_id TEXT NOT NULL,
                    parent_run_id TEXT,
                    session_id TEXT,
                    parent_session_id TEXT,
                    request_id TEXT,
                    task_id TEXT,
                    agent_role TEXT NOT NULL,
                    prompt_summary TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL,
                    dependencies_json TEXT NOT NULL DEFAULT '[]',
                    created_at TEXT NOT NULL,
                    started_at TEXT,
                    completed_at TEXT,
                    duration_seconds REAL NOT NULL DEFAULT 0,
                    error_summary TEXT,
                    result_summary TEXT NOT NULL DEFAULT '',
                    result_content TEXT NOT NULL DEFAULT '',
                    activity TEXT NOT NULL DEFAULT '',
                    current_tool TEXT NOT NULL DEFAULT '',
                    artifacts_json TEXT NOT NULL DEFAULT '[]',
                    iterations INTEGER NOT NULL DEFAULT 0,
                    cancelled INTEGER NOT NULL DEFAULT 0,
                    checkpoint_json TEXT NOT NULL DEFAULT '{}',
                    manifest_json TEXT NOT NULL DEFAULT '{}',
                    metadata_json TEXT NOT NULL DEFAULT '{}'
                );
                CREATE INDEX IF NOT EXISTS idx_agent_runs_root ON agent_runs(root_run_id, created_at);
                CREATE INDEX IF NOT EXISTS idx_agent_runs_created ON agent_runs(created_at);
                """
            )
            columns = {row["name"] for row in self.conn.execute("PRAGMA table_info(agent_runs)")}
            text_columns = {
                "activity": "''",
                "current_tool": "''",
                "parent_session_id": "''",
                "request_id": "''",
                "checkpoint_json": "'{}'",
                "manifest_json": "'{}'",
            }
            for name, default in text_columns.items():
                if name not in columns:
                    self.conn.execute(
                        f"ALTER TABLE agent_runs ADD COLUMN {name} TEXT NOT NULL DEFAULT {default}"
                    )
            self.conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_agent_runs_session "
                "ON agent_runs(session_id, parent_session_id, created_at)"
            )

    def upsert(self, run: dict[str, Any]) -> None:
        created_at = str(run.get("created_at") or _now())
        values = {
            "run_id": str(run["run_id"]),
            "root_run_id": str(run.get("root_run_id") or run["run_id"]),
            "parent_run_id": str(run.get("parent_run_id") or ""),
            "session_id": str(run.get("session_id") or ""),
            "parent_session_id": str(run.get("parent_session_id") or ""),
            "request_id": str(run.get("request_id") or ""),
            "task_id": str(run.get("task_id") or ""),
            "agent_role": str(run.get("agent_role") or run.get("agent") or "root"),
            "prompt_summary": str(run.get("prompt_summary") or "")[:1000],
            "status": str(run.get("status") or "queued"),
            "dependencies_json": json.dumps(list(run.get("dependencies") or []), ensure_ascii=False),
            "created_at": created_at,
            "started_at": run.get("started_at"),
            "completed_at": run.get("completed_at"),
            "duration_seconds": float(run.get("duration_seconds") or 0.0),
            "error_summary": str(run.get("error_summary") or "")[:2000] or None,
            "result_summary": str(run.get("result_summary") or "")[:4000],
            "result_content": str(run.get("result_content") or "")[:100_000],
            "activity": str(run.get("activity") or "")[:2000],
            "current_tool": str(run.get("current_tool") or "")[:500],
            "artifacts_json": json.dumps(list(run.get("artifacts") or []), ensure_ascii=False),
            "iterations": int(run.get("iterations") or 0),
            "cancelled": int(bool(run.get("cancelled"))),
            "checkpoint_json": json.dumps(dict(run.get("checkpoint") or {}), ensure_ascii=False),
            "manifest_json": json.dumps(dict(run.get("manifest") or {}), ensure_ascii=False),
            "metadata_json": json.dumps(dict(run.get("metadata") or {}), ensure_ascii=False),
        }
        with self._lock, self.conn:
            self.conn.execute(
                """
                INSERT INTO agent_runs (
                    run_id, root_run_id, parent_run_id, session_id, parent_session_id, request_id, task_id, agent_role,
                    prompt_summary, status, dependencies_json, created_at, started_at,
                    completed_at, duration_seconds, error_summary, result_summary,
                    result_content, activity, current_tool, artifacts_json, iterations, cancelled,
                    checkpoint_json, manifest_json, metadata_json
                ) VALUES (
                    :run_id, :root_run_id, :parent_run_id, :session_id, :parent_session_id, :request_id, :task_id, :agent_role,
                    :prompt_summary, :status, :dependencies_json, :created_at, :started_at,
                    :completed_at, :duration_seconds, :error_summary, :result_summary,
                    :result_content, :activity, :current_tool, :artifacts_json, :iterations, :cancelled,
                    :checkpoint_json, :manifest_json, :metadata_json
                )
                ON CONFLICT(run_id) DO UPDATE SET
                    status=excluded.status, started_at=COALESCE(excluded.started_at, agent_runs.started_at),
                    completed_at=excluded.completed_at, duration_seconds=excluded.duration_seconds,
                    error_summary=excluded.error_summary, result_summary=excluded.result_summary,
                    result_content=excluded.result_content, activity=excluded.activity,
                    current_tool=excluded.current_tool, artifacts_json=excluded.artifacts_json,
                    iterations=excluded.iterations, cancelled=excluded.cancelled,
                    checkpoint_json=excluded.checkpoint_json, manifest_json=excluded.manifest_json,
                    metadata_json=excluded.metadata_json
                """,
                values,
            )

    def update(self, run_id: str, **changes: Any) -> None:
        current = self.get(run_id, include_children=False)
        if current is None:
            return
        current.update(changes)
        self.upsert(current)

    @staticmethod
    def _row(row: Any) -> dict[str, Any]:
        item = dict(row)
        for source, target, fallback in (
            ("dependencies_json", "dependencies", []),
            ("artifacts_json", "artifacts", []),
            ("checkpoint_json", "checkpoint", {}),
            ("manifest_json", "manifest", {}),
            ("metadata_json", "metadata", {}),
        ):
            try:
                item[target] = json.loads(item.pop(source) or "null") or fallback
            except (json.JSONDecodeError, TypeError):
                item[target] = fallback
        item["cancelled"] = bool(item.get("cancelled"))
        return item

    @staticmethod
    def _authorized_session(item: dict[str, Any], session_id: str | None) -> bool:
        if session_id is None:
            return True
        selected = str(session_id)
        return selected in {
            str(item.get("session_id") or ""),
            str(item.get("parent_session_id") or ""),
        }

    def get(
        self,
        run_id: str,
        *,
        include_children: bool = True,
        session_id: str | None = None,
    ) -> dict[str, Any] | None:
        with self._lock:
            row = self.conn.execute("SELECT * FROM agent_runs WHERE run_id = ?", (run_id,)).fetchone()
            if row is None:
                row = self.conn.execute(
                    "SELECT * FROM agent_runs WHERE root_run_id = ? ORDER BY created_at LIMIT 1", (run_id,)
                ).fetchone()
            if row is None:
                return None
            result = self._row(row)
            if not self._authorized_session(result, session_id):
                return None
            if include_children and result["run_id"] == result["root_run_id"]:
                children = self.conn.execute(
                    "SELECT * FROM agent_runs WHERE root_run_id = ? AND run_id != ? ORDER BY created_at, task_id",
                    (result["root_run_id"], result["root_run_id"]),
                ).fetchall()
                result["children"] = [self._row(child) for child in children]
            elif include_children:
                result["children"] = []
            return result

    def list(
        self,
        *,
        limit: int = 30,
        session_id: str | None = None,
        status: str | None = None,
    ) -> list[dict[str, Any]]:
        query = "SELECT * FROM agent_runs WHERE run_id = root_run_id"
        params: list[Any] = []
        if session_id is not None:
            query += " AND session_id = ?"
            params.append(str(session_id))
        if status:
            query += " AND status = ?"
            params.append(str(status))
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(max(1, min(int(limit), 200)))
        with self._lock:
            roots = [self._row(row) for row in self.conn.execute(query, params).fetchall()]
        for root in roots:
            complete = self.get(str(root["run_id"]))
            root["children"] = (complete or {}).get("children", [])
        return roots

    def latest(self, *, session_id: str, include_children: bool = True) -> dict[str, Any] | None:
        with self._lock:
            row = self.conn.execute(
                "SELECT run_id FROM agent_runs WHERE run_id = root_run_id AND session_id = ? "
                "ORDER BY created_at DESC LIMIT 1",
                (str(session_id),),
            ).fetchone()
        if row is None:
            return None
        return self.get(
            str(row["run_id"]), include_children=include_children, session_id=str(session_id)
        )

    def mark_cancelled(self, root_run_id: str) -> None:
        completed = _now()
        with self._lock, self.conn:
            self.conn.execute(
                "UPDATE agent_runs SET status='cancelled', cancelled=1, completed_at=COALESCE(completed_at, ?) "
                "WHERE root_run_id=? AND status IN ('queued','running')",
                (completed, root_run_id),
            )

    def mark_interrupted(self) -> tuple[str, ...]:
        """Close orphaned queued/running records left by a process exit.

        A newly constructed runtime cannot own tasks from the previous event
        loop.  Persisting an explicit state avoids showing them as live forever
        and leaves their successful sibling checkpoints available for resume.
        """
        completed = _now()
        with self._lock, self.conn:
            roots = tuple(
                str(row["root_run_id"])
                for row in self.conn.execute(
                    "SELECT DISTINCT root_run_id FROM agent_runs "
                    "WHERE status IN ('queued','running')"
                ).fetchall()
            )
            if roots:
                self.conn.execute(
                    "UPDATE agent_runs SET status='interrupted', completed_at=COALESCE(completed_at, ?), "
                    "error_summary=COALESCE(error_summary, 'Ares stopped before this run completed') "
                    "WHERE status IN ('queued','running')",
                    (completed,),
                )
        return roots

    def cleanup(self, retention_days: int) -> int:
        cutoff = (datetime.now(UTC) - timedelta(days=max(1, retention_days))).isoformat()
        with self._lock, self.conn:
            cursor = self.conn.execute("DELETE FROM agent_runs WHERE created_at < ?", (cutoff,))
            return int(cursor.rowcount or 0)

    def close(self) -> None:
        with self._lock:
            self.conn.close()
