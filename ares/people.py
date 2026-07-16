"""Structured, local-only relationship memory for people the user names explicitly.

``MemoryStore`` deliberately stores freeform facts about the user.  This module
is separate because contact details describe other people and need a stricter
data model, deterministic alias resolution, and an explicit mutation boundary.
No caller should populate this store from notifications, contacts, or messages.
"""

from __future__ import annotations

import json
import re
import sqlite3
from difflib import SequenceMatcher
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from ares.config import get_db_path
from ares.sqlite_utils import connect_sqlite


_UNSET = object()
_WHITESPACE = re.compile(r"\s+")
_TOKEN = re.compile(r"[^\w]+", re.UNICODE)
_SOURCES = {"manual", "ares-suggested", "import"}


class PersonConflictError(ValueError):
    """A canonical name or alias is already owned by another person."""


class PersonResolutionError(ValueError):
    """A workflow reference could not be resolved safely to one person."""


def utc_now() -> str:
    """Return a stable UTC timestamp suitable for lexical SQLite ordering."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def normalize_reference(value: str) -> str:
    """Normalize names/aliases without losing the original display spelling."""
    return _WHITESPACE.sub(" ", _TOKEN.sub(" ", str(value or "").casefold())).strip()


def mask_phone(value: str | None) -> str:
    """Return a useful but non-sensitive phone hint for model/tool output."""
    raw = str(value or "").strip()
    if not raw:
        return ""
    digits = re.sub(r"\D", "", raw)
    if len(digits) <= 2:
        return "••"
    return f"•••{digits[-4:]}" if len(digits) >= 4 else "•••"


def mask_email(value: str | None) -> str:
    """Return an email hint without leaking the mailbox local-part."""
    raw = str(value or "").strip()
    if not raw:
        return ""
    local, separator, domain = raw.partition("@")
    if not separator:
        return "•••"
    return f"{(local[:1] or '•')}•••@{domain}"


def _clean_text(value: Any, *, field: str, maximum: int, allow_empty: bool = True) -> str:
    text = _WHITESPACE.sub(" ", str(value or "")).strip()
    if not allow_empty and not text:
        raise ValueError(f"{field} is required")
    if len(text) > maximum:
        raise ValueError(f"{field} must be at most {maximum} characters")
    return text


def _normalize_aliases(canonical_name: str, aliases: list[str] | tuple[str, ...] | None) -> list[tuple[str, str]]:
    entries: list[tuple[str, str]] = []
    seen: set[str] = set()
    for value in [canonical_name, *(aliases or [])]:
        display = _clean_text(value, field="alias", maximum=160, allow_empty=False)
        normalized = normalize_reference(display)
        if not normalized:
            raise ValueError("alias must contain letters or numbers")
        if normalized not in seen:
            seen.add(normalized)
            entries.append((display, normalized))
    return entries


def _normalize_dates(value: dict[str, Any] | None) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError("important_dates must be an object of label to date")
    cleaned: dict[str, str] = {}
    for key, item in value.items():
        label = _clean_text(key, field="important date label", maximum=60, allow_empty=False)
        date_value = _clean_text(item, field=f"important date '{label}'", maximum=80, allow_empty=False)
        cleaned[label] = date_value
    return cleaned


class PeopleStore:
    """SQLite-backed people records with collision-safe alias resolution."""

    def __init__(self, db_path: str | Path | None = None, *, connection: sqlite3.Connection | None = None):
        self.db_path = Path(db_path) if db_path is not None else get_db_path()
        self._owns_connection = connection is None
        if connection is None:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            self.conn = connect_sqlite(self.db_path)
        else:
            self.conn = connection
        self._init_db()

    def _init_db(self) -> None:
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS people_meta (
                person_id            INTEGER PRIMARY KEY AUTOINCREMENT,
                canonical_name       TEXT NOT NULL,
                normalized_name      TEXT NOT NULL UNIQUE,
                relation             TEXT NOT NULL DEFAULT '',
                phone                TEXT,
                email                TEXT,
                important_dates_json TEXT NOT NULL DEFAULT '{}',
                notes                TEXT NOT NULL DEFAULT '',
                last_referenced_at   TEXT,
                last_contacted_at    TEXT,
                last_contacted_via   TEXT NOT NULL DEFAULT '',
                created_at           TEXT NOT NULL,
                updated_at           TEXT NOT NULL,
                source               TEXT NOT NULL DEFAULT 'manual',
                confidence           REAL NOT NULL DEFAULT 1.0,
                revision             INTEGER NOT NULL DEFAULT 1
            )
            """
        )
        self._ensure_metadata_columns()
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS person_aliases (
                person_id        INTEGER NOT NULL,
                alias            TEXT NOT NULL,
                alias_normalized TEXT NOT NULL UNIQUE,
                PRIMARY KEY (person_id, alias_normalized),
                FOREIGN KEY (person_id) REFERENCES people_meta(person_id) ON DELETE CASCADE
            )
            """
        )
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_people_recent ON people_meta(last_referenced_at DESC, created_at DESC)")
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_alias_person ON person_aliases(person_id)")
        self.conn.execute(
            """CREATE TABLE IF NOT EXISTS person_revisions (
                   revision_id INTEGER PRIMARY KEY AUTOINCREMENT,
                   person_id INTEGER NOT NULL,
                   revision INTEGER NOT NULL,
                   snapshot_json TEXT NOT NULL,
                   changed_fields_json TEXT NOT NULL DEFAULT '[]',
                   created_at TEXT NOT NULL,
                   UNIQUE(person_id, revision)
               )"""
        )
        self.conn.execute(
            """CREATE TABLE IF NOT EXISTS person_links (
                   person_id INTEGER NOT NULL,
                   entity_type TEXT NOT NULL,
                   entity_id TEXT NOT NULL,
                   created_at TEXT NOT NULL,
                   PRIMARY KEY(person_id, entity_type, entity_id)
               )"""
        )
        self.conn.execute(
            """CREATE TABLE IF NOT EXISTS person_timeline (
                   event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                   person_id INTEGER NOT NULL,
                   event_type TEXT NOT NULL,
                   event_date TEXT,
                   note TEXT NOT NULL DEFAULT '',
                   created_at TEXT NOT NULL
               )"""
        )
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_person_links_entity ON person_links(entity_type, entity_id)")
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_person_timeline_person ON person_timeline(person_id, event_date DESC, created_at DESC)")
        self.conn.commit()

    def _ensure_metadata_columns(self) -> None:
        """Migrate contact-status metadata without disturbing saved contacts."""
        columns = {
            "last_contacted_at": "TEXT",
            "last_contacted_via": "TEXT NOT NULL DEFAULT ''",
            "pronouns": "TEXT NOT NULL DEFAULT ''",
            "preferred_address": "TEXT NOT NULL DEFAULT ''",
            "timezone": "TEXT NOT NULL DEFAULT ''",
            "communication_preferences_json": "TEXT NOT NULL DEFAULT '{}'",
            "preferred_contact_method": "TEXT NOT NULL DEFAULT ''",
            "organization": "TEXT NOT NULL DEFAULT ''",
            "role": "TEXT NOT NULL DEFAULT ''",
            "interests_json": "TEXT NOT NULL DEFAULT '[]'",
            "reminder_preferences_json": "TEXT NOT NULL DEFAULT '{}'",
        }
        for name, definition in columns.items():
            try:
                self.conn.execute(f"SELECT {name} FROM people_meta LIMIT 1")
            except sqlite3.OperationalError:
                self.conn.execute(f"ALTER TABLE people_meta ADD COLUMN {name} {definition}")

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        """Take an immediate write transaction so aliases cannot race each other."""
        if self.conn.in_transaction:
            yield
            return
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self.conn.rollback()
            raise
        else:
            self.conn.commit()

    def _aliases_for(self, person_id: int, canonical_normalized: str) -> list[str]:
        rows = self.conn.execute(
            "SELECT alias, alias_normalized FROM person_aliases WHERE person_id = ? ORDER BY alias COLLATE NOCASE",
            (person_id,),
        ).fetchall()
        return [row["alias"] for row in rows if row["alias_normalized"] != canonical_normalized]

    def _row_to_person(self, row: sqlite3.Row, *, include_sensitive: bool = True) -> dict[str, Any]:
        try:
            important_dates = json.loads(row["important_dates_json"] or "{}")
        except (TypeError, json.JSONDecodeError):
            important_dates = {}
        def load_json_field(name: str, fallback: Any) -> Any:
            try:
                value = json.loads(row[name] or json.dumps(fallback))
            except (KeyError, TypeError, json.JSONDecodeError):
                value = fallback
            return value if isinstance(value, type(fallback)) else fallback
        person = {
            "person_id": int(row["person_id"]),
            "canonical_name": row["canonical_name"],
            "aliases": self._aliases_for(int(row["person_id"]), row["normalized_name"]),
            "relation": row["relation"] or "",
            "important_dates": important_dates if isinstance(important_dates, dict) else {},
            "notes": row["notes"] or "",
            "pronouns": row["pronouns"] or "",
            "preferred_address": row["preferred_address"] or "",
            "timezone": row["timezone"] or "",
            "communication_preferences": load_json_field("communication_preferences_json", {}),
            "preferred_contact_method": row["preferred_contact_method"] or "",
            "organization": row["organization"] or "",
            "role": row["role"] or "",
            "interests": load_json_field("interests_json", []),
            "reminder_preferences": load_json_field("reminder_preferences_json", {}),
            "last_referenced_at": row["last_referenced_at"],
            "last_contacted_at": row["last_contacted_at"],
            "last_contacted_via": row["last_contacted_via"] or "",
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "source": row["source"],
            "confidence": float(row["confidence"]),
            "revision": int(row["revision"]),
            "has_phone": bool(row["phone"]),
            "has_email": bool(row["email"]),
            "links": self.links_for(int(row["person_id"])),
            "timeline": self.timeline_for(int(row["person_id"]), limit=20),
        }
        if include_sensitive:
            person["phone"] = row["phone"] or ""
            person["email"] = row["email"] or ""
        else:
            person["phone_hint"] = mask_phone(row["phone"])
            person["email_hint"] = mask_email(row["email"])
        return person

    def links_for(self, person_id: int) -> dict[str, list[str]]:
        rows = self.conn.execute(
            "SELECT entity_type, entity_id FROM person_links WHERE person_id = ? ORDER BY entity_type, entity_id",
            (int(person_id),),
        ).fetchall()
        links: dict[str, list[str]] = {}
        for row in rows:
            links.setdefault(str(row["entity_type"]), []).append(str(row["entity_id"]))
        return links

    def timeline_for(self, person_id: int, *, limit: int = 20) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """SELECT event_id, event_type, event_date, note, created_at
               FROM person_timeline WHERE person_id = ?
               ORDER BY COALESCE(event_date, created_at) DESC, event_id DESC LIMIT ?""",
            (int(person_id), max(1, min(int(limit), 100))),
        ).fetchall()
        return [dict(row) for row in rows]

    def _replace_links(self, person_id: int, links: dict[str, object]) -> None:
        self.conn.execute("DELETE FROM person_links WHERE person_id = ?", (int(person_id),))
        for entity_type, raw_values in links.items():
            kind = str(entity_type or "").strip().casefold()
            if kind not in {"memory", "conversation", "action", "goal", "file"}:
                raise ValueError(f"Unsupported person link type: {entity_type}")
            values = raw_values if isinstance(raw_values, (list, tuple, set)) else [raw_values]
            for raw in values:
                entity_id = str(raw or "").strip()
                if entity_id:
                    self.conn.execute(
                        "INSERT OR IGNORE INTO person_links (person_id, entity_type, entity_id, created_at) VALUES (?, ?, ?, ?)",
                        (int(person_id), kind, entity_id[:500], utc_now()),
                    )

    def _append_timeline(self, person_id: int, events: list[dict[str, Any]] | None) -> None:
        for event in events or []:
            if not isinstance(event, dict):
                raise ValueError("timeline entries must be objects")
            event_type = _clean_text(event.get("type") or event.get("event_type"), field="timeline event type", maximum=80, allow_empty=False)
            event_date = _clean_text(event.get("date") or event.get("event_date"), field="timeline event date", maximum=80) or None
            note = _clean_text(event.get("note"), field="timeline note", maximum=2_000)
            self.conn.execute(
                "INSERT INTO person_timeline (person_id, event_type, event_date, note, created_at) VALUES (?, ?, ?, ?, ?)",
                (int(person_id), event_type, event_date, note, utc_now()),
            )

    def _get_row(self, person_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM people_meta WHERE person_id = ?", (int(person_id),)).fetchone()

    @staticmethod
    def _validate_contact(value: Any, *, field: str) -> str:
        text = _clean_text(value, field=field, maximum=254)
        if field == "email" and text and ("@" not in text or text.startswith("@") or text.endswith("@")):
            raise ValueError("email must be a valid address")
        return text

    @staticmethod
    def _validate_confidence(value: Any) -> float:
        try:
            confidence = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("confidence must be a number between 0 and 1") from exc
        if not 0.0 <= confidence <= 1.0:
            raise ValueError("confidence must be between 0 and 1")
        return confidence

    def _replace_aliases(self, person_id: int, aliases: list[tuple[str, str]]) -> None:
        self.conn.execute("DELETE FROM person_aliases WHERE person_id = ?", (person_id,))
        for alias, normalized in aliases:
            self.conn.execute(
                "INSERT INTO person_aliases (person_id, alias, alias_normalized) VALUES (?, ?, ?)",
                (person_id, alias, normalized),
            )

    def _contact_conflicts(self, *, phone: str | None, email: str | None, exclude_person_id: int | None = None) -> list[dict[str, Any]]:
        conflicts: list[dict[str, Any]] = []
        for field, value in (("phone", phone), ("email", email)):
            if not value:
                continue
            query = f"SELECT person_id, canonical_name FROM people_meta WHERE {field} = ?"
            params: list[Any] = [value]
            if exclude_person_id is not None:
                query += " AND person_id != ?"
                params.append(int(exclude_person_id))
            for row in self.conn.execute(query, params).fetchall():
                conflicts.append({"field": field, "person_id": int(row["person_id"]), "canonical_name": row["canonical_name"]})
        return conflicts

    def create(
        self,
        canonical_name: str,
        *,
        aliases: list[str] | tuple[str, ...] | None = None,
        relation: str = "",
        phone: str | None = None,
        email: str | None = None,
        important_dates: dict[str, Any] | None = None,
        notes: str = "",
        source: str = "manual",
        confidence: float = 1.0,
        pronouns: str = "",
        preferred_address: str = "",
        timezone: str = "",
        communication_preferences: dict[str, Any] | None = None,
        preferred_contact_method: str = "",
        organization: str = "",
        role: str = "",
        interests: list[str] | tuple[str, ...] | None = None,
        reminder_preferences: dict[str, Any] | None = None,
        links: dict[str, object] | None = None,
        timeline: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        name = _clean_text(canonical_name, field="canonical_name", maximum=160, allow_empty=False)
        normalized_name = normalize_reference(name)
        if not normalized_name:
            raise ValueError("canonical_name must contain letters or numbers")
        aliases_to_store = _normalize_aliases(name, aliases)
        source = str(source or "manual").strip().casefold()
        if source not in _SOURCES:
            raise ValueError(f"source must be one of: {', '.join(sorted(_SOURCES))}")
        contact_method = str(preferred_contact_method or "").strip().casefold()
        if contact_method not in {"", "email", "sms", "phone"}:
            raise ValueError("preferred_contact_method must be email, sms, phone, or empty")
        clean_phone = self._validate_contact(phone, field="phone") or None
        clean_email = self._validate_contact(email, field="email") or None
        conflicts = self._contact_conflicts(phone=clean_phone, email=clean_email)
        if conflicts:
            conflict = conflicts[0]
            raise PersonConflictError(
                f"{conflict['field']} is already saved for {conflict['canonical_name']} (person #{conflict['person_id']})."
            )
        now = utc_now()
        try:
            with self._transaction():
                cursor = self.conn.execute(
                    """
                    INSERT INTO people_meta (
                        canonical_name, normalized_name, relation, phone, email,
                        important_dates_json, notes, created_at, updated_at, source, confidence,
                        pronouns, preferred_address, timezone, communication_preferences_json,
                        preferred_contact_method, organization, role, interests_json,
                        reminder_preferences_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        name,
                        normalized_name,
                        _clean_text(relation, field="relation", maximum=160),
                        clean_phone,
                        clean_email,
                        json.dumps(_normalize_dates(important_dates), ensure_ascii=False, sort_keys=True),
                        _clean_text(notes, field="notes", maximum=4_000),
                        now,
                        now,
                        source,
                        self._validate_confidence(confidence),
                        _clean_text(pronouns, field="pronouns", maximum=80),
                        _clean_text(preferred_address, field="preferred_address", maximum=160),
                        _clean_text(timezone, field="timezone", maximum=100),
                        json.dumps(communication_preferences or {}, ensure_ascii=False, sort_keys=True),
                        contact_method,
                        _clean_text(organization, field="organization", maximum=240),
                        _clean_text(role, field="role", maximum=160),
                        json.dumps([_clean_text(item, field="interest", maximum=160, allow_empty=False) for item in (interests or [])], ensure_ascii=False),
                        json.dumps(reminder_preferences or {}, ensure_ascii=False, sort_keys=True),
                    ),
                )
                person_id = int(cursor.lastrowid)
                self._replace_aliases(person_id, aliases_to_store)
                self._replace_links(person_id, links or {})
                self._append_timeline(person_id, timeline)
        except sqlite3.IntegrityError as exc:
            raise PersonConflictError("A person already owns that canonical name or alias.") from exc
        return self.get(person_id) or {}

    def get(self, person_id: int, *, include_sensitive: bool = True) -> dict[str, Any] | None:
        row = self._get_row(int(person_id))
        return self._row_to_person(row, include_sensitive=include_sensitive) if row else None

    def _touch(self, person_ids: list[int]) -> None:
        if not person_ids:
            return
        unique_ids = list(dict.fromkeys(int(person_id) for person_id in person_ids))
        placeholders = ",".join("?" for _ in unique_ids)
        with self._transaction():
            self.conn.execute(
                f"UPDATE people_meta SET last_referenced_at = ? WHERE person_id IN ({placeholders})",
                [utc_now(), *unique_ids],
            )

    def search(self, query: str, *, limit: int = 5, include_sensitive: bool = True) -> list[dict[str, Any]]:
        """Search names, aliases, relation, and notes; exact aliases rank first."""
        text = _clean_text(query, field="query", maximum=300, allow_empty=False)
        bounded_limit = max(1, min(int(limit), 50))
        normalized = normalize_reference(text)
        rows: list[sqlite3.Row] = []
        if normalized:
            rows.extend(
                self.conn.execute(
                    """
                    SELECT p.* FROM people_meta AS p
                    JOIN person_aliases AS a ON a.person_id = p.person_id
                    WHERE a.alias_normalized = ?
                    ORDER BY p.updated_at DESC
                    """,
                    (normalized,),
                ).fetchall()
            )
        like = f"%{text}%"
        rows.extend(
            self.conn.execute(
                """
                SELECT * FROM people_meta
                WHERE canonical_name LIKE ? COLLATE NOCASE
                   OR relation LIKE ? COLLATE NOCASE
                   OR notes LIKE ? COLLATE NOCASE
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                (like, like, like, bounded_limit * 3),
            ).fetchall()
        )
        unique_rows: list[sqlite3.Row] = []
        seen: set[int] = set()
        for row in rows:
            person_id = int(row["person_id"])
            if person_id not in seen:
                seen.add(person_id)
                unique_rows.append(row)
            if len(unique_rows) >= bounded_limit:
                break
        self._touch([int(row["person_id"]) for row in unique_rows])
        return [self.get(int(row["person_id"]), include_sensitive=include_sensitive) or {} for row in unique_rows]

    def search_advanced(
        self,
        query: str,
        *,
        limit: int = 5,
        relation: str = "",
        channel: str = "",
        purpose: str = "",
        include_sensitive: bool = True,
    ) -> list[dict[str, Any]]:
        """Return fuzzy, recency-aware, action-aware person candidates."""
        text = _clean_text(query, field="query", maximum=300, allow_empty=False)
        normalized = normalize_reference(text)
        normalized_relation = normalize_reference(relation)
        requested_channel = str(channel or "").strip().casefold()
        if not requested_channel:
            requested_channel = "email" if "email" in str(purpose).casefold() or "invitation" in str(purpose).casefold() else (
                "sms" if "sms" in str(purpose).casefold() or "text" in str(purpose).casefold() else ""
            )
        if requested_channel not in {"", "email", "sms", "phone"}:
            raise ValueError("channel must be email, sms, phone, or empty")
        candidates: list[dict[str, Any]] = []
        for person in self.list_all(include_sensitive=True):
            if normalized_relation and normalized_relation not in normalize_reference(person.get("relation", "")):
                continue
            if requested_channel == "email" and not person.get("email"):
                continue
            if requested_channel in {"sms", "phone"} and not person.get("phone"):
                continue
            references = [person["canonical_name"], *person.get("aliases", [])]
            ratios = [SequenceMatcher(None, normalized, normalize_reference(reference)).ratio() for reference in references]
            score = max(ratios or [0.0])
            reason = "fuzzy name or alias match"
            if any(normalize_reference(reference) == normalized for reference in references):
                score, reason = 1.0, "exact canonical name or alias"
            elif any(normalized in normalize_reference(reference) or normalize_reference(reference) in normalized for reference in references):
                score, reason = max(score, 0.88), "partial canonical name or alias"
            searchable = " ".join(
                str(person.get(field) or "") for field in ("relation", "organization", "role", "notes")
            )
            if normalized and normalized in normalize_reference(searchable):
                score = max(score, 0.72)
                reason = "relationship, organization, role, or note match"
            if person.get("last_referenced_at") or person.get("last_contacted_at"):
                score = min(1.0, score + 0.03)
            if score < 0.35:
                continue
            view = person if include_sensitive else self.get(person["person_id"], include_sensitive=False) or {}
            view = dict(view)
            view["match_score"] = round(score, 3)
            view["match_reason"] = reason
            view["recommended_channel"] = requested_channel or person.get("preferred_contact_method") or (
                "email" if person.get("email") else ("sms" if person.get("phone") else "")
            )
            candidates.append(view)
        candidates.sort(
            key=lambda item: (
                float(item.get("match_score") or 0.0),
                str(item.get("last_referenced_at") or item.get("last_contacted_at") or item.get("updated_at") or ""),
            ),
            reverse=True,
        )
        selected = candidates[:max(1, min(int(limit), 50))]
        self._touch([int(item["person_id"]) for item in selected])
        return selected

    def revision_history(self, person_id: int) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT revision, snapshot_json, changed_fields_json, created_at FROM person_revisions WHERE person_id = ? ORDER BY revision DESC",
            (int(person_id),),
        ).fetchall()
        history: list[dict[str, Any]] = []
        current = self.get(int(person_id), include_sensitive=True)
        if current:
            history.append({"revision": current["revision"], "snapshot": current, "changed_fields": [], "created_at": current["updated_at"]})
        for row in rows:
            try:
                snapshot = json.loads(row["snapshot_json"])
                changed_fields = json.loads(row["changed_fields_json"])
            except (TypeError, json.JSONDecodeError):
                snapshot, changed_fields = {}, []
            history.append({
                "revision": int(row["revision"]), "snapshot": snapshot,
                "changed_fields": changed_fields, "created_at": row["created_at"],
            })
        return history

    def merge_people(
        self,
        target_id: int,
        duplicate_id: int,
        *,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        """Merge a duplicate into a target while preserving aliases, notes, and links."""
        if int(target_id) == int(duplicate_id):
            raise ValueError("target and duplicate person IDs must differ")
        target = self.get(int(target_id), include_sensitive=True)
        duplicate = self.get(int(duplicate_id), include_sensitive=True)
        if target is None or duplicate is None:
            raise ValueError("Target or duplicate person was not found.")
        if expected_revision is not None and int(target["revision"]) != int(expected_revision):
            raise PersonConflictError(
                f"Person #{target_id} changed since revision {expected_revision}; current revision is {target['revision']}."
            )
        phone = target.get("phone") or duplicate.get("phone")
        email = target.get("email") or duplicate.get("email")
        if target.get("phone") and duplicate.get("phone") and target["phone"] != duplicate["phone"]:
            raise PersonConflictError("Duplicate people have conflicting phone numbers; resolve the field explicitly before merging.")
        if target.get("email") and duplicate.get("email") and target["email"] != duplicate["email"]:
            raise PersonConflictError("Duplicate people have conflicting email addresses; resolve the field explicitly before merging.")
        aliases = list(dict.fromkeys([
            *target.get("aliases", []), duplicate["canonical_name"], *duplicate.get("aliases", []),
        ]))
        notes = "\n".join(part for part in (str(target.get("notes") or "").strip(), str(duplicate.get("notes") or "").strip()) if part)
        dates = {**duplicate.get("important_dates", {}), **target.get("important_dates", {})}
        interests = list(dict.fromkeys([*target.get("interests", []), *duplicate.get("interests", [])]))
        links: dict[str, list[str]] = {kind: list(values) for kind, values in target.get("links", {}).items()}
        for kind, values in duplicate.get("links", {}).items():
            links[kind] = list(dict.fromkeys([*links.get(kind, []), *values]))
        with self._transaction():
            # Free unique aliases and contacts while keeping the whole merge in
            # one transaction. The nested update joins this transaction.
            self.conn.execute("DELETE FROM person_aliases WHERE person_id = ?", (int(duplicate_id),))
            self.conn.execute(
                "UPDATE people_meta SET phone = NULL, email = NULL WHERE person_id = ?",
                (int(duplicate_id),),
            )
            merged = self.update(
                int(target_id), aliases=aliases, phone=phone, email=email, important_dates=dates,
                notes=notes, interests=interests, links=links, expected_revision=expected_revision,
                timeline=[{"type": "merged_duplicate", "note": f"Merged person #{duplicate_id}"}],
            )
            if merged is None:
                raise ValueError("Target person was not found.")
            self.conn.execute(
                "UPDATE person_timeline SET person_id = ? WHERE person_id = ?",
                (int(target_id), int(duplicate_id)),
            )
            self.conn.execute("DELETE FROM person_links WHERE person_id = ?", (int(duplicate_id),))
            self.conn.execute("DELETE FROM person_revisions WHERE person_id = ?", (int(duplicate_id),))
            self.conn.execute("DELETE FROM people_meta WHERE person_id = ?", (int(duplicate_id),))
        return self.get(int(target_id), include_sensitive=True) or merged

    def resolve(self, reference: str, *, require: str | None = None) -> dict[str, Any]:
        """Resolve one exact canonical name/alias, never fuzzy-match a recipient."""
        normalized = normalize_reference(reference)
        if not normalized:
            raise PersonResolutionError("A person name or alias is required.")
        rows = self.conn.execute(
            """
            SELECT p.* FROM people_meta AS p
            JOIN person_aliases AS a ON a.person_id = p.person_id
            WHERE a.alias_normalized = ?
            ORDER BY p.person_id
            """,
            (normalized,),
        ).fetchall()
        if not rows:
            raise PersonResolutionError(f"No saved person exactly matches '{reference}'.")
        if len(rows) != 1:
            names = ", ".join(row["canonical_name"] for row in rows[:5])
            raise PersonResolutionError(f"'{reference}' is ambiguous between: {names}.")
        person = self._row_to_person(rows[0], include_sensitive=True)
        if require == "phone" and not person.get("phone"):
            raise PersonResolutionError(f"{person['canonical_name']} has no saved phone number.")
        if require == "email" and not person.get("email"):
            raise PersonResolutionError(f"{person['canonical_name']} has no saved email address.")
        self._touch([person["person_id"]])
        return person

    def mentioned_in(self, text: str, *, limit: int = 5) -> list[dict[str, Any]]:
        """Find explicitly saved names/aliases mentioned in a user request.

        This only matches whole normalized aliases already saved by the user;
        it never mines names or contact data from conversations or tool output.
        """
        normalized_text = normalize_reference(text)
        if not normalized_text:
            return []
        padded = f" {normalized_text} "
        bounded = max(1, min(int(limit), 20))
        rows = self.conn.execute(
            """
            SELECT p.*, a.alias_normalized FROM people_meta AS p
            JOIN person_aliases AS a ON a.person_id = p.person_id
            ORDER BY LENGTH(a.alias_normalized) DESC, p.updated_at DESC
            """
        ).fetchall()
        matched: list[int] = []
        for row in rows:
            alias = str(row["alias_normalized"] or "")
            if len(alias) < 3 or f" {alias} " not in padded:
                continue
            person_id = int(row["person_id"])
            if person_id not in matched:
                matched.append(person_id)
            if len(matched) >= bounded:
                break
        self._touch(matched)
        return [self.get(person_id, include_sensitive=True) or {} for person_id in matched]

    def mark_contacted(self, reference: str, *, channel: str) -> bool:
        """Record a successful use of a saved alias without exposing its value."""
        normalized_channel = str(channel or "").strip().casefold()
        if normalized_channel not in {"email", "sms", "phone"}:
            raise ValueError("channel must be email, sms, or phone")
        try:
            person = self.resolve(reference)
        except PersonResolutionError:
            return False
        with self._transaction():
            self.conn.execute(
                """UPDATE people_meta
                   SET last_contacted_at = ?, last_contacted_via = ?, updated_at = ?
                   WHERE person_id = ?""",
                (utc_now(), normalized_channel, utc_now(), person["person_id"]),
            )
        return True

    def update(
        self,
        person_id: int,
        *,
        canonical_name: str | object = _UNSET,
        aliases: list[str] | tuple[str, ...] | None | object = _UNSET,
        relation: str | object = _UNSET,
        phone: str | None | object = _UNSET,
        email: str | None | object = _UNSET,
        important_dates: dict[str, Any] | None | object = _UNSET,
        notes: str | object = _UNSET,
        source: str | object = _UNSET,
        confidence: float | object = _UNSET,
        pronouns: str | object = _UNSET,
        preferred_address: str | object = _UNSET,
        timezone: str | object = _UNSET,
        communication_preferences: dict[str, Any] | object = _UNSET,
        preferred_contact_method: str | object = _UNSET,
        organization: str | object = _UNSET,
        role: str | object = _UNSET,
        interests: list[str] | tuple[str, ...] | object = _UNSET,
        reminder_preferences: dict[str, Any] | object = _UNSET,
        links: dict[str, object] | object = _UNSET,
        timeline: list[dict[str, Any]] | None = None,
        expected_revision: int | None = None,
    ) -> dict[str, Any] | None:
        existing = self.get(int(person_id), include_sensitive=True)
        if existing is None:
            return None
        if expected_revision is not None and existing["revision"] != int(expected_revision):
            raise PersonConflictError(
                f"Person #{person_id} changed since revision {expected_revision}; current revision is {existing['revision']}."
            )

        new_name = existing["canonical_name"] if canonical_name is _UNSET else _clean_text(
            canonical_name, field="canonical_name", maximum=160, allow_empty=False
        )
        new_normalized_name = normalize_reference(new_name)
        if not new_normalized_name:
            raise ValueError("canonical_name must contain letters or numbers")
        new_aliases = existing["aliases"] if aliases is _UNSET else list(aliases or [])
        aliases_to_store = _normalize_aliases(new_name, new_aliases)
        new_source = existing["source"] if source is _UNSET else str(source or "").strip().casefold()
        if new_source not in _SOURCES:
            raise ValueError(f"source must be one of: {', '.join(sorted(_SOURCES))}")
        new_contact_method = existing["preferred_contact_method"] if preferred_contact_method is _UNSET else str(preferred_contact_method or "").strip().casefold()
        if new_contact_method not in {"", "email", "sms", "phone"}:
            raise ValueError("preferred_contact_method must be email, sms, phone, or empty")
        new_phone = existing["phone"] if phone is _UNSET else (self._validate_contact(phone, field="phone") or None)
        new_email = existing["email"] if email is _UNSET else (self._validate_contact(email, field="email") or None)
        conflicts = self._contact_conflicts(phone=new_phone, email=new_email, exclude_person_id=int(person_id))
        if conflicts:
            conflict = conflicts[0]
            raise PersonConflictError(
                f"{conflict['field']} is already saved for {conflict['canonical_name']} (person #{conflict['person_id']})."
            )
        values = {
            "canonical_name": new_name,
            "normalized_name": new_normalized_name,
            "relation": existing["relation"] if relation is _UNSET else _clean_text(relation, field="relation", maximum=160),
            "phone": new_phone,
            "email": new_email,
            "important_dates_json": json.dumps(
                existing["important_dates"] if important_dates is _UNSET else _normalize_dates(important_dates),
                ensure_ascii=False,
                sort_keys=True,
            ),
            "notes": existing["notes"] if notes is _UNSET else _clean_text(notes, field="notes", maximum=4_000),
            "source": new_source,
            "confidence": existing["confidence"] if confidence is _UNSET else self._validate_confidence(confidence),
            "pronouns": existing["pronouns"] if pronouns is _UNSET else _clean_text(pronouns, field="pronouns", maximum=80),
            "preferred_address": existing["preferred_address"] if preferred_address is _UNSET else _clean_text(preferred_address, field="preferred_address", maximum=160),
            "timezone": existing["timezone"] if timezone is _UNSET else _clean_text(timezone, field="timezone", maximum=100),
            "communication_preferences_json": json.dumps(
                existing["communication_preferences"] if communication_preferences is _UNSET else communication_preferences,
                ensure_ascii=False, sort_keys=True,
            ),
            "preferred_contact_method": new_contact_method,
            "organization": existing["organization"] if organization is _UNSET else _clean_text(organization, field="organization", maximum=240),
            "role": existing["role"] if role is _UNSET else _clean_text(role, field="role", maximum=160),
            "interests_json": json.dumps(
                existing["interests"] if interests is _UNSET else [
                    _clean_text(item, field="interest", maximum=160, allow_empty=False) for item in interests
                ], ensure_ascii=False,
            ),
            "reminder_preferences_json": json.dumps(
                existing["reminder_preferences"] if reminder_preferences is _UNSET else reminder_preferences,
                ensure_ascii=False, sort_keys=True,
            ),
        }
        try:
            with self._transaction():
                changed_fields = sorted(
                    key for key, value in {
                        "canonical_name": canonical_name, "aliases": aliases, "relation": relation,
                        "phone": phone, "email": email, "important_dates": important_dates,
                        "notes": notes, "source": source, "confidence": confidence,
                        "pronouns": pronouns, "preferred_address": preferred_address,
                        "timezone": timezone, "communication_preferences": communication_preferences,
                        "preferred_contact_method": preferred_contact_method, "organization": organization,
                        "role": role, "interests": interests,
                        "reminder_preferences": reminder_preferences, "links": links,
                    }.items() if value is not _UNSET
                )
                self.conn.execute(
                    """INSERT OR IGNORE INTO person_revisions
                       (person_id, revision, snapshot_json, changed_fields_json, created_at)
                       VALUES (?, ?, ?, ?, ?)""",
                    (
                        int(person_id), int(existing["revision"]),
                        json.dumps(existing, ensure_ascii=False, sort_keys=True, default=str),
                        json.dumps(changed_fields), utc_now(),
                    ),
                )
                self.conn.execute(
                    """
                    UPDATE people_meta
                    SET canonical_name = ?, normalized_name = ?, relation = ?, phone = ?, email = ?,
                        important_dates_json = ?, notes = ?, updated_at = ?, source = ?, confidence = ?,
                        pronouns = ?, preferred_address = ?, timezone = ?,
                        communication_preferences_json = ?, preferred_contact_method = ?,
                        organization = ?, role = ?, interests_json = ?, reminder_preferences_json = ?,
                        revision = revision + 1
                    WHERE person_id = ?
                    """,
                    (
                        values["canonical_name"], values["normalized_name"], values["relation"], values["phone"],
                        values["email"], values["important_dates_json"], values["notes"], utc_now(),
                        values["source"], values["confidence"],
                        values["pronouns"], values["preferred_address"], values["timezone"],
                        values["communication_preferences_json"], values["preferred_contact_method"],
                        values["organization"], values["role"], values["interests_json"],
                        values["reminder_preferences_json"], int(person_id),
                    ),
                )
                self._replace_aliases(int(person_id), aliases_to_store)
                if links is not _UNSET:
                    self._replace_links(int(person_id), links)
                self._append_timeline(int(person_id), timeline)
        except sqlite3.IntegrityError as exc:
            raise PersonConflictError("A person already owns that canonical name or alias.") from exc
        return self.get(int(person_id), include_sensitive=True)

    def delete(self, person_id: int, *, expected_revision: int | None = None) -> bool:
        existing = self.get(int(person_id), include_sensitive=False)
        if existing is None:
            return False
        if expected_revision is not None and existing["revision"] != int(expected_revision):
            raise PersonConflictError(f"Person #{person_id} changed since revision {expected_revision}.")
        with self._transaction():
            self.conn.execute("DELETE FROM people_meta WHERE person_id = ?", (int(person_id),))
        return True

    def recent_for_context(self, *, limit: int = 6) -> list[dict[str, Any]]:
        """Return bounded complete records for the local assistant context."""
        bounded = max(1, min(int(limit), 20))
        rows = self.conn.execute(
            """
            SELECT * FROM people_meta
            ORDER BY CASE WHEN last_referenced_at IS NULL THEN 1 ELSE 0 END,
                     last_referenced_at DESC, updated_at DESC
            LIMIT ?
            """,
            (bounded,),
        ).fetchall()
        return [self._row_to_person(row, include_sensitive=True) for row in rows]

    def list_all(self, *, include_sensitive: bool = True) -> list[dict[str, Any]]:
        rows = self.conn.execute("SELECT * FROM people_meta ORDER BY canonical_name COLLATE NOCASE").fetchall()
        return [self._row_to_person(row, include_sensitive=include_sensitive) for row in rows]

    def import_people(self, people: list[dict[str, Any]]) -> int:
        """Import explicitly exported people while skipping canonical-name collisions."""
        imported = 0
        existing_names = {normalize_reference(person["canonical_name"]) for person in self.list_all()}
        for item in people:
            name = str(item.get("canonical_name") or "").strip()
            if not name or normalize_reference(name) in existing_names:
                continue
            self.create(
                name,
                aliases=item.get("aliases") or [],
                relation=item.get("relation") or "",
                phone=item.get("phone") or None,
                email=item.get("email") or None,
                important_dates=item.get("important_dates") or {},
                notes=item.get("notes") or "",
                source="import",
                confidence=float(item.get("confidence", 1.0)),
            )
            existing_names.add(normalize_reference(name))
            imported += 1
        return imported

    def count(self) -> int:
        row = self.conn.execute("SELECT COUNT(*) AS count FROM people_meta").fetchone()
        return int(row["count"]) if row else 0

    def close(self) -> None:
        if self._owns_connection:
            self.conn.close()
