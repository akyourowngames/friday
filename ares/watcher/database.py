"""SQLite persistence for watcher configuration, history, and telemetry."""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

from ares.watcher.models import CheckRun, Event, Monitor, Notification, Snapshot, utc_now


def resolve_watcher_database_path(config: Any) -> Path:
    """Keep custom and test Ares data roots self-contained for the default path."""
    configured = Path(config.watcher.database_path).expanduser()
    normal_default = Path("~/.ares/data/watchers.db").expanduser()
    data_dir = Path(config.data_dir).expanduser()
    if configured == normal_default and data_dir != normal_default.parent:
        return data_dir / "watchers.db"
    return configured


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS monitors (
 id TEXT PRIMARY KEY, name TEXT NOT NULL, type TEXT NOT NULL, url TEXT,
 config TEXT NOT NULL DEFAULT '{}', interval_seconds INTEGER NOT NULL DEFAULT 900,
 ai_action TEXT NOT NULL DEFAULT 'notify', ai_prompt TEXT, enabled INTEGER NOT NULL DEFAULT 1,
 last_checked_at TEXT, next_check_at TEXT, last_status TEXT, error_count INTEGER NOT NULL DEFAULT 0,
 total_checks INTEGER NOT NULL DEFAULT 0, total_changes INTEGER NOT NULL DEFAULT 0,
 last_duration_ms INTEGER, last_error TEXT, lease_owner TEXT, lease_expires_at TEXT,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS snapshots (
 id TEXT PRIMARY KEY, monitor_id TEXT NOT NULL REFERENCES monitors(id) ON DELETE CASCADE,
 content_hash TEXT, content TEXT, price_value REAL, metadata TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
 id TEXT PRIMARY KEY, monitor_id TEXT NOT NULL REFERENCES monitors(id) ON DELETE CASCADE,
 event_type TEXT NOT NULL, old_value TEXT, new_value TEXT, change_summary TEXT,
 severity TEXT NOT NULL DEFAULT 'info', notified INTEGER NOT NULL DEFAULT 0,
 acknowledged INTEGER NOT NULL DEFAULT 0, ai_analyzed INTEGER NOT NULL DEFAULT 0,
 ai_summary TEXT, confidence REAL NOT NULL DEFAULT 1.0, change_percent REAL,
 suppressed INTEGER NOT NULL DEFAULT 0, suppression_reason TEXT, feedback TEXT,
 feedback_note TEXT, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS notifications (
 id TEXT PRIMARY KEY, event_id TEXT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
 channel TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
 next_retry_at TEXT, sent_at TEXT, error TEXT
);
CREATE TABLE IF NOT EXISTS instagram_state (
 id TEXT PRIMARY KEY, monitor_id TEXT NOT NULL REFERENCES monitors(id) ON DELETE CASCADE,
 last_dm_id TEXT, last_mention_id TEXT, last_check_at TEXT
);
CREATE TABLE IF NOT EXISTS check_runs (
 id TEXT PRIMARY KEY, monitor_id TEXT NOT NULL REFERENCES monitors(id) ON DELETE CASCADE,
 status TEXT NOT NULL, started_at TEXT NOT NULL, finished_at TEXT NOT NULL,
 duration_ms INTEGER NOT NULL, changed INTEGER NOT NULL DEFAULT 0, http_status INTEGER,
 bytes_received INTEGER NOT NULL DEFAULT 0, error TEXT
);
CREATE INDEX IF NOT EXISTS idx_monitors_due ON monitors(enabled, next_check_at);
CREATE INDEX IF NOT EXISTS idx_snapshots_monitor ON snapshots(monitor_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_events_monitor ON events(monitor_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_events_queue ON events(notified, created_at);
CREATE INDEX IF NOT EXISTS idx_notifications_queue ON notifications(status, next_retry_at);
CREATE INDEX IF NOT EXISTS idx_check_runs_monitor ON check_runs(monitor_id, started_at DESC);
"""


def _dt(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


class WatcherDatabase:
    """Thread-safe local watcher store with WAL and explicit transactions."""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = str(Path(db_path).expanduser())
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(self.db_path, timeout=15, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.execute("PRAGMA busy_timeout=15000")
        with self._lock:
            self.conn.executescript(SCHEMA_SQL)
            self._migrate_schema()
            self.conn.commit()

    def _migrate_schema(self) -> None:
        """Add columns introduced after the original design without data loss."""
        additions = {
            "monitors": {
                "next_check_at":"TEXT", "total_checks":"INTEGER NOT NULL DEFAULT 0", "total_changes":"INTEGER NOT NULL DEFAULT 0",
                "last_duration_ms":"INTEGER", "last_error":"TEXT",
                "lease_owner":"TEXT", "lease_expires_at":"TEXT",
            },
            "events": {
                "acknowledged":"INTEGER NOT NULL DEFAULT 0", "ai_summary":"TEXT",
                "confidence":"REAL NOT NULL DEFAULT 1.0", "change_percent":"REAL",
                "suppressed":"INTEGER NOT NULL DEFAULT 0", "suppression_reason":"TEXT",
                "feedback":"TEXT", "feedback_note":"TEXT",
            },
            "notifications": {"attempts":"INTEGER NOT NULL DEFAULT 0", "next_retry_at":"TEXT"},
        }
        for table, columns in additions.items():
            existing = {row["name"] for row in self.conn.execute(f"PRAGMA table_info({table})")}
            for name, definition in columns.items():
                if name not in existing:
                    self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            try:
                yield self.conn
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                raise

    def close(self) -> None:
        with self._lock:
            self.conn.close()

    def insert_monitor(self, monitor: Monitor) -> None:
        values = self._monitor_values(monitor)
        with self.transaction() as conn:
            conn.execute("""INSERT INTO monitors
             (id,name,type,url,config,interval_seconds,ai_action,ai_prompt,enabled,last_checked_at,next_check_at,
              last_status,error_count,total_checks,total_changes,last_duration_ms,last_error,created_at,updated_at)
             VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", values)

    create_monitor = insert_monitor

    def update_monitor(self, monitor: Monitor) -> None:
        monitor.updated_at = utc_now()
        values = self._monitor_values(monitor)
        with self.transaction() as conn:
            cursor = conn.execute("""UPDATE monitors SET name=?,type=?,url=?,config=?,interval_seconds=?,ai_action=?,
             ai_prompt=?,enabled=?,last_checked_at=?,next_check_at=?,last_status=?,error_count=?,total_checks=?,
             total_changes=?,last_duration_ms=?,last_error=?,created_at=?,updated_at=?,lease_owner=NULL,lease_expires_at=NULL WHERE id=?""", values[1:] + values[:1])
            if cursor.rowcount == 0:
                raise KeyError(f"Unknown monitor: {monitor.id}")

    def get_monitor(self, monitor_id: str) -> Monitor | None:
        with self._lock:
            row = self.conn.execute("SELECT * FROM monitors WHERE id=?", (monitor_id,)).fetchone()
        return self._row_monitor(row) if row else None

    def list_monitors(self, enabled_only: bool = False) -> list[Monitor]:
        sql = "SELECT * FROM monitors" + (" WHERE enabled=1" if enabled_only else "") + " ORDER BY created_at DESC"
        with self._lock:
            rows = self.conn.execute(sql).fetchall()
        return [self._row_monitor(row) for row in rows]

    def list_due_monitors(self, now: datetime | None = None, limit: int = 100) -> list[Monitor]:
        stamp = (now or utc_now()).isoformat()
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM monitors WHERE enabled=1 AND (next_check_at IS NULL OR next_check_at<=?) ORDER BY COALESCE(next_check_at,created_at) LIMIT ?",
                (stamp, limit),
            ).fetchall()
        return [self._row_monitor(row) for row in rows]

    def claim_due_monitors(self, owner: str, *, now: datetime | None = None, limit: int = 100, lease_seconds: int = 180) -> list[Monitor]:
        """Atomically lease due work so multiple Ares surfaces never double-check it."""
        stamp = now or utc_now()
        expires = (stamp + timedelta(seconds=lease_seconds)).isoformat()
        with self.transaction() as conn:
            rows = conn.execute("""SELECT * FROM monitors WHERE enabled=1
                AND (next_check_at IS NULL OR next_check_at<=?)
                AND (lease_expires_at IS NULL OR lease_expires_at<=?)
                ORDER BY COALESCE(next_check_at,created_at) LIMIT ?""", (stamp.isoformat(), stamp.isoformat(), limit)).fetchall()
            ids = [row["id"] for row in rows]
            if ids:
                placeholders = ",".join("?" for _ in ids)
                conn.execute(f"UPDATE monitors SET lease_owner=?,lease_expires_at=? WHERE id IN ({placeholders})", (owner, expires, *ids))
        return [self._row_monitor(row) for row in rows]

    def delete_monitor(self, monitor_id: str) -> bool:
        with self.transaction() as conn:
            return conn.execute("DELETE FROM monitors WHERE id=?", (monitor_id,)).rowcount > 0

    def insert_snapshot(self, snapshot: Snapshot, retain: int = 30) -> None:
        with self.transaction() as conn:
            conn.execute("INSERT INTO snapshots VALUES (?,?,?,?,?,?,?)", (
                snapshot.id, snapshot.monitor_id, snapshot.content_hash, snapshot.content, snapshot.price_value,
                _dump(snapshot.metadata), snapshot.created_at.isoformat(),
            ))
            if retain > 0:
                conn.execute("""DELETE FROM snapshots WHERE monitor_id=? AND id NOT IN
                  (SELECT id FROM snapshots WHERE monitor_id=? ORDER BY created_at DESC LIMIT ?)""",
                  (snapshot.monitor_id, snapshot.monitor_id, retain))

    def get_latest_snapshot(self, monitor_id: str) -> Snapshot | None:
        with self._lock:
            row = self.conn.execute("SELECT * FROM snapshots WHERE monitor_id=? ORDER BY created_at DESC LIMIT 1", (monitor_id,)).fetchone()
        return self._row_snapshot(row) if row else None

    def insert_event(self, event: Event) -> None:
        with self.transaction() as conn:
            conn.execute("""INSERT INTO events
             (id,monitor_id,event_type,old_value,new_value,change_summary,severity,notified,acknowledged,
              ai_analyzed,ai_summary,confidence,change_percent,suppressed,suppression_reason,feedback,feedback_note,created_at)
             VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
                event.id, event.monitor_id, event.event_type, event.old_value, event.new_value,
                event.change_summary, event.severity, int(event.notified), int(event.acknowledged),
                int(event.ai_analyzed), event.ai_summary, event.confidence, event.change_percent,
                int(event.suppressed), event.suppression_reason, event.feedback, event.feedback_note,
                event.created_at.isoformat(),
            ))

    def get_event(self, event_id: str) -> Event | None:
        with self._lock:
            row = self.conn.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone()
        return self._row_event(row) if row else None

    def list_events(self, monitor_id: str | None = None, *, limit: int = 100, severity: str | None = None, unacknowledged: bool = False) -> list[Event]:
        clauses, params = [], []
        if monitor_id:
            clauses.append("monitor_id=?")
            params.append(monitor_id)
        if severity:
            clauses.append("severity=?")
            params.append(severity)
        if unacknowledged:
            clauses.append("acknowledged=0")
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._lock:
            rows = self.conn.execute(f"SELECT * FROM events{where} ORDER BY created_at DESC LIMIT ?", (*params, limit)).fetchall()
        return [self._row_event(row) for row in rows]

    def get_unnotified_events(self, limit: int = 100) -> list[Event]:
        with self._lock:
            rows = self.conn.execute("SELECT * FROM events WHERE notified=0 ORDER BY created_at LIMIT ?", (limit,)).fetchall()
        return [self._row_event(row) for row in rows]

    def mark_event_notified(self, event_id: str) -> None:
        with self.transaction() as conn:
            conn.execute("UPDATE events SET notified=1 WHERE id=?", (event_id,))

    def acknowledge_event(self, event_id: str, acknowledged: bool = True, *, feedback: str | None = None, feedback_note: str | None = None) -> bool:
        with self.transaction() as conn:
            return conn.execute(
                "UPDATE events SET acknowledged=?,feedback=COALESCE(?,feedback),feedback_note=COALESCE(?,feedback_note) WHERE id=?",
                (int(acknowledged), feedback, feedback_note, event_id),
            ).rowcount > 0

    def update_event_analysis(self, event_id: str, summary: str) -> None:
        with self.transaction() as conn:
            conn.execute("UPDATE events SET ai_analyzed=1, ai_summary=? WHERE id=?", (summary, event_id))

    def insert_notification(self, notification: Notification) -> None:
        with self.transaction() as conn:
            conn.execute("INSERT INTO notifications VALUES (?,?,?,?,?,?,?,?)", (
                notification.id, notification.event_id, notification.channel, notification.status,
                notification.attempts, notification.next_retry_at.isoformat() if notification.next_retry_at else None,
                notification.sent_at.isoformat() if notification.sent_at else None, notification.error,
            ))

    def update_notification(self, notification: Notification) -> None:
        with self.transaction() as conn:
            conn.execute("""UPDATE notifications SET status=?,attempts=?,next_retry_at=?,sent_at=?,error=? WHERE id=?""", (
                notification.status, notification.attempts,
                notification.next_retry_at.isoformat() if notification.next_retry_at else None,
                notification.sent_at.isoformat() if notification.sent_at else None,
                notification.error, notification.id,
            ))

    def list_notifications(self, *, event_id: str | None = None, limit: int = 100) -> list[Notification]:
        query = "SELECT * FROM notifications"
        params: tuple[Any, ...] = ()
        if event_id:
            query += " WHERE event_id=?"
            params = (event_id,)
        with self._lock:
            rows = self.conn.execute(query + " ORDER BY rowid DESC LIMIT ?", (*params, limit)).fetchall()
        return [Notification(id=r["id"], event_id=r["event_id"], channel=r["channel"], status=r["status"], attempts=r["attempts"], next_retry_at=_dt(r["next_retry_at"]), sent_at=_dt(r["sent_at"]), error=r["error"]) for r in rows]

    def list_retryable_notifications(self, *, max_attempts: int = 4, limit: int = 50) -> list[Notification]:
        now = utc_now().isoformat()
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM notifications WHERE status='failed' AND attempts<? AND (next_retry_at IS NULL OR next_retry_at<=?) ORDER BY rowid LIMIT ?",
                (max_attempts, now, limit),
            ).fetchall()
        return [Notification(id=r["id"], event_id=r["event_id"], channel=r["channel"], status=r["status"], attempts=r["attempts"], next_retry_at=_dt(r["next_retry_at"]), sent_at=_dt(r["sent_at"]), error=r["error"]) for r in rows]

    def insert_check_run(self, run: CheckRun) -> None:
        with self.transaction() as conn:
            conn.execute("INSERT INTO check_runs VALUES (?,?,?,?,?,?,?,?,?,?)", (
                run.id, run.monitor_id, run.status, run.started_at.isoformat(), run.finished_at.isoformat(),
                run.duration_ms, int(run.changed), run.http_status, run.bytes_received, run.error,
            ))

    def list_check_runs(self, monitor_id: str | None = None, limit: int = 200) -> list[CheckRun]:
        query, params = "SELECT * FROM check_runs", []
        if monitor_id:
            query += " WHERE monitor_id=?"
            params.append(monitor_id)
        with self._lock:
            rows = self.conn.execute(query + " ORDER BY started_at DESC LIMIT ?", (*params, limit)).fetchall()
        return [CheckRun(id=r["id"], monitor_id=r["monitor_id"], status=r["status"], started_at=_dt(r["started_at"]), finished_at=_dt(r["finished_at"]), duration_ms=r["duration_ms"], changed=bool(r["changed"]), http_status=r["http_status"], bytes_received=r["bytes_received"], error=r["error"]) for r in rows]

    def overview(self) -> dict[str, Any]:
        with self._lock:
            counts = self.conn.execute("""SELECT COUNT(*) total, SUM(enabled) active,
             SUM(CASE WHEN enabled=0 THEN 1 ELSE 0 END) paused,
             SUM(CASE WHEN last_status IN ('error','timeout') THEN 1 ELSE 0 END) failing,
             SUM(total_checks) checks, SUM(total_changes) changes, AVG(last_duration_ms) avg_latency FROM monitors""").fetchone()
            alerts = self.conn.execute("SELECT COUNT(*) count FROM events WHERE acknowledged=0").fetchone()[0]
            delivery_failures = self.conn.execute("SELECT COUNT(*) FROM notifications WHERE status='failed'").fetchone()[0]
            checks_24h = self.conn.execute("SELECT COUNT(*), SUM(CASE WHEN status='ok' THEN 1 ELSE 0 END) FROM check_runs WHERE started_at>=datetime('now','-1 day')").fetchone()
        total_24h, ok_24h = checks_24h[0] or 0, checks_24h[1] or 0
        return {
            "monitors": counts["total"] or 0, "active": counts["active"] or 0, "paused": counts["paused"] or 0,
            "failing": counts["failing"] or 0, "unacknowledged_alerts": alerts, "delivery_failures": delivery_failures,
            "total_checks": counts["checks"] or 0, "total_changes": counts["changes"] or 0,
            "average_latency_ms": round(counts["avg_latency"] or 0),
            "checks_24h": total_24h, "success_rate_24h": round(ok_24h * 100 / total_24h, 2) if total_24h else 100.0,
        }

    @staticmethod
    def _monitor_values(m: Monitor) -> tuple[Any, ...]:
        return (m.id,m.name,m.type,m.url,_dump(m.config),m.interval_seconds,m.ai_action,m.ai_prompt,int(m.enabled),
            m.last_checked_at.isoformat() if m.last_checked_at else None,m.next_check_at.isoformat() if m.next_check_at else None,
            m.last_status,m.error_count,m.total_checks,m.total_changes,m.last_duration_ms,m.last_error,
            m.created_at.isoformat(),m.updated_at.isoformat())

    @staticmethod
    def _row_monitor(r: sqlite3.Row) -> Monitor:
        return Monitor(id=r["id"],name=r["name"],type=r["type"],url=r["url"],config=json.loads(r["config"] or "{}"),
            interval_seconds=r["interval_seconds"],ai_action=r["ai_action"],ai_prompt=r["ai_prompt"],enabled=bool(r["enabled"]),
            last_checked_at=_dt(r["last_checked_at"]),next_check_at=_dt(r["next_check_at"]),last_status=r["last_status"],
            error_count=r["error_count"],total_checks=r["total_checks"],total_changes=r["total_changes"],
            last_duration_ms=r["last_duration_ms"],last_error=r["last_error"],created_at=_dt(r["created_at"]),updated_at=_dt(r["updated_at"]))

    @staticmethod
    def _row_snapshot(r: sqlite3.Row) -> Snapshot:
        return Snapshot(id=r["id"],monitor_id=r["monitor_id"],content_hash=r["content_hash"],content=r["content"],
            price_value=r["price_value"],metadata=json.loads(r["metadata"] or "{}"),created_at=_dt(r["created_at"]))

    @staticmethod
    def _row_event(r: sqlite3.Row) -> Event:
        return Event(id=r["id"],monitor_id=r["monitor_id"],event_type=r["event_type"],old_value=r["old_value"],
            new_value=r["new_value"],change_summary=r["change_summary"],severity=r["severity"],notified=bool(r["notified"]),
            acknowledged=bool(r["acknowledged"]),ai_analyzed=bool(r["ai_analyzed"]),ai_summary=r["ai_summary"],
            confidence=float(r["confidence"] if r["confidence"] is not None else 1.0), change_percent=r["change_percent"],
            suppressed=bool(r["suppressed"]), suppression_reason=r["suppression_reason"],
            feedback=r["feedback"], feedback_note=r["feedback_note"], created_at=_dt(r["created_at"]))
