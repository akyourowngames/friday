"""Encrypted contact and durable call-session storage."""

from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from cryptography.fernet import Fernet, InvalidToken

from ares.telephony.models import CallContact, CallDirection, CallSession, CallStatus


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


class PhoneCipher:
    """Encrypt local contact numbers with a key kept outside SQLite."""

    def __init__(self, key_path: Path) -> None:
        key_path.parent.mkdir(parents=True, exist_ok=True)
        if not key_path.exists():
            key_path.write_bytes(Fernet.generate_key())
        self._fernet = Fernet(key_path.read_bytes())

    def encrypt(self, value: str) -> str:
        return self._fernet.encrypt(value.encode("utf-8")).decode("ascii")

    def decrypt(self, value: str) -> str:
        try:
            return self._fernet.decrypt(value.encode("ascii")).decode("utf-8")
        except (InvalidToken, UnicodeError) as exc:
            raise ValueError("Unable to decrypt this local telephony contact.") from exc


class TelephonyStore:
    """SQLite persistence for contacts, calls, and append-only transcripts."""

    def __init__(self, db_path: str | Path, *, connection: sqlite3.Connection | None = None, data_dir: str | Path | None = None) -> None:
        self.db_path = Path(db_path)
        self._owns_connection = connection is None
        self.conn = connection or sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        root = Path(data_dir) if data_dir is not None else self.db_path.parent
        self.cipher = PhoneCipher(root / "telephony.key")
        self._create_tables()

    def close(self) -> None:
        if self._owns_connection:
            self.conn.close()

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        try:
            yield
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    def _create_tables(self) -> None:
        with self._transaction():
            self.conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS telephony_contacts (
                    contact_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    normalized_name TEXT NOT NULL UNIQUE,
                    nickname TEXT NOT NULL DEFAULT '',
                    normalized_nickname TEXT NOT NULL DEFAULT '',
                    phone_number_encrypted TEXT NOT NULL,
                    notes TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_telephony_contacts_nickname
                    ON telephony_contacts(normalized_nickname);
                CREATE TABLE IF NOT EXISTS telephony_calls (
                    call_id TEXT PRIMARY KEY,
                    call_sid TEXT UNIQUE,
                    caller TEXT NOT NULL,
                    callee TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    status TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    answered_at TEXT,
                    ended_at TEXT,
                    duration_seconds INTEGER,
                    summary TEXT NOT NULL DEFAULT '',
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    conversation_id INTEGER,
                    error TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS idx_telephony_calls_started ON telephony_calls(started_at DESC);
                CREATE TABLE IF NOT EXISTS telephony_transcript (
                    transcript_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    call_id TEXT NOT NULL REFERENCES telephony_calls(call_id) ON DELETE CASCADE,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    metadata_json TEXT NOT NULL DEFAULT '{}'
                );
                CREATE INDEX IF NOT EXISTS idx_telephony_transcript_call ON telephony_transcript(call_id, transcript_id);
                CREATE TABLE IF NOT EXISTS telephony_events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    call_id TEXT NOT NULL REFERENCES telephony_calls(call_id) ON DELETE CASCADE,
                    event_type TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    payload_json TEXT NOT NULL DEFAULT '{}'
                );
                CREATE INDEX IF NOT EXISTS idx_telephony_events_call ON telephony_events(call_id, event_id);
                """
            )

    @staticmethod
    def _normalize(value: str) -> str:
        return " ".join(str(value or "").casefold().split())

    def upsert_contact(self, name: str, phone_number: str, *, nickname: str = "", notes: str = "") -> CallContact:
        clean_name = " ".join(str(name or "").split())
        clean_number = "".join(str(phone_number or "").split())
        if not clean_name or not clean_number:
            raise ValueError("Contact name and phone number are required.")
        now = utc_now()
        normalized_name = self._normalize(clean_name)
        with self._transaction():
            existing = self.conn.execute("SELECT contact_id FROM telephony_contacts WHERE normalized_name = ?", (normalized_name,)).fetchone()
            encrypted = self.cipher.encrypt(clean_number)
            if existing:
                self.conn.execute(
                    """UPDATE telephony_contacts SET name=?, nickname=?, normalized_nickname=?,
                       phone_number_encrypted=?, notes=?, updated_at=? WHERE contact_id=?""",
                    (clean_name, nickname.strip(), self._normalize(nickname), encrypted, notes.strip(), now, existing["contact_id"]),
                )
                contact_id = int(existing["contact_id"])
            else:
                cursor = self.conn.execute(
                    """INSERT INTO telephony_contacts
                       (name, normalized_name, nickname, normalized_nickname, phone_number_encrypted, notes, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (clean_name, normalized_name, nickname.strip(), self._normalize(nickname), encrypted, notes.strip(), now, now),
                )
                contact_id = int(cursor.lastrowid)
        return self.get_contact(contact_id)  # type: ignore[return-value]

    def get_contact(self, contact_id: int) -> CallContact | None:
        row = self.conn.execute("SELECT * FROM telephony_contacts WHERE contact_id = ?", (int(contact_id),)).fetchone()
        return self._contact_from_row(row) if row else None

    def find_contact(self, reference: str) -> CallContact | None:
        normal = self._normalize(reference)
        if not normal:
            return None
        rows = self.conn.execute(
            """SELECT * FROM telephony_contacts WHERE normalized_name = ? OR normalized_nickname = ?
               ORDER BY CASE WHEN normalized_name = ? THEN 0 ELSE 1 END, contact_id""",
            (normal, normal, normal),
        ).fetchall()
        if len(rows) != 1:
            return None
        return self._contact_from_row(rows[0])

    def list_contacts(self, limit: int = 100) -> list[CallContact]:
        rows = self.conn.execute("SELECT * FROM telephony_contacts ORDER BY name COLLATE NOCASE LIMIT ?", (max(1, min(int(limit), 500)),)).fetchall()
        return [self._contact_from_row(row) for row in rows]

    def _contact_from_row(self, row: sqlite3.Row) -> CallContact:
        return CallContact(
            contact_id=int(row["contact_id"]), name=row["name"], nickname=row["nickname"],
            phone_number=self.cipher.decrypt(row["phone_number_encrypted"]), notes=row["notes"],
            created_at=row["created_at"], updated_at=row["updated_at"],
        )

    def create_call(self, *, caller: str, callee: str, direction: CallDirection, status: CallStatus = CallStatus.QUEUED, metadata: dict[str, Any] | None = None, conversation_id: int | None = None) -> CallSession:
        call_id = f"call-{uuid.uuid4().hex}"
        started_at = utc_now()
        with self._transaction():
            self.conn.execute(
                """INSERT INTO telephony_calls
                   (call_id, caller, callee, direction, status, started_at, metadata_json, conversation_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (call_id, caller, callee, direction.value, status.value, started_at, json.dumps(metadata or {}, ensure_ascii=False), conversation_id),
            )
        return self.get_call(call_id)  # type: ignore[return-value]

    def get_call(self, call_id: str) -> CallSession | None:
        row = self.conn.execute("SELECT * FROM telephony_calls WHERE call_id = ?", (call_id,)).fetchone()
        return self._call_from_row(row) if row else None

    def get_call_by_sid(self, call_sid: str) -> CallSession | None:
        row = self.conn.execute("SELECT * FROM telephony_calls WHERE call_sid = ?", (call_sid,)).fetchone()
        return self._call_from_row(row) if row else None

    def list_calls(self, limit: int = 20) -> list[CallSession]:
        rows = self.conn.execute("SELECT * FROM telephony_calls ORDER BY started_at DESC LIMIT ?", (max(1, min(int(limit), 100)),)).fetchall()
        return [self._call_from_row(row, include_transcript=False) for row in rows]

    def update_call(self, call_id: str, *, call_sid: str | None = None, status: CallStatus | None = None, error: str | None = None, metadata: dict[str, Any] | None = None) -> CallSession | None:
        existing = self.get_call(call_id)
        if existing is None:
            return None
        next_status = status or existing.status
        now = utc_now()
        answered_at = existing.answered_at or (now if next_status == CallStatus.IN_PROGRESS else None)
        ended_at = existing.ended_at or (now if next_status.terminal else None)
        duration = existing.duration_seconds
        if ended_at:
            try:
                duration = max(0, int((datetime.fromisoformat(ended_at.replace("Z", "+00:00")) - datetime.fromisoformat(existing.started_at.replace("Z", "+00:00"))).total_seconds()))
            except ValueError:
                duration = None
        merged_metadata = {**existing.metadata, **(metadata or {})}
        with self._transaction():
            self.conn.execute(
                """UPDATE telephony_calls SET call_sid=COALESCE(?, call_sid), status=?, answered_at=?, ended_at=?,
                   duration_seconds=?, metadata_json=?, error=? WHERE call_id=?""",
                (call_sid, next_status.value, answered_at, ended_at, duration, json.dumps(merged_metadata, ensure_ascii=False), error if error is not None else existing.error, call_id),
            )
        return self.get_call(call_id)

    def append_transcript(self, call_id: str, role: str, content: str, *, metadata: dict[str, Any] | None = None) -> None:
        if not str(content or "").strip():
            return
        with self._transaction():
            self.conn.execute(
                "INSERT INTO telephony_transcript (call_id, role, content, timestamp, metadata_json) VALUES (?, ?, ?, ?, ?)",
                (call_id, role, str(content).strip(), utc_now(), json.dumps(metadata or {}, ensure_ascii=False)),
            )

    def record_event(self, call_id: str, event_type: str, *, payload: dict[str, Any] | None = None) -> None:
        with self._transaction():
            self.conn.execute(
                "INSERT INTO telephony_events (call_id, event_type, timestamp, payload_json) VALUES (?, ?, ?, ?)",
                (call_id, event_type, utc_now(), json.dumps(payload or {}, ensure_ascii=False)),
            )

    def list_events(self, call_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute("SELECT event_type, timestamp, payload_json FROM telephony_events WHERE call_id=? ORDER BY event_id", (call_id,)).fetchall()
        return [{"event_type": row["event_type"], "timestamp": row["timestamp"], "payload": json.loads(row["payload_json"] or "{}")} for row in rows]

    def complete_call(self, call_id: str, summary: str) -> CallSession | None:
        call = self.update_call(call_id, status=CallStatus.COMPLETED)
        if call is None:
            return None
        with self._transaction():
            self.conn.execute("UPDATE telephony_calls SET summary=? WHERE call_id=?", (summary.strip(), call_id))
        return self.get_call(call_id)

    def _call_from_row(self, row: sqlite3.Row, *, include_transcript: bool = True) -> CallSession:
        transcript: list[dict[str, Any]] = []
        if include_transcript:
            transcript = [dict(item) for item in self.conn.execute("SELECT role, content, timestamp, metadata_json FROM telephony_transcript WHERE call_id=? ORDER BY transcript_id", (row["call_id"],)).fetchall()]
            for item in transcript:
                item["metadata"] = json.loads(item.pop("metadata_json") or "{}")
        return CallSession(
            call_id=row["call_id"], call_sid=row["call_sid"], caller=row["caller"], callee=row["callee"],
            direction=CallDirection(row["direction"]), status=CallStatus(row["status"]), started_at=row["started_at"],
            answered_at=row["answered_at"], ended_at=row["ended_at"], duration_seconds=row["duration_seconds"],
            transcript=transcript, summary=row["summary"], metadata=json.loads(row["metadata_json"] or "{}"),
            conversation_id=row["conversation_id"], error=row["error"],
        )
