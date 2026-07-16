"""Durable, local-only storage for Ares Vision metadata.

Frames are deliberately not written by this module.  It records a reference
only when a caller has explicitly decided that an event frame should be kept.
This keeps normal observation and live watches free of continuous footage.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from ares.vision.models import (
    SceneSnapshot,
    VisualEvent,
    VisionSource,
    VisionWatch,
)


def utc_now() -> str:
    """Return a timezone-aware ISO timestamp suitable for SQLite."""
    return datetime.now(timezone.utc).isoformat()


def _dump(value: Any) -> str:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def _load(value: str | None, fallback: Any) -> Any:
    if not value:
        return fallback
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return fallback


class VisionStore:
    """SQLite state for sources, snapshots, events, watches and memory links.

    The connection is private to VisionStore and protected by a re-entrant
    lock.  This avoids handing Ares' shared MemoryStore connection to capture
    workers, which may execute in a background thread.
    """

    def __init__(
        self,
        database_path: str | Path,
        *,
        artifact_root: str | Path | None = None,
    ) -> None:
        self.database_path = Path(database_path).expanduser()
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.artifact_root = (
            Path(artifact_root).expanduser()
            if artifact_root is not None
            else self.database_path.parent / "vision"
        )
        self.artifact_root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(self.database_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA journal_mode=WAL")
        self._init_db()

    def close(self) -> None:
        with self._lock:
            self.conn.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            try:
                self.conn.execute("BEGIN IMMEDIATE")
                yield self.conn
            except BaseException:
                self.conn.rollback()
                raise
            else:
                self.conn.commit()

    def _init_db(self) -> None:
        with self._lock:
            self.conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS vision_sources (
                    source_id TEXT PRIMARY KEY,
                    source_type TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'stopped',
                    source_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS vision_permissions (
                    source_id TEXT PRIMARY KEY,
                    observe_allowed INTEGER NOT NULL DEFAULT 0,
                    remember_allowed INTEGER NOT NULL DEFAULT 0,
                    active_indicator INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS vision_snapshots (
                    snapshot_id TEXT PRIMARY KEY,
                    source_id TEXT NOT NULL,
                    captured_at TEXT NOT NULL,
                    snapshot_json TEXT NOT NULL,
                    frame_reference TEXT,
                    FOREIGN KEY(source_id) REFERENCES vision_sources(source_id)
                );
                CREATE INDEX IF NOT EXISTS idx_vision_snapshots_source_time
                    ON vision_snapshots(source_id, captured_at DESC);
                CREATE TABLE IF NOT EXISTS vision_events (
                    event_id TEXT PRIMARY KEY,
                    source_id TEXT NOT NULL,
                    occurred_at TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    event_json TEXT NOT NULL,
                    frame_reference TEXT,
                    remembered INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS idx_vision_events_source_time
                    ON vision_events(source_id, occurred_at DESC);
                CREATE TABLE IF NOT EXISTS vision_watches (
                    watch_id TEXT PRIMARY KEY,
                    source_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    expires_at TEXT,
                    watch_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_vision_watches_source_status
                    ON vision_watches(source_id, status);
                CREATE TABLE IF NOT EXISTS vision_memory_links (
                    event_id TEXT NOT NULL,
                    fact_id INTEGER NOT NULL UNIQUE,
                    frame_reference TEXT,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(event_id, fact_id),
                    FOREIGN KEY(event_id) REFERENCES vision_events(event_id) ON DELETE CASCADE
                );
                """
            )
            self.conn.commit()

    # -- Sources and consent -------------------------------------------------

    def save_source(self, source: VisionSource) -> VisionSource:
        data = source.model_dump(mode="json")
        now = utc_now()
        with self.transaction() as conn:
            conn.execute(
                """INSERT INTO vision_sources(source_id, source_type, status, source_json, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(source_id) DO UPDATE SET
                     source_type=excluded.source_type, status=excluded.status,
                     source_json=excluded.source_json, updated_at=excluded.updated_at""",
                (
                    source.source_id,
                    str(getattr(source.source_type, "value", source.source_type)),
                    str(getattr(source.status, "value", source.status)),
                    _dump(data), now, now,
                ),
            )
        return source

    def get_source(self, source_id: str) -> VisionSource | None:
        with self._lock:
            row = self.conn.execute(
                "SELECT source_json FROM vision_sources WHERE source_id=?", (source_id,)
            ).fetchone()
        return VisionSource.model_validate(_load(row["source_json"], {})) if row else None

    def list_sources(self) -> list[VisionSource]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT source_json FROM vision_sources ORDER BY updated_at DESC"
            ).fetchall()
        return [VisionSource.model_validate(_load(row["source_json"], {})) for row in rows]

    def delete_source(self, source_id: str) -> bool:
        """Erase one source and all of its local vision history.

        A source is the privacy boundary for snapshots and watches.  Deleting
        it must not leave an inaccessible row (or an owned retained frame)
        behind merely because snapshots use a foreign key.
        """
        with self._lock:
            event_ids = [
                str(row["event_id"])
                for row in self.conn.execute(
                    "SELECT event_id FROM vision_events WHERE source_id=?", (source_id,)
                ).fetchall()
            ]
            snapshot_references = [
                str(row["frame_reference"])
                for row in self.conn.execute(
                    "SELECT frame_reference FROM vision_snapshots WHERE source_id=? AND frame_reference IS NOT NULL",
                    (source_id,),
                ).fetchall()
            ]
        for event_id in event_ids:
            self.delete_event(event_id, delete_frame=True)
        with self.transaction() as conn:
            conn.execute("DELETE FROM vision_watches WHERE source_id=?", (source_id,))
            conn.execute("DELETE FROM vision_snapshots WHERE source_id=?", (source_id,))
            conn.execute("DELETE FROM vision_permissions WHERE source_id=?", (source_id,))
            deleted = bool(conn.execute("DELETE FROM vision_sources WHERE source_id=?", (source_id,)).rowcount)
        for reference in snapshot_references:
            self._delete_artifact(reference)
        return deleted

    def set_permission(
        self,
        source_id: str,
        *,
        observe_allowed: bool | None = None,
        remember_allowed: bool | None = None,
        active_indicator: bool | None = None,
    ) -> dict[str, bool]:
        current = self.get_permission(source_id)
        values = {
            "observe_allowed": current["observe_allowed"] if observe_allowed is None else bool(observe_allowed),
            "remember_allowed": current["remember_allowed"] if remember_allowed is None else bool(remember_allowed),
            "active_indicator": current["active_indicator"] if active_indicator is None else bool(active_indicator),
        }
        with self.transaction() as conn:
            conn.execute(
                """INSERT INTO vision_permissions(source_id, observe_allowed, remember_allowed, active_indicator, updated_at)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(source_id) DO UPDATE SET
                     observe_allowed=excluded.observe_allowed,
                     remember_allowed=excluded.remember_allowed,
                     active_indicator=excluded.active_indicator,
                     updated_at=excluded.updated_at""",
                (source_id, int(values["observe_allowed"]), int(values["remember_allowed"]),
                 int(values["active_indicator"]), utc_now()),
            )
        return values

    def get_permission(self, source_id: str) -> dict[str, bool]:
        with self._lock:
            row = self.conn.execute(
                "SELECT observe_allowed, remember_allowed, active_indicator FROM vision_permissions WHERE source_id=?",
                (source_id,),
            ).fetchone()
        if row is None:
            return {"observe_allowed": False, "remember_allowed": False, "active_indicator": False}
        return {key: bool(row[key]) for key in ("observe_allowed", "remember_allowed", "active_indicator")}

    # -- Snapshots and events -------------------------------------------------

    def save_snapshot(self, snapshot: SceneSnapshot) -> SceneSnapshot:
        data = snapshot.model_dump(mode="json")
        with self.transaction() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO vision_snapshots
                   (snapshot_id, source_id, captured_at, snapshot_json, frame_reference)
                   VALUES (?, ?, ?, ?, ?)""",
                (
                    snapshot.snapshot_id, snapshot.source_id,
                    snapshot.captured_at.isoformat(), _dump(data), snapshot.frame_reference,
                ),
            )
        return snapshot

    def get_snapshot(self, snapshot_id: str) -> SceneSnapshot | None:
        with self._lock:
            row = self.conn.execute(
                "SELECT snapshot_json FROM vision_snapshots WHERE snapshot_id=?", (snapshot_id,)
            ).fetchone()
        return SceneSnapshot.model_validate(_load(row["snapshot_json"], {})) if row else None

    def latest_snapshot(self, source_id: str) -> SceneSnapshot | None:
        with self._lock:
            row = self.conn.execute(
                "SELECT snapshot_json FROM vision_snapshots WHERE source_id=? ORDER BY captured_at DESC LIMIT 1",
                (source_id,),
            ).fetchone()
        return SceneSnapshot.model_validate(_load(row["snapshot_json"], {})) if row else None

    def list_snapshots(self, source_id: str, *, limit: int = 100) -> list[SceneSnapshot]:
        """Return recent source-local snapshots without exposing frame pixels."""
        with self._lock:
            rows = self.conn.execute(
                "SELECT snapshot_json FROM vision_snapshots WHERE source_id=? ORDER BY captured_at DESC LIMIT ?",
                (source_id, max(1, min(int(limit), 1000))),
            ).fetchall()
        return [SceneSnapshot.model_validate(_load(row["snapshot_json"], {})) for row in rows]

    def prune_snapshots(self, source_id: str, *, keep: int = 2) -> int:
        """Bound semantic history per source while retaining before/after compare."""

        bounded = max(2, min(int(keep), 100))
        with self._lock:
            rows = self.conn.execute(
                """SELECT snapshot_id, frame_reference FROM vision_snapshots
                   WHERE source_id=? ORDER BY captured_at DESC, rowid DESC
                   LIMIT -1 OFFSET ?""",
                (source_id, bounded),
            ).fetchall()
        if not rows:
            return 0
        ids = [str(row["snapshot_id"]) for row in rows]
        references = [str(row["frame_reference"]) for row in rows if row["frame_reference"]]
        placeholders = ",".join("?" for _ in ids)
        with self.transaction() as conn:
            conn.execute(f"DELETE FROM vision_snapshots WHERE snapshot_id IN ({placeholders})", ids)
        for reference in references:
            self._delete_artifact(reference)
        return len(ids)

    def save_event(self, event: VisualEvent) -> VisualEvent:
        data = event.model_dump(mode="json")
        with self.transaction() as conn:
            conn.execute(
                """INSERT INTO vision_events
                   (event_id, source_id, occurred_at, event_type, event_json, frame_reference, remembered)
                   VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(event_id) DO UPDATE SET
                     source_id=excluded.source_id,
                     occurred_at=excluded.occurred_at,
                     event_type=excluded.event_type,
                     event_json=excluded.event_json,
                     frame_reference=excluded.frame_reference,
                     remembered=excluded.remembered""",
                (
                    event.event_id, event.source_id, event.occurred_at.isoformat(), event.event_type,
                    _dump(data), event.frame_reference, int(event.remembered),
                ),
            )
        return event

    def get_event(self, event_id: str) -> VisualEvent | None:
        with self._lock:
            row = self.conn.execute(
                "SELECT event_json FROM vision_events WHERE event_id=?", (event_id,)
            ).fetchone()
        return VisualEvent.model_validate(_load(row["event_json"], {})) if row else None

    def list_events(
        self,
        *,
        source_id: str | None = None,
        limit: int = 100,
        since: datetime | None = None,
    ) -> list[VisualEvent]:
        clauses: list[str] = []
        params: list[Any] = []
        if source_id:
            clauses.append("source_id=?")
            params.append(source_id)
        if since:
            clauses.append("occurred_at>=?")
            params.append(since.isoformat())
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._lock:
            rows = self.conn.execute(
                f"SELECT event_json FROM vision_events{where} ORDER BY occurred_at DESC LIMIT ?",
                (*params, max(1, min(int(limit), 1000))),
            ).fetchall()
        return [VisualEvent.model_validate(_load(row["event_json"], {})) for row in rows]

    def event_ids_since(
        self,
        since: datetime,
        *,
        source_id: str | None = None,
    ) -> list[str]:
        """Return every matching event ID for an intentional erase operation.

        ``list_events`` is deliberately bounded for ordinary UI/tool reads.
        Privacy erasure must not inherit that presentation limit, otherwise a
        busy source could leave older matching events behind.
        """

        clauses = ["occurred_at>=?"]
        params: list[Any] = [since.astimezone(timezone.utc).isoformat()]
        if source_id:
            clauses.append("source_id=?")
            params.append(source_id)
        with self._lock:
            rows = self.conn.execute(
                f"SELECT event_id FROM vision_events WHERE {' AND '.join(clauses)} ORDER BY occurred_at DESC",
                params,
            ).fetchall()
        return [str(row["event_id"]) for row in rows]

    def mark_event_remembered(self, event_id: str, remembered: bool = True) -> bool:
        event = self.get_event(event_id)
        if event is None:
            return False
        event.remembered = bool(remembered)
        self.save_event(event)
        return True

    # -- Watches --------------------------------------------------------------

    def save_watch(self, watch: VisionWatch) -> VisionWatch:
        data = watch.model_dump(mode="json")
        now = utc_now()
        expires_at = watch.expires_at.isoformat() if watch.expires_at else None
        with self.transaction() as conn:
            conn.execute(
                """INSERT INTO vision_watches
                   (watch_id, source_id, status, expires_at, watch_json, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(watch_id) DO UPDATE SET
                     status=excluded.status, expires_at=excluded.expires_at,
                     watch_json=excluded.watch_json, updated_at=excluded.updated_at""",
                (watch.watch_id, watch.source_id, str(getattr(watch.status, "value", watch.status)),
                 expires_at, _dump(data), now, now),
            )
        return watch

    def get_watch(self, watch_id: str) -> VisionWatch | None:
        with self._lock:
            row = self.conn.execute(
                "SELECT watch_json FROM vision_watches WHERE watch_id=?", (watch_id,)
            ).fetchone()
        return VisionWatch.model_validate(_load(row["watch_json"], {})) if row else None

    def list_watches(
        self, *, source_id: str | None = None, status: str | None = None, limit: int = 100,
    ) -> list[VisionWatch]:
        clauses: list[str] = []
        params: list[Any] = []
        if source_id:
            clauses.append("source_id=?")
            params.append(source_id)
        if status:
            clauses.append("status=?")
            params.append(status)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._lock:
            rows = self.conn.execute(
                f"SELECT watch_json FROM vision_watches{where} ORDER BY updated_at DESC LIMIT ?",
                (*params, max(1, min(int(limit), 1000))),
            ).fetchall()
        return [VisionWatch.model_validate(_load(row["watch_json"], {})) for row in rows]

    def delete_watch(self, watch_id: str) -> bool:
        with self.transaction() as conn:
            return bool(conn.execute("DELETE FROM vision_watches WHERE watch_id=?", (watch_id,)).rowcount)

    # -- Memory/frame lifecycle ----------------------------------------------

    def record_memory_link(self, event_id: str, fact_id: int, frame_reference: str | None = None) -> None:
        with self.transaction() as conn:
            conn.execute(
                """INSERT INTO vision_memory_links(event_id, fact_id, frame_reference, created_at)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(event_id, fact_id) DO UPDATE SET frame_reference=excluded.frame_reference""",
                (event_id, int(fact_id), frame_reference, utc_now()),
            )
        self.mark_event_remembered(event_id, True)

    def frame_references_for_memory(self, fact_id: int) -> list[str]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT frame_reference FROM vision_memory_links WHERE fact_id=?", (int(fact_id),)
            ).fetchall()
        return [str(row["frame_reference"]) for row in rows if row["frame_reference"]]

    def memory_fact_ids_for_event(self, event_id: str) -> list[int]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT fact_id FROM vision_memory_links WHERE event_id=?", (event_id,)
            ).fetchall()
        return [int(row["fact_id"]) for row in rows]

    def memory_fact_ids_for_source(self, source_id: str) -> list[int]:
        """Find ordinary memories derived from one source before erasing it."""

        with self._lock:
            rows = self.conn.execute(
                """SELECT DISTINCT link.fact_id
                   FROM vision_memory_links AS link
                   INNER JOIN vision_events AS event ON event.event_id=link.event_id
                   WHERE event.source_id=?""",
                (source_id,),
            ).fetchall()
        return [int(row["fact_id"]) for row in rows]

    def delete_frame_references_for_memory(self, fact_id: int) -> list[str]:
        """Delete only a retained frame while preserving its text memory."""

        references = self.frame_references_for_memory(fact_id)
        with self.transaction() as conn:
            conn.execute(
                "UPDATE vision_memory_links SET frame_reference=NULL WHERE fact_id=?",
                (int(fact_id),),
            )
        for reference in references:
            self._delete_artifact(reference)
        return references

    def delete_memory_links(self, fact_id: int, *, delete_frames: bool = True) -> list[str]:
        references = self.frame_references_for_memory(fact_id)
        with self.transaction() as conn:
            rows = conn.execute(
                "SELECT event_id FROM vision_memory_links WHERE fact_id=?", (int(fact_id),)
            ).fetchall()
            conn.execute("DELETE FROM vision_memory_links WHERE fact_id=?", (int(fact_id),))
        for row in rows:
            self.mark_event_remembered(str(row["event_id"]), False)
        if delete_frames:
            for reference in references:
                self._delete_artifact(reference)
        return references

    def delete_event(self, event_id: str, *, delete_frame: bool = True) -> bool:
        event = self.get_event(event_id)
        if event is None:
            return False
        with self.transaction() as conn:
            refs = [
                str(row["frame_reference"]) for row in conn.execute(
                    "SELECT frame_reference FROM vision_memory_links WHERE event_id=?", (event_id,)
                ).fetchall() if row["frame_reference"]
            ]
            conn.execute("DELETE FROM vision_memory_links WHERE event_id=?", (event_id,))
            deleted = bool(conn.execute("DELETE FROM vision_events WHERE event_id=?", (event_id,)).rowcount)
        if delete_frame:
            for reference in set(refs + ([event.frame_reference] if event.frame_reference else [])):
                self._delete_artifact(reference)
        return deleted

    def erase_events_since(self, since: datetime, *, source_id: str | None = None) -> int:
        events = self.list_events(source_id=source_id, limit=1000, since=since)
        return sum(1 for event in events if self.delete_event(event.event_id, delete_frame=True))

    def expire_frame_references(self, before: datetime) -> int:
        """Remove expired event artifacts without deleting their metadata/facts."""

        cutoff = before.astimezone(timezone.utc).isoformat()
        with self._lock:
            rows = self.conn.execute(
                """SELECT event_id, event_json, frame_reference FROM vision_events
                   WHERE frame_reference IS NOT NULL AND occurred_at<=?""",
                (cutoff,),
            ).fetchall()
        if not rows:
            return 0
        references: list[str] = []
        with self.transaction() as conn:
            for row in rows:
                event = VisualEvent.model_validate(_load(row["event_json"], {}))
                if event.frame_reference:
                    references.append(event.frame_reference)
                event.frame_reference = None
                link_rows = conn.execute(
                    "SELECT frame_reference FROM vision_memory_links WHERE event_id=? AND frame_reference IS NOT NULL",
                    (event.event_id,),
                ).fetchall()
                references.extend(str(item["frame_reference"]) for item in link_rows)
                conn.execute(
                    "UPDATE vision_events SET event_json=?, frame_reference=NULL WHERE event_id=?",
                    (_dump(event.model_dump(mode="json")), event.event_id),
                )
                conn.execute(
                    "UPDATE vision_memory_links SET frame_reference=NULL WHERE event_id=?",
                    (event.event_id,),
                )
        for reference in set(references):
            self._delete_artifact(reference)
        return len(rows)

    def _delete_artifact(self, reference: str) -> bool:
        """Delete only owned vision artifacts; never erase an arbitrary source file."""
        try:
            candidate = Path(reference).expanduser().resolve(strict=False)
            root = self.artifact_root.resolve(strict=False)
            if not candidate.is_relative_to(root) or not candidate.is_file():
                return False
            candidate.unlink(missing_ok=True)
            return True
        except (OSError, RuntimeError, ValueError):
            return False


__all__ = ["VisionStore", "utc_now"]
