"""Durable lightweight commitments extracted from conversations.

Commitments are intentionally separate from ``TaskStore``.  A Task is an
executable, tool-backed workflow; a commitment is a promise or obligation that
may not have an executable plan yet.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ares.config import get_db_path
from ares.infra.sqlite_utils import connect_sqlite


_STATUSES = {"pending", "completed", "cancelled"}
_OWNERS = {"user", "ares", "shared"}
_SPACE_RE = re.compile(r"\s+")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _clean(value: Any, *, field: str, maximum: int, required: bool = False) -> str:
    text = _SPACE_RE.sub(" ", str(value or "")).strip()
    if required and not text:
        raise ValueError(f"{field} is required")
    if len(text) > maximum:
        raise ValueError(f"{field} must be at most {maximum} characters")
    return text


def _choice(value: Any, choices: set[str], field: str) -> str:
    normalized = str(value or "").strip().casefold()
    if normalized not in choices:
        raise ValueError(f"{field} must be one of: {', '.join(sorted(choices))}")
    return normalized


def _iso_timestamp(value: Any, field: str) -> str | None:
    if value is None or not str(value).strip():
        return None
    text = str(value).strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"{field} must be an ISO timestamp") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _dedupe_key(owner: str, description: str) -> str:
    normalized = _SPACE_RE.sub(" ", description).strip().casefold()
    return hashlib.sha256(f"{owner}\0{normalized}".encode("utf-8")).hexdigest()


class CommitmentStore:
    """SQLite-backed promises and obligations with idempotent creation."""

    def __init__(
        self,
        db_path: str | Path | None = None,
        *,
        connection: sqlite3.Connection | None = None,
    ) -> None:
        self.db_path = Path(db_path) if db_path is not None else get_db_path()
        self._owns_connection = connection is None
        if connection is None:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            self.conn = connect_sqlite(self.db_path)
        else:
            self.conn = connection
        self._init_db()

    def _init_db(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS commitments_meta (
                commitment_id INTEGER PRIMARY KEY AUTOINCREMENT,
                description TEXT NOT NULL,
                owner TEXT NOT NULL DEFAULT 'user',
                status TEXT NOT NULL DEFAULT 'pending',
                due_at TEXT,
                confidence REAL NOT NULL DEFAULT 1.0,
                source_conversation_id TEXT,
                source_reflection_id TEXT,
                dedupe_key TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                completed_at TEXT,
                last_activity_at TEXT NOT NULL,
                last_reminder_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_commitments_status_due
                ON commitments_meta(status, due_at);
            CREATE INDEX IF NOT EXISTS idx_commitments_activity
                ON commitments_meta(status, last_activity_at);
            CREATE UNIQUE INDEX IF NOT EXISTS idx_commitments_pending_dedupe
                ON commitments_meta(dedupe_key) WHERE status='pending';
            """
        )
        self.conn.commit()

    @staticmethod
    def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
        return dict(row) if row is not None else None

    def create(
        self,
        description: str,
        *,
        owner: str = "user",
        due_at: str | None = None,
        confidence: float = 1.0,
        source_conversation_id: str | None = None,
        source_reflection_id: str | None = None,
    ) -> dict[str, Any]:
        clean_description = _clean(description, field="description", maximum=2_000, required=True)
        clean_owner = _choice(owner, _OWNERS, "owner")
        numeric_confidence = float(confidence)
        if not 0 <= numeric_confidence <= 1:
            raise ValueError("confidence must be from 0 to 1")
        key = _dedupe_key(clean_owner, clean_description)
        existing = self.conn.execute(
            "SELECT * FROM commitments_meta WHERE dedupe_key=? AND status='pending'",
            (key,),
        ).fetchone()
        if existing is not None:
            return dict(existing)
        now = utc_now()
        cursor = self.conn.execute(
            """INSERT INTO commitments_meta
               (description, owner, status, due_at, confidence, source_conversation_id,
                source_reflection_id, dedupe_key, created_at, updated_at, completed_at,
                last_activity_at, last_reminder_at)
               VALUES (?, ?, 'pending', ?, ?, ?, ?, ?, ?, ?, NULL, ?, NULL)""",
            (
                clean_description,
                clean_owner,
                _iso_timestamp(due_at, "due_at"),
                numeric_confidence,
                _clean(source_conversation_id, field="source_conversation_id", maximum=180) or None,
                _clean(source_reflection_id, field="source_reflection_id", maximum=80) or None,
                key,
                now,
                now,
                now,
            ),
        )
        self.conn.commit()
        return self.get(int(cursor.lastrowid)) or {}

    def get(self, commitment_id: int) -> dict[str, Any] | None:
        return self._row(self.conn.execute(
            "SELECT * FROM commitments_meta WHERE commitment_id=?", (int(commitment_id),)
        ).fetchone())

    def update(
        self,
        commitment_id: int,
        *,
        description: str | None = None,
        owner: str | None = None,
        status: str | None = None,
        due_at: str | None = None,
        confidence: float | None = None,
    ) -> dict[str, Any] | None:
        existing = self.get(commitment_id)
        if existing is None:
            return None
        new_description = (
            _clean(description, field="description", maximum=2_000, required=True)
            if description is not None else existing["description"]
        )
        new_owner = _choice(owner, _OWNERS, "owner") if owner is not None else existing["owner"]
        new_status = _choice(status, _STATUSES, "status") if status is not None else existing["status"]
        new_due = _iso_timestamp(due_at, "due_at") if due_at is not None else existing["due_at"]
        new_confidence = float(confidence) if confidence is not None else float(existing["confidence"])
        if not 0 <= new_confidence <= 1:
            raise ValueError("confidence must be from 0 to 1")
        now = utc_now()
        completed_at = existing["completed_at"]
        if new_status == "completed":
            completed_at = completed_at or now
        elif existing["status"] == "completed":
            completed_at = None
        self.conn.execute(
            """UPDATE commitments_meta SET description=?, owner=?, status=?, due_at=?,
               confidence=?, dedupe_key=?, updated_at=?, last_activity_at=?, completed_at=?
               WHERE commitment_id=?""",
            (
                new_description,
                new_owner,
                new_status,
                new_due,
                new_confidence,
                _dedupe_key(new_owner, new_description),
                now,
                now,
                completed_at,
                int(commitment_id),
            ),
        )
        self.conn.commit()
        return self.get(commitment_id)

    def complete(self, commitment_id: int) -> dict[str, Any] | None:
        return self.update(commitment_id, status="completed")

    def list_pending(self, *, limit: int = 20) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """SELECT * FROM commitments_meta WHERE status='pending'
               ORDER BY CASE WHEN due_at IS NULL THEN 1 ELSE 0 END, due_at, updated_at DESC
               LIMIT ?""",
            (max(1, min(int(limit), 200)),),
        ).fetchall()
        return [dict(row) for row in rows]

    def inactive(self, *, before: str, limit: int = 50) -> list[dict[str, Any]]:
        cutoff = _iso_timestamp(before, "before")
        rows = self.conn.execute(
            """SELECT * FROM commitments_meta WHERE status='pending' AND last_activity_at <= ?
               ORDER BY last_activity_at, due_at LIMIT ?""",
            (cutoff, max(1, min(int(limit), 200))),
        ).fetchall()
        return [dict(row) for row in rows]

    def mark_reminded(self, commitment_id: int, *, when: str | None = None) -> dict[str, Any] | None:
        stamp = _iso_timestamp(when, "when") if when else utc_now()
        self.conn.execute(
            "UPDATE commitments_meta SET last_reminder_at=?, updated_at=? WHERE commitment_id=?",
            (stamp, stamp, int(commitment_id)),
        )
        self.conn.commit()
        return self.get(commitment_id)

    def list_all_for_export(self) -> list[dict[str, Any]]:
        rows = self.conn.execute("SELECT * FROM commitments_meta ORDER BY commitment_id").fetchall()
        return [dict(row) for row in rows]

    def import_commitments(self, commitments: list[dict[str, Any]]) -> int:
        """Restore commitment records without duplicating exact existing rows."""
        if not isinstance(commitments, list):
            return 0
        imported = 0
        for item in commitments:
            if not isinstance(item, dict) or not str(item.get("description") or "").strip():
                continue
            owner = _choice(item.get("owner") or "user", _OWNERS, "owner")
            description = _clean(
                item["description"], field="description", maximum=2_000, required=True,
            )
            status = _choice(item.get("status") or "pending", _STATUSES, "status")
            existing = self.conn.execute(
                """SELECT commitment_id FROM commitments_meta
                   WHERE owner=? AND description=? AND status=? LIMIT 1""",
                (owner, description, status),
            ).fetchone()
            if existing is not None:
                continue
            now = utc_now()
            created_at = _iso_timestamp(item.get("created_at"), "created_at") or now
            updated_at = _iso_timestamp(item.get("updated_at"), "updated_at") or created_at
            completed_at = _iso_timestamp(item.get("completed_at"), "completed_at")
            if status == "completed" and completed_at is None:
                completed_at = updated_at
            confidence = float(item.get("confidence", 1.0))
            if not 0 <= confidence <= 1:
                continue
            self.conn.execute(
                """INSERT INTO commitments_meta
                   (description, owner, status, due_at, confidence, source_conversation_id,
                    source_reflection_id, dedupe_key, created_at, updated_at, completed_at,
                    last_activity_at, last_reminder_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    description,
                    owner,
                    status,
                    _iso_timestamp(item.get("due_at"), "due_at"),
                    confidence,
                    _clean(item.get("source_conversation_id"), field="source_conversation_id", maximum=180) or None,
                    _clean(item.get("source_reflection_id"), field="source_reflection_id", maximum=80) or None,
                    _dedupe_key(owner, description),
                    created_at,
                    updated_at,
                    completed_at,
                    _iso_timestamp(item.get("last_activity_at"), "last_activity_at") or updated_at,
                    _iso_timestamp(item.get("last_reminder_at"), "last_reminder_at"),
                ),
            )
            self.conn.commit()
            imported += 1
        return imported

    def close(self) -> None:
        if self._owns_connection:
            self.conn.close()
