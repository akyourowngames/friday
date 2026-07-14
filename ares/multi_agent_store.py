"""SQLite persistence for inspectable native multi-agent runs."""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from ares.sqlite_utils import connect_sqlite


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
                    artifacts_json TEXT NOT NULL DEFAULT '[]',
                    iterations INTEGER NOT NULL DEFAULT 0,
                    cancelled INTEGER NOT NULL DEFAULT 0,
                    metadata_json TEXT NOT NULL DEFAULT '{}'
                );
                CREATE INDEX IF NOT EXISTS idx_agent_runs_root ON agent_runs(root_run_id, created_at);
                CREATE INDEX IF NOT EXISTS idx_agent_runs_created ON agent_runs(created_at);
                """
            )

    def upsert(self, run: dict[str, Any]) -> None:
        created_at = str(run.get("created_at") or _now())
        values = {
            "run_id": str(run["run_id"]),
            "root_run_id": str(run.get("root_run_id") or run["run_id"]),
            "parent_run_id": str(run.get("parent_run_id") or ""),
            "session_id": str(run.get("session_id") or ""),
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
            "artifacts_json": json.dumps(list(run.get("artifacts") or []), ensure_ascii=False),
            "iterations": int(run.get("iterations") or 0),
            "cancelled": int(bool(run.get("cancelled"))),
            "metadata_json": json.dumps(dict(run.get("metadata") or {}), ensure_ascii=False),
        }
        with self._lock, self.conn:
            self.conn.execute(
                """
                INSERT INTO agent_runs (
                    run_id, root_run_id, parent_run_id, session_id, task_id, agent_role,
                    prompt_summary, status, dependencies_json, created_at, started_at,
                    completed_at, duration_seconds, error_summary, result_summary,
                    result_content, artifacts_json, iterations, cancelled, metadata_json
                ) VALUES (
                    :run_id, :root_run_id, :parent_run_id, :session_id, :task_id, :agent_role,
                    :prompt_summary, :status, :dependencies_json, :created_at, :started_at,
                    :completed_at, :duration_seconds, :error_summary, :result_summary,
                    :result_content, :artifacts_json, :iterations, :cancelled, :metadata_json
                )
                ON CONFLICT(run_id) DO UPDATE SET
                    status=excluded.status, started_at=COALESCE(excluded.started_at, agent_runs.started_at),
                    completed_at=excluded.completed_at, duration_seconds=excluded.duration_seconds,
                    error_summary=excluded.error_summary, result_summary=excluded.result_summary,
                    result_content=excluded.result_content, artifacts_json=excluded.artifacts_json,
                    iterations=excluded.iterations, cancelled=excluded.cancelled,
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
            ("metadata_json", "metadata", {}),
        ):
            try:
                item[target] = json.loads(item.pop(source) or "null") or fallback
            except (json.JSONDecodeError, TypeError):
                item[target] = fallback
        item["cancelled"] = bool(item.get("cancelled"))
        return item

    def get(self, run_id: str, *, include_children: bool = True) -> dict[str, Any] | None:
        with self._lock:
            row = self.conn.execute("SELECT * FROM agent_runs WHERE run_id = ?", (run_id,)).fetchone()
            if row is None:
                row = self.conn.execute(
                    "SELECT * FROM agent_runs WHERE root_run_id = ? ORDER BY created_at LIMIT 1", (run_id,)
                ).fetchone()
            if row is None:
                return None
            result = self._row(row)
            if include_children and result["run_id"] == result["root_run_id"]:
                children = self.conn.execute(
                    "SELECT * FROM agent_runs WHERE root_run_id = ? AND run_id != ? ORDER BY created_at, task_id",
                    (result["root_run_id"], result["root_run_id"]),
                ).fetchall()
                result["children"] = [self._row(child) for child in children]
            elif include_children:
                result["children"] = []
            return result

    def list(self, *, limit: int = 30, session_id: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM agent_runs WHERE run_id = root_run_id"
        params: list[Any] = []
        if session_id is not None:
            query += " AND session_id = ?"
            params.append(str(session_id))
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(max(1, min(int(limit), 200)))
        with self._lock:
            roots = [self._row(row) for row in self.conn.execute(query, params).fetchall()]
        for root in roots:
            complete = self.get(str(root["run_id"]))
            root["children"] = (complete or {}).get("children", [])
        return roots

    def mark_cancelled(self, root_run_id: str) -> None:
        completed = _now()
        with self._lock, self.conn:
            self.conn.execute(
                "UPDATE agent_runs SET status='cancelled', cancelled=1, completed_at=COALESCE(completed_at, ?) "
                "WHERE root_run_id=? AND status IN ('queued','running')",
                (completed, root_run_id),
            )

    def cleanup(self, retention_days: int) -> int:
        cutoff = (datetime.now(UTC) - timedelta(days=max(1, retention_days))).isoformat()
        with self._lock, self.conn:
            cursor = self.conn.execute("DELETE FROM agent_runs WHERE created_at < ?", (cutoff,))
            return int(cursor.rowcount or 0)

    def close(self) -> None:
        with self._lock:
            self.conn.close()
