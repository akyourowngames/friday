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
                created_at           TEXT NOT NULL,
                updated_at           TEXT NOT NULL,
                source               TEXT NOT NULL DEFAULT 'manual',
                confidence           REAL NOT NULL DEFAULT 1.0,
                revision             INTEGER NOT NULL DEFAULT 1
            )
            """
        )
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
        self.conn.commit()

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        """Take an immediate write transaction so aliases cannot race each other."""
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
        person = {
            "person_id": int(row["person_id"]),
            "canonical_name": row["canonical_name"],
            "aliases": self._aliases_for(int(row["person_id"]), row["normalized_name"]),
            "relation": row["relation"] or "",
            "important_dates": important_dates if isinstance(important_dates, dict) else {},
            "notes": row["notes"] or "",
            "last_referenced_at": row["last_referenced_at"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "source": row["source"],
            "confidence": float(row["confidence"]),
            "revision": int(row["revision"]),
            "has_phone": bool(row["phone"]),
            "has_email": bool(row["email"]),
        }
        if include_sensitive:
            person["phone"] = row["phone"] or ""
            person["email"] = row["email"] or ""
        else:
            person["phone_hint"] = mask_phone(row["phone"])
            person["email_hint"] = mask_email(row["email"])
        return person

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
    ) -> dict[str, Any]:
        name = _clean_text(canonical_name, field="canonical_name", maximum=160, allow_empty=False)
        normalized_name = normalize_reference(name)
        if not normalized_name:
            raise ValueError("canonical_name must contain letters or numbers")
        aliases_to_store = _normalize_aliases(name, aliases)
        source = str(source or "manual").strip().casefold()
        if source not in _SOURCES:
            raise ValueError(f"source must be one of: {', '.join(sorted(_SOURCES))}")
        now = utc_now()
        try:
            with self._transaction():
                cursor = self.conn.execute(
                    """
                    INSERT INTO people_meta (
                        canonical_name, normalized_name, relation, phone, email,
                        important_dates_json, notes, created_at, updated_at, source, confidence
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        name,
                        normalized_name,
                        _clean_text(relation, field="relation", maximum=160),
                        self._validate_contact(phone, field="phone") or None,
                        self._validate_contact(email, field="email") or None,
                        json.dumps(_normalize_dates(important_dates), ensure_ascii=False, sort_keys=True),
                        _clean_text(notes, field="notes", maximum=4_000),
                        now,
                        now,
                        source,
                        self._validate_confidence(confidence),
                    ),
                )
                person_id = int(cursor.lastrowid)
                self._replace_aliases(person_id, aliases_to_store)
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
        values = {
            "canonical_name": new_name,
            "normalized_name": new_normalized_name,
            "relation": existing["relation"] if relation is _UNSET else _clean_text(relation, field="relation", maximum=160),
            "phone": existing["phone"] if phone is _UNSET else (self._validate_contact(phone, field="phone") or None),
            "email": existing["email"] if email is _UNSET else (self._validate_contact(email, field="email") or None),
            "important_dates_json": json.dumps(
                existing["important_dates"] if important_dates is _UNSET else _normalize_dates(important_dates),
                ensure_ascii=False,
                sort_keys=True,
            ),
            "notes": existing["notes"] if notes is _UNSET else _clean_text(notes, field="notes", maximum=4_000),
            "source": new_source,
            "confidence": existing["confidence"] if confidence is _UNSET else self._validate_confidence(confidence),
        }
        try:
            with self._transaction():
                self.conn.execute(
                    """
                    UPDATE people_meta
                    SET canonical_name = ?, normalized_name = ?, relation = ?, phone = ?, email = ?,
                        important_dates_json = ?, notes = ?, updated_at = ?, source = ?, confidence = ?,
                        revision = revision + 1
                    WHERE person_id = ?
                    """,
                    (
                        values["canonical_name"], values["normalized_name"], values["relation"], values["phone"],
                        values["email"], values["important_dates_json"], values["notes"], utc_now(),
                        values["source"], values["confidence"], int(person_id),
                    ),
                )
                self._replace_aliases(int(person_id), aliases_to_store)
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
        """Return bounded, contact-redacted entries safe for model context."""
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
        return [self._row_to_person(row, include_sensitive=False) for row in rows]

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
