"""Durable reflection follow-up opportunities and their lifecycle."""

from __future__ import annotations

import hashlib
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from ares.config import get_db_path
from ares.sqlite_utils import connect_sqlite


_SPACE_RE = re.compile(r"\s+")
_OPEN_STATUSES = {"pending", "snoozed"}
_TERMINAL_STATUSES = {"resolved", "dismissed", "cancelled"}
_STATUSES = _OPEN_STATUSES | _TERMINAL_STATUSES


def utc_now(value: datetime | None = None) -> str:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def future_utc(hours: int, *, now: datetime | None = None) -> str:
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return utc_now(current + timedelta(hours=max(0, int(hours))))


def _clean(value: Any, *, field: str, maximum: int, required: bool = False) -> str:
    text = _SPACE_RE.sub(" ", str(value or "")).strip()
    if required and not text:
        raise ValueError(f"{field} is required")
    if len(text) > maximum:
        raise ValueError(f"{field} must be at most {maximum} characters")
    return text


def _iso_timestamp(value: Any, field: str) -> str | None:
    if value is None or not str(value).strip():
        return None
    try:
        parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field} must be an ISO timestamp") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return utc_now(parsed)


def _dedupe_key(description: str) -> str:
    normalized = _SPACE_RE.sub(" ", description).strip().casefold()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


class FollowUpStore:
    """SQLite-backed queue for opportunities discovered by reflection."""

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
            CREATE TABLE IF NOT EXISTS follow_up_opportunities (
                follow_up_id TEXT PRIMARY KEY,
                description TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                confidence REAL NOT NULL,
                source_conversation_id TEXT,
                source_reflection_id TEXT,
                evidence TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                eligible_at TEXT NOT NULL,
                cooldown_hours INTEGER NOT NULL DEFAULT 72,
                last_attempt_at TEXT,
                last_delivered_at TEXT,
                resolution TEXT NOT NULL DEFAULT '',
                resolved_at TEXT,
                dedupe_key TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_followups_eligible
                ON follow_up_opportunities(status, eligible_at, confidence DESC);
            CREATE INDEX IF NOT EXISTS idx_followups_source
                ON follow_up_opportunities(source_conversation_id, created_at DESC);
            CREATE UNIQUE INDEX IF NOT EXISTS idx_followups_open_dedupe
                ON follow_up_opportunities(dedupe_key)
                WHERE status IN ('pending', 'snoozed');
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
        confidence: float,
        source_conversation_id: str | None,
        source_reflection_id: str | None,
        eligible_at: str | None = None,
        cooldown_hours: int = 72,
        evidence: str = "",
    ) -> dict[str, Any]:
        clean_description = _clean(
            description, field="description", maximum=2_000, required=True,
        )
        numeric_confidence = float(confidence)
        if not 0 <= numeric_confidence <= 1:
            raise ValueError("confidence must be from 0 to 1")
        cooldown = max(1, min(int(cooldown_hours), 8_760))
        key = _dedupe_key(clean_description)
        existing = self.conn.execute(
            """SELECT * FROM follow_up_opportunities
               WHERE dedupe_key=? AND status IN ('pending', 'snoozed')""",
            (key,),
        ).fetchone()
        if existing is not None:
            return dict(existing)
        now = utc_now()
        eligible = _iso_timestamp(eligible_at, "eligible_at") or now
        follow_up_id = hashlib.sha256(
            f"{key}\0{source_reflection_id or now}".encode("utf-8")
        ).hexdigest()[:32]
        self.conn.execute(
            """INSERT INTO follow_up_opportunities
               (follow_up_id, description, status, confidence, source_conversation_id,
                source_reflection_id, evidence, created_at, updated_at, eligible_at,
                cooldown_hours, last_attempt_at, last_delivered_at, resolution,
                resolved_at, dedupe_key)
               VALUES (?, ?, 'pending', ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, '', NULL, ?)""",
            (
                follow_up_id,
                clean_description,
                numeric_confidence,
                _clean(source_conversation_id, field="source_conversation_id", maximum=180) or None,
                _clean(source_reflection_id, field="source_reflection_id", maximum=80) or None,
                _clean(evidence, field="evidence", maximum=1_000),
                now,
                now,
                eligible,
                cooldown,
                key,
            ),
        )
        self.conn.commit()
        return self.get(follow_up_id) or {}

    def get(self, follow_up_id: str) -> dict[str, Any] | None:
        return self._row(self.conn.execute(
            "SELECT * FROM follow_up_opportunities WHERE follow_up_id=?",
            (str(follow_up_id),),
        ).fetchone())

    def list_eligible(self, *, now: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        stamp = _iso_timestamp(now, "now") if now else utc_now()
        rows = self.conn.execute(
            """SELECT * FROM follow_up_opportunities
               WHERE status IN ('pending', 'snoozed') AND eligible_at<=?
               ORDER BY confidence DESC, eligible_at, created_at LIMIT ?""",
            (stamp, max(1, min(int(limit), 200))),
        ).fetchall()
        return [dict(row) for row in rows]

    def list_open(self, *, limit: int = 100) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """SELECT * FROM follow_up_opportunities
               WHERE status IN ('pending', 'snoozed')
               ORDER BY eligible_at, confidence DESC LIMIT ?""",
            (max(1, min(int(limit), 500)),),
        ).fetchall()
        return [dict(row) for row in rows]

    def defer(
        self,
        follow_up_id: str,
        *,
        eligible_at: str,
        delivered: bool = False,
        when: datetime | None = None,
    ) -> dict[str, Any] | None:
        if self.get(follow_up_id) is None:
            return None
        now = utc_now(when)
        self.conn.execute(
            """UPDATE follow_up_opportunities
               SET status=?, eligible_at=?, updated_at=?, last_attempt_at=?,
                   last_delivered_at=CASE WHEN ? THEN ? ELSE last_delivered_at END
               WHERE follow_up_id=? AND status IN ('pending', 'snoozed')""",
            (
                "snoozed" if delivered else "pending",
                _iso_timestamp(eligible_at, "eligible_at"),
                now,
                now,
                1 if delivered else 0,
                now,
                str(follow_up_id),
            ),
        )
        self.conn.commit()
        return self.get(follow_up_id)

    def resolve(
        self,
        follow_up_id: str,
        *,
        status: str = "resolved",
        resolution: str,
    ) -> dict[str, Any] | None:
        normalized = str(status or "resolved").strip().casefold()
        if normalized not in _TERMINAL_STATUSES:
            raise ValueError("status must be resolved, dismissed, or cancelled")
        now = utc_now()
        cursor = self.conn.execute(
            """UPDATE follow_up_opportunities
               SET status=?, resolution=?, resolved_at=?, updated_at=?
               WHERE follow_up_id=? AND status IN ('pending', 'snoozed')""",
            (
                normalized,
                _clean(resolution, field="resolution", maximum=2_000, required=True),
                now,
                now,
                str(follow_up_id),
            ),
        )
        self.conn.commit()
        return self.get(follow_up_id) if cursor.rowcount else None

    def list_all_for_export(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM follow_up_opportunities ORDER BY created_at, follow_up_id"
        ).fetchall()
        return [dict(row) for row in rows]

    def close(self) -> None:
        if self._owns_connection:
            self.conn.close()


__all__ = ["FollowUpStore", "future_utc", "utc_now"]
