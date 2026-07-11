"""Durable, privacy-minimized provenance for consequential Ares actions."""

from __future__ import annotations

import json
import re
import sqlite3
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from ares.config import get_db_path
from ares.sqlite_utils import connect_sqlite


_CURRENT_TASK_ID: ContextVar[str | None] = ContextVar("ares_action_task_id", default=None)
_ACTION_COLLECTOR: ContextVar[list[int] | None] = ContextVar("ares_action_collector", default=None)
_WHITESPACE = re.compile(r"\s+")
_TAG = re.compile(r"[^a-z0-9_.:-]+")
_REFERENCE_LANGUAGE = re.compile(
    r"\b(?:that\s+(?:file|thing|image|task)|the\s+(?:file|thing)\s+i\s+(?:made|created)|"
    r"remember\s+when|yesterday|last\s+(?:week|month)|\d+\s+(?:day|week|month|year)s?\s+ago|from\s+\d+\s+days?\s+ago)\b",
    re.IGNORECASE,
)
_RELATIVE_SINCE = re.compile(
    r"\b(?:yesterday|last\s+(?:week|month)|\d+\s+(?:day|week|month|year)s?\s+ago)\b",
    re.IGNORECASE,
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def active_task_id() -> str | None:
    """Return the workflow task owning the current action, if any."""
    return _CURRENT_TASK_ID.get()


@contextmanager
def action_task_context(task_id: str | None) -> Iterator[None]:
    """Tag all nested action records with a workflow task without globals."""
    token = _CURRENT_TASK_ID.set(str(task_id) if task_id else None)
    try:
        yield
    finally:
        _CURRENT_TASK_ID.reset(token)


@contextmanager
def collect_action_ids() -> Iterator[list[int]]:
    """Collect action ids produced by nested tool calls for task provenance."""
    collected: list[int] = []
    token = _ACTION_COLLECTOR.set(collected)
    try:
        yield collected
    finally:
        _ACTION_COLLECTOR.reset(token)


def has_reference_language(text: str) -> bool:
    """Whether a message likely refers to work performed in another session."""
    return bool(_REFERENCE_LANGUAGE.search(str(text or "")))


def extract_since_reference(text: str) -> str | None:
    match = _RELATIVE_SINCE.search(str(text or ""))
    return match.group(0) if match else None


def _clean_one_line(value: Any, *, maximum: int) -> str:
    return _WHITESPACE.sub(" ", str(value or "")).strip()[:maximum]


def _clean_tags(tags: list[str] | tuple[str, ...] | set[str] | None) -> list[str]:
    clean: list[str] = []
    for value in tags or []:
        tag = _TAG.sub("-", str(value or "").casefold()).strip("-._:")[:64]
        if tag and tag not in clean:
            clean.append(tag)
    return clean[:20]


def _parse_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _parse_since_value(value: str) -> datetime:
    """Parse ISO or local relative text without importing ``ares.tools``.

    Importing a submodule of ``ares.tools`` initializes its public executor
    package, which itself owns the ledger. Keeping this tiny boundary local
    avoids an import cycle during application startup.
    """
    text = str(value or "").strip()
    try:
        return _parse_timestamp(text)
    except (TypeError, ValueError):
        pass
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
        raise ValueError(f"Could not parse relative date: {value}")
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=datetime.now().astimezone().tzinfo)


class ActionLedger:
    """Searchable local record of what happened, without copying private content."""

    def __init__(self, db_path: str | Path | None = None, *, connection: sqlite3.Connection | None = None):
        self.db_path = Path(db_path) if db_path is not None else get_db_path()
        self._owns_connection = connection is None
        if connection is None:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            self.conn = connect_sqlite(self.db_path)
        else:
            self.conn = connection
        self.fts_enabled = False
        self._init_db()

    def _init_db(self) -> None:
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS actions_log (
                action_id   INTEGER PRIMARY KEY AUTOINCREMENT,
                action_type TEXT NOT NULL,
                target      TEXT NOT NULL DEFAULT '',
                summary     TEXT NOT NULL,
                tool_name   TEXT NOT NULL,
                session_id  TEXT,
                task_id     TEXT,
                created_at  TEXT NOT NULL,
                tags_json   TEXT NOT NULL DEFAULT '[]'
            )
            """
        )
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_actions_recent ON actions_log(created_at DESC, action_id DESC)")
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_actions_task ON actions_log(task_id, created_at DESC)")
        try:
            self.conn.execute(
                """
                CREATE VIRTUAL TABLE IF NOT EXISTS actions_fts USING fts5(
                    target, summary, tags,
                    content='actions_log', content_rowid='action_id'
                )
                """
            )
            self.fts_enabled = True
        except sqlite3.DatabaseError:
            self.fts_enabled = False
        self.conn.commit()

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
    def _validate_action_type(value: str) -> str:
        action_type = str(value or "").strip().casefold()
        if not re.fullmatch(r"[a-z][a-z0-9_:-]{0,79}", action_type):
            raise ValueError("action_type must be a short lowercase identifier")
        return action_type

    def record(
        self,
        action_type: str,
        *,
        target: str = "",
        summary: str,
        tool_name: str,
        session_id: str | None = None,
        task_id: str | None = None,
        tags: list[str] | tuple[str, ...] | set[str] | None = None,
        created_at: str | None = None,
    ) -> int:
        """Record provenance only; callers must never pass message/body content."""
        normalized_type = self._validate_action_type(action_type)
        clean_summary = _clean_one_line(summary, maximum=360)
        if not clean_summary:
            raise ValueError("action summary is required")
        clean_target = _clean_one_line(target, maximum=512)
        clean_tool = _clean_one_line(tool_name, maximum=160)
        if not clean_tool:
            raise ValueError("tool_name is required")
        tags_list = _clean_tags(tags)
        timestamp = created_at or utc_now()
        # Reject malformed imported timestamps rather than corrupt ordering.
        _parse_timestamp(timestamp)
        owning_task = task_id if task_id is not None else active_task_id()
        with self._transaction():
            cursor = self.conn.execute(
                """
                INSERT INTO actions_log (action_type, target, summary, tool_name, session_id, task_id, created_at, tags_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    normalized_type,
                    clean_target,
                    clean_summary,
                    clean_tool,
                    _clean_one_line(session_id, maximum=160) or None,
                    _clean_one_line(owning_task, maximum=160) or None,
                    timestamp,
                    json.dumps(tags_list, ensure_ascii=False),
                ),
            )
            action_id = int(cursor.lastrowid)
            if self.fts_enabled:
                self.conn.execute(
                    "INSERT INTO actions_fts (rowid, target, summary, tags) VALUES (?, ?, ?, ?)",
                    (action_id, clean_target, clean_summary, " ".join(tags_list)),
                )
        collector = _ACTION_COLLECTOR.get()
        if collector is not None:
            collector.append(action_id)
        return action_id

    @staticmethod
    def _row_to_action(row: sqlite3.Row) -> dict[str, Any]:
        try:
            tags = json.loads(row["tags_json"] or "[]")
        except (TypeError, json.JSONDecodeError):
            tags = []
        return {
            "action_id": int(row["action_id"]),
            "action_type": row["action_type"],
            "target": row["target"],
            "summary": row["summary"],
            "tool_name": row["tool_name"],
            "session_id": row["session_id"],
            "task_id": row["task_id"],
            "created_at": row["created_at"],
            "tags": tags if isinstance(tags, list) else [],
        }

    @staticmethod
    def _since_timestamp(since: str | None) -> str | None:
        if since is None or not str(since).strip():
            return None
        try:
            parsed = _parse_since_value(str(since))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Could not parse 'since' value: {since}") from exc
        return parsed.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")

    @staticmethod
    def _fts_query(query: str) -> str:
        tokens = re.findall(r"[\w]+", query, flags=re.UNICODE)
        return " AND ".join(f'"{token.replace(chr(34), chr(34) * 2)}"' for token in tokens)

    def search(
        self,
        query: str = "",
        *,
        since: str | None = None,
        limit: int = 20,
        task_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Search action summaries/tags while keeping a deterministic fallback."""
        bounded = max(1, min(int(limit), 100))
        query_text = _clean_one_line(query, maximum=300)
        clauses: list[str] = []
        params: list[Any] = []
        since_timestamp = self._since_timestamp(since)
        if since_timestamp:
            clauses.append("a.created_at >= ?")
            params.append(since_timestamp)
        if task_id:
            clauses.append("a.task_id = ?")
            params.append(str(task_id))
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""

        rows: list[sqlite3.Row] = []
        if query_text and self.fts_enabled and self._fts_query(query_text):
            try:
                fts_where = " AND ".join(["actions_fts MATCH ?", *clauses])
                rows = self.conn.execute(
                    f"""
                    SELECT a.* FROM actions_fts
                    JOIN actions_log AS a ON a.action_id = actions_fts.rowid
                    WHERE {fts_where}
                    ORDER BY actions_fts.rank, a.created_at DESC
                    LIMIT ?
                    """,
                    [self._fts_query(query_text), *params, bounded],
                ).fetchall()
            except sqlite3.DatabaseError:
                rows = []
        if not rows:
            if query_text:
                like = f"%{query_text}%"
                extra = " AND " if clauses else " WHERE "
                rows = self.conn.execute(
                    f"""
                    SELECT a.* FROM actions_log AS a{where}{extra}
                    (a.target LIKE ? COLLATE NOCASE OR a.summary LIKE ? COLLATE NOCASE OR a.tags_json LIKE ? COLLATE NOCASE)
                    ORDER BY a.created_at DESC, a.action_id DESC
                    LIMIT ?
                    """,
                    [*params, like, like, like, bounded],
                ).fetchall()
            else:
                rows = self.conn.execute(
                    f"SELECT a.* FROM actions_log AS a{where} ORDER BY a.created_at DESC, a.action_id DESC LIMIT ?",
                    [*params, bounded],
                ).fetchall()
        return [self._row_to_action(row) for row in rows]

    def recent(self, *, limit: int = 8) -> list[dict[str, Any]]:
        return self.search(limit=limit)

    def list_all(self) -> list[dict[str, Any]]:
        """Return the complete local ledger for explicit backup/export paths."""
        rows = self.conn.execute(
            "SELECT * FROM actions_log ORDER BY created_at DESC, action_id DESC"
        ).fetchall()
        return [self._row_to_action(row) for row in rows]

    def import_actions(self, actions: list[dict[str, Any]]) -> int:
        """Import provenance records, skipping exact action-id-independent duplicates."""
        imported = 0
        for item in actions:
            action_type = str(item.get("action_type") or "").strip()
            summary = str(item.get("summary") or "").strip()
            created_at = item.get("created_at")
            if not action_type or not summary or not created_at:
                continue
            duplicate = self.conn.execute(
                """
                SELECT 1 FROM actions_log
                WHERE action_type = ? AND target = ? AND summary = ? AND created_at = ?
                LIMIT 1
                """,
                (action_type, str(item.get("target") or ""), summary, str(created_at)),
            ).fetchone()
            if duplicate:
                continue
            self.record(
                action_type,
                target=str(item.get("target") or ""),
                summary=summary,
                tool_name=str(item.get("tool_name") or "import"),
                session_id=item.get("session_id"),
                task_id=item.get("task_id"),
                tags=item.get("tags") if isinstance(item.get("tags"), list) else [],
                created_at=str(created_at),
            )
            imported += 1
        return imported

    def count(self) -> int:
        row = self.conn.execute("SELECT COUNT(*) AS count FROM actions_log").fetchone()
        return int(row["count"]) if row else 0

    def close(self) -> None:
        if self._owns_connection:
            self.conn.close()
