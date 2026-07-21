"""Memory system: SQLite + sqlite-vec for vector search + FTS5 for keyword search."""

from datetime import datetime, timezone
from collections import OrderedDict
import hashlib
import json
import logging
import math
import re
import sqlite3
import time
import weakref
from collections.abc import Callable
from pathlib import Path
from typing import Any

import sqlite_vec

from ares.config import get_db_path, load_config
from ares.memory.embeddings import (
    DEFAULT_EMBEDDING_MODEL,
    EmbeddingProvider,
)
from ares.infra.sqlite_utils import connect_sqlite

EMBEDDING_MODEL_NAME = DEFAULT_EMBEDDING_MODEL

_default_provider: EmbeddingProvider | None = None
logger = logging.getLogger(__name__)
_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_WORD_RE = re.compile(r"[A-Za-z0-9']+")
_UNSET = object()
_DEFAULT_QUERY_EMBEDDING_CACHE_SIZE = 128
# Keep this deliberately narrow.  A standalone greeting or acknowledgement
# cannot benefit from semantic recall, but a longer request that merely starts
# with one of these words still takes the normal hybrid path.
_TRIVIAL_MEMORY_QUERY_RE = re.compile(
    r"^\s*(?:"
    r"hi|hello|hey|yo|"
    r"good\s+(?:morning|afternoon|evening)|"
    r"thanks?|thank\s+you|thx|"
    r"ok(?:ay)?|k|sure|yeah|yep|yes|"
    r"no|nope|nah|got\s+it|sounds\s+good|"
    r"all\s+good|cool|great|perfect|fine"
    r")\s*[!.?,]*\s*$",
    re.IGNORECASE,
)


class MemoryConflictError(ValueError):
    """Raised when a memory revision or merge target changed concurrently."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def calculate_importance(content: str, category: str) -> float:
    """Calculate a bounded durable-memory importance score."""
    score = {
        "preference": 0.72,
        "fact": 0.62,
        "relationship": 0.72,
        "project": 0.76,
        "plan": 0.5,
        "belief": 0.58,
        "habit": 0.65,
        "note": 0.45,
    }.get(str(category or "note").casefold(), 0.5)
    text = str(content or "").casefold()
    if any(marker in text for marker in ("always", "never", "important", "remember", "deadline")):
        score += 0.1
    if any(marker in text for marker in ("maybe", "might", "temporary", "for now")):
        score -= 0.12
    return round(max(0.0, min(score, 1.0)), 3)


def _get_default_provider() -> EmbeddingProvider:
    """Lazy-load the configured embedding provider."""
    global _default_provider
    if _default_provider is None:
        config = load_config()
        _default_provider = EmbeddingProvider(
            model_name=config.embedding_model,
            backend=config.embedding_backend,
            provider=config.embedding_provider,
            file_name=config.embedding_file_name,
            local_files_only=not config.embedding_allow_download,
        )
    return _default_provider


def _embed(text: str) -> bytes:
    """Embed text and return as raw float32 bytes for sqlite-vec."""
    return _get_default_provider().embed_bytes(text)


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    """Add a trusted schema column if missing.

    SQLite does not support binding identifiers. Validate table/column names
    before interpolation; `definition` is an internal migration string only.
    """
    if not _IDENTIFIER_RE.match(table) or not _IDENTIFIER_RE.match(column):
        raise ValueError("Invalid SQLite identifier for schema migration")
    columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def _parse_db_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


class MemoryStore:
    """Manages the memory database: storing, searching, and deleting facts."""

    def __init__(
        self,
        db_path: Path | None = None,
        *,
        embedding_provider: EmbeddingProvider | None = None,
        embedding_backend: str | None = None,
        embedding_model: str | None = None,
        query_embedding_cache_size: int = _DEFAULT_QUERY_EMBEDDING_CACHE_SIZE,
    ):
        config = load_config()
        self.retrieval_config = getattr(getattr(config, "memory", None), "retrieval", None)
        self.db_path = db_path or get_db_path()
        self.embedding_provider = embedding_provider or EmbeddingProvider(
            model_name=embedding_model or config.embedding_model,
            backend=embedding_backend or config.embedding_backend,
            provider=config.embedding_provider,
            file_name=config.embedding_file_name,
            local_files_only=not config.embedding_allow_download,
        )
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = connect_sqlite(self.db_path)
        self.vector_enabled = False
        # Optional consumers can react to a deleted generic fact without
        # coupling this store to their own persistence or retention rules.
        # Bound methods are kept weakly; see add_deletion_observer().
        self._deletion_observers: dict[int, Callable[[], Any]] = {}
        self._next_deletion_observer_id = 0
        self._query_embedding_cache_size = max(0, int(query_embedding_cache_size))
        self._query_embedding_cache: OrderedDict[str, bytes] = OrderedDict()
        # Search is part of the response critical path.  Queue access counters
        # and commit them later in one batch instead of making every retrieval
        # wait for a SQLite write.
        self._pending_access_counts: dict[int, int] = {}
        self._pending_candidate_feedback: dict[tuple[int, str], float] = {}
        self._init_db()

    def add_deletion_observer(self, observer: Callable[[int], Any]) -> Callable[[], None]:
        """Register a post-commit deletion callback and return its remover.

        Bound-method callbacks are weakly held so feature services cannot be
        retained by a long-lived MemoryStore.  A returned remover is available
        for deterministic shutdown and for callables that cannot be weakly
        referenced.
        """

        if not callable(observer):
            raise TypeError("deletion observer must be callable")
        try:
            if getattr(observer, "__self__", None) is not None:
                reference: Callable[[], Any] = weakref.WeakMethod(observer)  # type: ignore[arg-type]
            else:
                # Plain functions and callable objects are conventionally
                # retained by event registries. Their returned unsubscribe
                # callback keeps that lifetime explicit and predictable.
                reference = lambda: observer
        except TypeError:
            # A few built-in callable objects cannot be weak-referenced. They
            # remain explicitly unregisterable through the returned callback.
            reference = lambda: observer

        observer_id = self._next_deletion_observer_id
        self._next_deletion_observer_id += 1
        self._deletion_observers[observer_id] = reference

        def unsubscribe() -> None:
            self._deletion_observers.pop(observer_id, None)

        return unsubscribe

    register_deletion_observer = add_deletion_observer

    def _notify_deleted(self, fact_ids: tuple[int, ...]) -> None:
        """Run observers after commit without changing public delete results."""

        for fact_id in fact_ids:
            for observer_id, reference in tuple(self._deletion_observers.items()):
                observer = reference()
                if observer is None:
                    self._deletion_observers.pop(observer_id, None)
                    continue
                try:
                    observer(int(fact_id))
                except Exception:
                    # The generic fact is already gone. An optional cleanup
                    # must not make a normal memory delete appear to fail.
                    logger.exception("memory deletion observer failed for fact %s", fact_id)

    def _init_db(self):
        """Initialize database tables if they don't exist."""
        try:
            enable_load_extension = getattr(self.conn, "enable_load_extension")
            enable_load_extension(True)
            sqlite_vec.load(self.conn)
            enable_load_extension(False)
            self.vector_enabled = True
        except Exception as exc:
            self.vector_enabled = False
            logger.warning("sqlite-vec unavailable; memory search will use FTS only: %s", exc)

        # Vector table for semantic search. If sqlite-vec is unavailable, create
        # a plain compatibility table so insert/delete paths remain harmless.
        if self.vector_enabled:
            self.conn.execute("""
                CREATE VIRTUAL TABLE IF NOT EXISTS user_facts USING vec0(
                    embedding float[384]
                )
            """)
        else:
            self.conn.execute("""
                CREATE TABLE IF NOT EXISTS user_facts (
                    rowid INTEGER PRIMARY KEY,
                    embedding BLOB
                )
            """)

        # Metadata table
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS facts_meta (
                fact_id       INTEGER PRIMARY KEY,
                fact_text     TEXT NOT NULL,
                category      TEXT DEFAULT 'note',
                confidence    REAL DEFAULT 1.0,
                importance    REAL DEFAULT 0.5,
                source        TEXT DEFAULT 'conversation',
                created_at    TEXT DEFAULT (datetime('now')),
                updated_at    TEXT DEFAULT (datetime('now')),
                last_accessed TEXT,
                access_count  INTEGER DEFAULT 0,
                superseded_by INTEGER
            )
        """)
        _ensure_column(self.conn, "facts_meta", "importance", "REAL DEFAULT 0.5")
        _ensure_column(self.conn, "facts_meta", "source", "TEXT DEFAULT 'conversation'")
        _ensure_column(self.conn, "facts_meta", "updated_at", "TEXT")
        _ensure_column(self.conn, "facts_meta", "session_id", "TEXT DEFAULT NULL")
        _ensure_column(self.conn, "facts_meta", "source_conversation_id", "TEXT DEFAULT NULL")
        _ensure_column(self.conn, "facts_meta", "source_reflection_id", "TEXT DEFAULT NULL")
        _ensure_column(self.conn, "facts_meta", "source_message_id", "TEXT DEFAULT NULL")
        _ensure_column(self.conn, "facts_meta", "tags_json", "TEXT NOT NULL DEFAULT '[]'")
        _ensure_column(self.conn, "facts_meta", "valid_from", "TEXT DEFAULT NULL")
        _ensure_column(self.conn, "facts_meta", "expires_at", "TEXT DEFAULT NULL")
        _ensure_column(self.conn, "facts_meta", "outdated_at", "TEXT DEFAULT NULL")
        _ensure_column(self.conn, "facts_meta", "project", "TEXT DEFAULT NULL")
        _ensure_column(self.conn, "facts_meta", "revision", "INTEGER NOT NULL DEFAULT 1")
        _ensure_column(self.conn, "facts_meta", "archived_at", "TEXT DEFAULT NULL")
        _ensure_column(self.conn, "facts_meta", "source_candidate_id", "INTEGER DEFAULT NULL")
        self.conn.execute(
            """CREATE TABLE IF NOT EXISTS memory_revisions (
                   revision_id INTEGER PRIMARY KEY AUTOINCREMENT,
                   fact_id INTEGER NOT NULL,
                   revision INTEGER NOT NULL,
                   snapshot_json TEXT NOT NULL,
                   change_summary TEXT NOT NULL DEFAULT '',
                   created_at TEXT NOT NULL,
                   UNIQUE(fact_id, revision)
               )"""
        )
        self.conn.execute(
            """CREATE TABLE IF NOT EXISTS memory_links (
                   fact_id INTEGER NOT NULL,
                   entity_type TEXT NOT NULL,
                   entity_id TEXT NOT NULL,
                   created_at TEXT NOT NULL,
                   PRIMARY KEY(fact_id, entity_type, entity_id)
               )"""
        )
        self.conn.execute(
            """CREATE TABLE IF NOT EXISTS memory_relations (
                   source_fact_id INTEGER NOT NULL,
                   target_fact_id INTEGER NOT NULL,
                   relation TEXT NOT NULL,
                   confidence REAL NOT NULL DEFAULT 1.0,
                   created_at TEXT NOT NULL,
                   PRIMARY KEY(source_fact_id, target_fact_id, relation)
               )"""
        )
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_memory_validity ON facts_meta(outdated_at, expires_at, valid_from)")
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_memory_project ON facts_meta(project)")
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_memory_archive ON facts_meta(archived_at, outdated_at)")
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_memory_candidate_source ON facts_meta(source_candidate_id)")
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_memory_links_entity ON memory_links(entity_type, entity_id)")

        # Reflected facts are durable user memories, not conversation-local
        # scratch state. Migrate PR #28 rows once while retaining their source
        # conversation independently for provenance and auditability.
        self.conn.execute(
            """UPDATE facts_meta
               SET source_conversation_id = COALESCE(source_conversation_id, session_id),
                   session_id = NULL
               WHERE source = 'conversation_reflection' AND session_id IS NOT NULL"""
        )

        # FTS5 for keyword search
        self.conn.execute("""
            CREATE VIRTUAL TABLE IF NOT EXISTS facts_fts USING fts5(
                fact_text,
                content='facts_meta',
                content_rowid='fact_id'
            )
        """)

        self.conn.commit()

    def _embed(self, text: str) -> bytes:
        return self.embedding_provider.embed_bytes(text)

    def _embed_query(self, query: str) -> bytes:
        """Embed a search query with a small per-store LRU cache."""
        if self._query_embedding_cache_size <= 0:
            return self._embed(query)

        cached = self._query_embedding_cache.get(query)
        if cached is not None:
            self._query_embedding_cache.move_to_end(query)
            return cached

        embedding = self._embed(query)
        self._query_embedding_cache[query] = embedding
        self._query_embedding_cache.move_to_end(query)
        while len(self._query_embedding_cache) > self._query_embedding_cache_size:
            self._query_embedding_cache.popitem(last=False)
        return embedding

    @staticmethod
    def _should_use_semantic_search(query: str) -> bool:
        """Return whether semantic retrieval is useful for this short query."""
        return not bool(_TRIVIAL_MEMORY_QUERY_RE.match(query))

    def _queue_access_stats(self, fact_ids: list[int]) -> None:
        """Accumulate retrieval access counts without issuing SQLite writes."""
        for fact_id in fact_ids:
            normalized_id = int(fact_id)
            self._pending_access_counts[normalized_id] = (
                self._pending_access_counts.get(normalized_id, 0) + 1
            )

    def _queue_candidate_feedback(self, memories: list[dict], query: str) -> None:
        """Defer query-diversity feedback to the normal batched write path."""

        query_key = hashlib.sha256(
            _normalize_memory_text(query).encode("utf-8")
        ).hexdigest()
        for memory in memories:
            candidate_id = memory.get("source_candidate_id")
            if candidate_id is None:
                continue
            key = (int(candidate_id), query_key)
            self._pending_candidate_feedback[key] = max(
                self._pending_candidate_feedback.get(key, 0.0),
                float(memory.get("_relevance") or 0.0),
            )

    def flush_access_stats(self) -> int:
        """Persist queued memory access counters in one SQLite transaction.

        Returns the number of distinct memories updated.  This is safe to call
        after a streamed response, from a periodic maintenance path, or during
        shutdown.  Failed writes are returned to the queue for a later retry.
        """
        if not self._pending_access_counts and not self._pending_candidate_feedback:
            return 0

        pending = self._pending_access_counts
        feedback = self._pending_candidate_feedback
        self._pending_access_counts = {}
        self._pending_candidate_feedback = {}
        try:
            self.conn.executemany(
                """UPDATE facts_meta
                   SET last_accessed = datetime('now'),
                       access_count = access_count + ?
                   WHERE fact_id = ?""",
                [(count, fact_id) for fact_id, count in pending.items()],
            )
            query_table = self.conn.execute(
                """SELECT 1 FROM sqlite_master
                   WHERE type='table' AND name='memory_candidate_queries'"""
            ).fetchone()
            if query_table is not None and feedback:
                now = utc_now()
                self.conn.executemany(
                    """INSERT INTO memory_candidate_queries
                       (candidate_id, query_key, relevance, created_at)
                       VALUES (?, ?, ?, ?)
                       ON CONFLICT(candidate_id, query_key) DO UPDATE SET
                         relevance=MAX(relevance, excluded.relevance)""",
                    [
                        (candidate_id, query_key, relevance, now)
                        for (candidate_id, query_key), relevance in feedback.items()
                    ],
                )
                for candidate_id in {key[0] for key in feedback}:
                    self.conn.execute(
                        """UPDATE memory_candidates
                           SET unique_query_count=(
                                   SELECT COUNT(*) FROM memory_candidate_queries
                                   WHERE candidate_id=?
                               ),
                               average_relevance=COALESCE((
                                   SELECT AVG(relevance) FROM memory_candidate_queries
                                   WHERE candidate_id=?
                               ), 0.0)
                           WHERE candidate_id=?""",
                        (candidate_id, candidate_id, candidate_id),
                    )
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            for fact_id, count in pending.items():
                self._pending_access_counts[fact_id] = (
                    self._pending_access_counts.get(fact_id, 0) + count
                )
            for key, relevance in feedback.items():
                self._pending_candidate_feedback[key] = max(
                    self._pending_candidate_feedback.get(key, 0.0), relevance
                )
            raise
        return len(pending)
    def _delete_fts_entry(self, fact_id: int, fact_text: str) -> None:
        """Remove an external-content FTS5 row without corrupting its index."""
        self.conn.execute(
            "INSERT INTO facts_fts(facts_fts, rowid, fact_text) VALUES ('delete', ?, ?)",
            (int(fact_id), str(fact_text)),
        )

    def _replace_fts_entry(self, fact_id: int, old_text: str, new_text: str) -> None:
        self._delete_fts_entry(fact_id, old_text)
        self.conn.execute(
            "INSERT INTO facts_fts (rowid, fact_text) VALUES (?, ?)",
            (int(fact_id), str(new_text)),
        )

    def store(
        self,
        fact_text: str,
        category: str = "note",
        confidence: float = 1.0,
        importance: float = 0.5,
        source: str = "conversation",
        session_id: str | None = None,
        source_conversation_id: str | None = None,
        source_reflection_id: str | None = None,
        source_message_id: str | None = None,
        tags: list[str] | tuple[str, ...] | None = None,
        valid_from: str | None = None,
        expires_at: str | None = None,
        supersedes_memory_id: int | None = None,
        project: str | None = None,
        links: dict[str, list[str] | tuple[str, ...] | str] | None = None,
        source_candidate_id: int | None = None,
    ) -> int:
        """Store a new fact. Returns the fact_id."""
        # Insert metadata
        cursor = self.conn.execute(
            """INSERT INTO facts_meta
               (fact_text, category, confidence, importance, source, session_id,
                source_conversation_id, source_reflection_id, source_message_id,
                tags_json, valid_from, expires_at, project, source_candidate_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                fact_text, category, confidence, importance, source, session_id,
                source_conversation_id, source_reflection_id, source_message_id,
                json.dumps(self._normalize_tags(tags), ensure_ascii=False),
                valid_from, expires_at, project, source_candidate_id,
            ),
        )
        fact_id = int(cursor.lastrowid)

        # Insert embedding when vector search is available.
        if self.vector_enabled:
            embedding = self._embed(fact_text)
            self.conn.execute(
                "INSERT INTO user_facts (rowid, embedding) VALUES (?, ?)",
                (fact_id, embedding),
            )

        # Insert into FTS5
        self.conn.execute(
            "INSERT INTO facts_fts (rowid, fact_text) VALUES (?, ?)",
            (fact_id, fact_text),
        )

        self._replace_links(fact_id, links or {})
        if supersedes_memory_id is not None:
            previous = self.get(int(supersedes_memory_id))
            if previous is None:
                self.conn.rollback()
                raise ValueError(f"Memory #{supersedes_memory_id} was not found.")
            self.conn.execute(
                "UPDATE facts_meta SET superseded_by = ?, outdated_at = COALESCE(outdated_at, ?), updated_at = datetime('now') WHERE fact_id = ?",
                (fact_id, utc_now(), int(supersedes_memory_id)),
            )
            self._add_relation(fact_id, int(supersedes_memory_id), "supersedes", 1.0)

        self.conn.commit()
        return fact_id

    @staticmethod
    def _normalize_tags(tags: list[str] | tuple[str, ...] | None) -> list[str]:
        normalized: list[str] = []
        for value in tags or ():
            tag = re.sub(r"\s+", "-", str(value or "").strip().casefold())[:80]
            if tag and tag not in normalized:
                normalized.append(tag)
        return normalized[:50]

    def _replace_links(self, fact_id: int, links: dict[str, object]) -> None:
        self.conn.execute("DELETE FROM memory_links WHERE fact_id = ?", (int(fact_id),))
        for entity_type, raw_values in links.items():
            kind = str(entity_type or "").strip().casefold()
            if kind not in {"person", "goal", "action", "file", "project"}:
                raise ValueError(f"Unsupported memory link type: {entity_type}")
            values = raw_values if isinstance(raw_values, (list, tuple, set)) else [raw_values]
            for raw in values:
                entity_id = str(raw or "").strip()
                if entity_id:
                    self.conn.execute(
                        "INSERT OR IGNORE INTO memory_links (fact_id, entity_type, entity_id, created_at) VALUES (?, ?, ?, ?)",
                        (int(fact_id), kind, entity_id[:500], utc_now()),
                    )

    def _add_relation(self, source_id: int, target_id: int, relation: str, confidence: float) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO memory_relations (source_fact_id, target_fact_id, relation, confidence, created_at) VALUES (?, ?, ?, ?, ?)",
            (int(source_id), int(target_id), str(relation), float(confidence), utc_now()),
        )

    def suggest_merge(
        self,
        fact_text: str,
        category: str = "note",
        *,
        limit: int = 5,
    ) -> list[dict]:
        """Suggest duplicate or conflicting memories before storing a new fact."""
        normalized = _normalize_memory_text(fact_text)
        if not normalized:
            return []
        suggestions: list[dict] = []
        for memory in self.list_all():
            existing = _normalize_memory_text(memory.get("fact_text", ""))
            if not existing:
                continue
            if existing == normalized and memory.get("category", "note") == category:
                suggestions.append({
                    "kind": "duplicate",
                    "fact_id": memory["fact_id"],
                    "fact_text": memory["fact_text"],
                    "confidence": 1.0,
                    "recommendation": "Reuse the existing memory instead of storing a duplicate.",
                })
            elif _memory_relation(existing) == _memory_relation(normalized):
                overlap = _token_overlap(existing, normalized)
                if overlap >= 0.25:
                    suggestions.append({
                        "kind": "possible_conflict",
                        "fact_id": memory["fact_id"],
                        "fact_text": memory["fact_text"],
                        "confidence": round(overlap, 2),
                        "recommendation": "Review and update/merge the existing memory if this supersedes it.",
                    })
            if len(suggestions) >= limit:
                break
        return suggestions

    def get(self, fact_id: int) -> dict | None:
        """Return one memory by ID."""
        row = self.conn.execute(
            "SELECT * FROM facts_meta WHERE fact_id = ?", (fact_id,)
        ).fetchone()
        return self._row_to_memory(row) if row else None

    def _row_to_memory(self, row: sqlite3.Row | dict) -> dict:
        memory = dict(row)
        try:
            tags = json.loads(memory.get("tags_json") or "[]")
        except (TypeError, json.JSONDecodeError):
            tags = []
        memory["tags"] = tags if isinstance(tags, list) else []
        memory.pop("tags_json", None)
        memory["links"] = self.links_for(int(memory["fact_id"]))
        memory["related_memories"] = self.relations_for(int(memory["fact_id"]))
        memory["may_be_outdated"] = self._is_outdated(memory)
        return memory

    @staticmethod
    def _is_outdated(memory: dict) -> bool:
        if memory.get("archived_at") or memory.get("outdated_at") or memory.get("superseded_by"):
            return True
        expires = _parse_db_datetime(memory.get("expires_at"))
        return bool(expires and expires <= datetime.now(timezone.utc))

    def links_for(self, fact_id: int) -> dict[str, list[str]]:
        rows = self.conn.execute(
            "SELECT entity_type, entity_id FROM memory_links WHERE fact_id = ? ORDER BY entity_type, entity_id",
            (int(fact_id),),
        ).fetchall()
        links: dict[str, list[str]] = {}
        for row in rows:
            links.setdefault(str(row["entity_type"]), []).append(str(row["entity_id"]))
        return links

    def relations_for(self, fact_id: int) -> list[dict]:
        rows = self.conn.execute(
            """SELECT source_fact_id, target_fact_id, relation, confidence, created_at
               FROM memory_relations
               WHERE source_fact_id = ? OR target_fact_id = ?
               ORDER BY created_at DESC""",
            (int(fact_id), int(fact_id)),
        ).fetchall()
        return [dict(row) for row in rows]

    def update(
        self,
        fact_id: int,
        *,
        fact_text: str | None = None,
        category: str | None = None,
        confidence: float | None = None,
        importance: float | None = None,
        source: str | None = None,
        session_id: str | None | object = _UNSET,
        source_conversation_id: str | None | object = _UNSET,
        source_reflection_id: str | None | object = _UNSET,
        source_message_id: str | None | object = _UNSET,
        tags: list[str] | tuple[str, ...] | object = _UNSET,
        valid_from: str | None | object = _UNSET,
        expires_at: str | None | object = _UNSET,
        project: str | None | object = _UNSET,
        links: dict[str, object] | object = _UNSET,
        append: bool = False,
        mark_outdated: bool | None = None,
        expected_revision: int | None = None,
        change_summary: str = "",
    ) -> bool:
        """Update a memory and refresh search indexes when text changes."""
        existing = self.get(fact_id)
        if not existing:
            return False

        current_revision = int(existing.get("revision") or 1)
        if expected_revision is not None and current_revision != int(expected_revision):
            raise MemoryConflictError(
                f"Memory #{fact_id} changed since revision {expected_revision}; current revision is {current_revision}."
            )

        if append and fact_text:
            addition = str(fact_text).strip()
            new_text = f"{existing['fact_text'].rstrip()}\n{addition}" if addition else existing["fact_text"]
        else:
            new_text = fact_text if fact_text is not None else existing["fact_text"]
        updates = {
            "fact_text": new_text,
            "category": category if category is not None else existing["category"],
            "confidence": confidence if confidence is not None else existing["confidence"],
            "importance": importance if importance is not None else existing.get("importance", 0.5),
            "source": source if source is not None else existing.get("source", "conversation"),
            "session_id": existing.get("session_id") if session_id is _UNSET else session_id,
            "source_conversation_id": (
                existing.get("source_conversation_id")
                if source_conversation_id is _UNSET else source_conversation_id
            ),
            "source_reflection_id": (
                existing.get("source_reflection_id")
                if source_reflection_id is _UNSET else source_reflection_id
            ),
            "source_message_id": existing.get("source_message_id") if source_message_id is _UNSET else source_message_id,
            "tags": existing.get("tags", []) if tags is _UNSET else self._normalize_tags(tags),
            "valid_from": existing.get("valid_from") if valid_from is _UNSET else valid_from,
            "expires_at": existing.get("expires_at") if expires_at is _UNSET else expires_at,
            "project": existing.get("project") if project is _UNSET else project,
            "outdated_at": (
                existing.get("outdated_at") if mark_outdated is None
                else (utc_now() if mark_outdated else None)
            ),
        }
        self.conn.execute(
            """INSERT OR IGNORE INTO memory_revisions
               (fact_id, revision, snapshot_json, change_summary, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (
                int(fact_id), current_revision,
                json.dumps(existing, ensure_ascii=False, sort_keys=True, default=str),
                str(change_summary or ("append" if append else "update"))[:500], utc_now(),
            ),
        )
        self.conn.execute(
            """UPDATE facts_meta
               SET fact_text = ?, category = ?, confidence = ?, importance = ?,
                   source = ?, session_id = ?, source_conversation_id = ?,
                   source_reflection_id = ?, source_message_id = ?, tags_json = ?,
                   valid_from = ?, expires_at = ?, project = ?, outdated_at = ?,
                   revision = revision + 1, updated_at = datetime('now')
               WHERE fact_id = ?""",
            (
                updates["fact_text"],
                updates["category"],
                updates["confidence"],
                updates["importance"],
                updates["source"],
                updates["session_id"],
                updates["source_conversation_id"],
                updates["source_reflection_id"],
                updates["source_message_id"],
                json.dumps(updates["tags"], ensure_ascii=False),
                updates["valid_from"],
                updates["expires_at"],
                updates["project"],
                updates["outdated_at"],
                fact_id,
            ),
        )

        if links is not _UNSET:
            self._replace_links(fact_id, links)

        if new_text != existing["fact_text"]:
            if self.vector_enabled:
                embedding = self._embed(new_text)
                self.conn.execute("DELETE FROM user_facts WHERE rowid = ?", (fact_id,))
                self.conn.execute(
                    "INSERT INTO user_facts (rowid, embedding) VALUES (?, ?)",
                    (fact_id, embedding),
                )
            self._replace_fts_entry(fact_id, existing["fact_text"], new_text)

        self.conn.commit()
        return True

    def revision_history(self, fact_id: int) -> list[dict]:
        rows = self.conn.execute(
            "SELECT revision, snapshot_json, change_summary, created_at FROM memory_revisions WHERE fact_id = ? ORDER BY revision DESC",
            (int(fact_id),),
        ).fetchall()
        history: list[dict] = []
        for row in rows:
            try:
                snapshot = json.loads(row["snapshot_json"])
            except (TypeError, json.JSONDecodeError):
                snapshot = {}
            history.append({
                "revision": int(row["revision"]),
                "snapshot": snapshot,
                "change_summary": row["change_summary"],
                "created_at": row["created_at"],
            })
        current = self.get(int(fact_id))
        if current:
            history.insert(0, {
                "revision": int(current.get("revision") or 1),
                "snapshot": current,
                "change_summary": "current",
                "created_at": current.get("updated_at"),
            })
        return history

    def _rank_score(self, base_score: float, meta: sqlite3.Row) -> float:
        """Blend retrieval score with importance, confidence, age, and use."""
        importance = float(meta["importance"] if "importance" in meta.keys() else 0.5)
        raw_confidence = meta["confidence"] if "confidence" in meta.keys() else None
        # 0.0 is an explicit user signal, not a missing value.
        confidence = 1.0 if raw_confidence is None else float(raw_confidence)
        access_count = int(meta["access_count"] or 0)
        created = _parse_db_datetime(meta["created_at"])
        age_days = 0.0
        if created:
            age_days = max((datetime.now(timezone.utc) - created.astimezone(timezone.utc)).days, 0)

        # Lower scores rank first. Older, low-importance memories get a small
        # positive age term; importance/confidence/access subtract from score.
        age_term = min(age_days, 365) / 365 * (1.0 - importance) * 0.05
        access_boost = min(access_count, 20) * 0.002
        importance_boost = importance * 0.05
        confidence_boost = confidence * 0.02
        return base_score + age_term - importance_boost - confidence_boost - access_boost

    @staticmethod
    def _temporal_decay(meta: sqlite3.Row, enabled: bool) -> float:
        """Return category-aware retention without aging evergreen facts."""

        if not enabled:
            return 1.0
        category = str(meta["category"] or "note").casefold()
        half_life_days: dict[str, float | None] = {
            "identity": None,
            "fact": None,
            "preference": 3_650.0,
            "relationship": 3_650.0,
            "habit": 1_460.0,
            "belief": 730.0,
            "project": 365.0,
            "plan": 60.0,
            "note": 45.0,
            "observation": 21.0,
        }
        half_life = half_life_days.get(category, 180.0)
        if half_life is None:
            return 1.0
        created = _parse_db_datetime(meta["updated_at"] or meta["created_at"])
        if created is None:
            return 1.0
        age_days = max(
            (datetime.now(timezone.utc) - created.astimezone(timezone.utc)).total_seconds() / 86_400,
            0.0,
        )
        return max(0.05, min(1.0, 0.5 ** (age_days / half_life)))

    @staticmethod
    def _metadata_relevance(meta: sqlite3.Row, query_tokens: set[str]) -> float:
        importance = max(0.0, min(float(meta["importance"] or 0.5), 1.0))
        raw_confidence = meta["confidence"]
        confidence = 1.0 if raw_confidence is None else max(0.0, min(float(raw_confidence), 1.0))
        fact_tokens = {token.casefold() for token in _WORD_RE.findall(str(meta["fact_text"] or ""))}
        exact = 1.0 if query_tokens and query_tokens.issubset(fact_tokens) else 0.0
        access = min(math.log1p(max(0, int(meta["access_count"] or 0))) / math.log(21), 1.0)
        project = str(meta["project"] or "").casefold()
        project_match = 1.0 if project and project in " ".join(sorted(query_tokens)) else 0.0
        return min(
            1.0,
            0.40 * importance
            + 0.30 * confidence
            + 0.15 * exact
            + 0.05 * access
            + 0.10 * project_match,
        )

    @staticmethod
    def _mmr_select(
        candidates: list[dict],
        *,
        limit: int,
        lambda_value: float,
    ) -> list[dict]:
        if len(candidates) <= 1 or limit <= 1:
            return candidates[:limit]
        remaining = list(candidates)
        selected: list[dict] = []
        weight = max(0.0, min(float(lambda_value), 1.0))
        while remaining and len(selected) < limit:
            best = None
            best_score = float("-inf")
            for candidate in remaining:
                diversity_penalty = max(
                    (
                        _token_overlap(
                            _normalize_memory_text(candidate.get("fact_text", "")),
                            _normalize_memory_text(item.get("fact_text", "")),
                        )
                        for item in selected
                    ),
                    default=0.0,
                )
                mmr_score = weight * float(candidate.get("_relevance") or 0.0) - (
                    1.0 - weight
                ) * diversity_penalty
                if mmr_score > best_score:
                    best = candidate
                    best_score = mmr_score
            if best is None:
                break
            best["_mmr_score"] = round(best_score, 6)
            selected.append(best)
            remaining.remove(best)
        return selected

    def search(
        self,
        query: str,
        limit: int = 5,
        scope: str = "all",
        session_id: str | None = None,
        recent_sessions: int = 3,
        *,
        semantic: bool | None = None,
        retrieval_config: Any | None = None,
        include_outdated: bool = False,
        query_vector: bytes | None = None,
    ) -> list[dict]:
        """Memory V3 retrieval: normalized fusion, decay, metadata, and MMR."""

        started = time.monotonic()
        query_text = " ".join(str(query or "").split()).strip()
        bounded_limit = max(1, min(int(limit), 100))
        config = retrieval_config or self.retrieval_config
        max_candidates = max(
            bounded_limit,
            min(int(getattr(config, "max_candidates", 40)), 200),
        )
        vector_weight = max(0.0, float(getattr(config, "vector_weight", 0.55)))
        keyword_weight = max(0.0, float(getattr(config, "keyword_weight", 0.30)))
        metadata_weight = max(0.0, float(getattr(config, "metadata_weight", 0.15)))
        weight_total = vector_weight + keyword_weight + metadata_weight
        if weight_total <= 0:
            vector_weight, keyword_weight, metadata_weight, weight_total = 0.55, 0.30, 0.15, 1.0
        vector_weight /= weight_total
        keyword_weight /= weight_total
        metadata_weight /= weight_total
        use_semantic = (
            self._should_use_semantic_search(query_text)
            if semantic is None
            else bool(semantic)
        )
        results: dict[int, dict[str, Any]] = {}
        diagnostics: dict[str, Any] = {
            "query": query_text,
            "scope": scope,
            "vector": "disabled",
            "fts": "not-run",
            "mode": "degraded",
            "fusion_weights": {
                "vector": round(vector_weight, 4),
                "keyword": round(keyword_weight, 4),
                "metadata": round(metadata_weight, 4),
            },
            "max_candidates": max_candidates,
        }
        if not query_text:
            self.last_search_diagnostics = {**diagnostics, "fallback": "empty-query", "elapsed_ms": 0.0}
            return []

        lifecycle_filter = "AND m.archived_at IS NULL"
        if not include_outdated:
            lifecycle_filter += """ AND m.outdated_at IS NULL
                AND (m.expires_at IS NULL OR datetime(m.expires_at) > datetime('now'))
                AND (m.valid_from IS NULL OR datetime(m.valid_from) <= datetime('now'))"""
        session_filter = ""
        session_params: list[object] = []
        if scope == "session" and session_id:
            session_filter = """AND (m.session_id = ? OR m.session_id IS NULL
                OR m.session_id IN (
                    SELECT DISTINCT session_id FROM facts_meta
                    WHERE session_id IS NOT NULL
                    ORDER BY created_at DESC LIMIT ?
                ))"""
            session_params = [session_id, recent_sessions]

        vector_rows: list[sqlite3.Row] = []
        if self.vector_enabled and use_semantic:
            try:
                query_embedding = (
                    query_vector
                    if query_vector is not None
                    else self._embed_query(query_text)
                )
                vector_rows = self.conn.execute(
                    """SELECT rowid, distance FROM user_facts
                       WHERE embedding MATCH ? ORDER BY distance LIMIT ?""",
                    (query_embedding, max(max_candidates * 4, 100)),
                ).fetchall()
                if vector_rows:
                    ids = [int(row["rowid"]) for row in vector_rows]
                    placeholders = ",".join("?" for _ in ids)
                    valid_rows = self.conn.execute(
                        f"""SELECT fact_id FROM facts_meta AS m
                            WHERE fact_id IN ({placeholders}) {lifecycle_filter} {session_filter}""",
                        [*ids, *session_params],
                    ).fetchall()
                    valid_ids = {int(row["fact_id"]) for row in valid_rows}
                    vector_rows = [row for row in vector_rows if int(row["rowid"]) in valid_ids][:max_candidates]
                for rank, row in enumerate(vector_rows, start=1):
                    results[int(row["rowid"])] = {
                        "vector_rank": rank,
                        "vector_score": 1.0 / rank,
                        "distance": float(row["distance"]),
                        "source": "vector",
                    }
                diagnostics["vector"] = "ok" if vector_rows else "no-results"
            except Exception as exc:
                diagnostics["vector"] = f"failed: {type(exc).__name__}"
                logger.debug("Vector memory search failed; using FTS: %s", exc)
        elif self.vector_enabled:
            diagnostics["vector"] = "skipped-trivial" if semantic is None else "skipped-by-request"

        fts_rows: list[sqlite3.Row] = []
        structured_error: Exception | None = None
        queries: list[tuple[str, str]] = [(query_text, "structured")]
        literal_terms = re.findall(r"[\w]+", query_text, flags=re.UNICODE)
        if literal_terms:
            literal_query = " AND ".join(
                f'"{term.replace(chr(34), chr(34) * 2)}"' for term in literal_terms
            )
            if literal_query != query_text:
                queries.append((literal_query, "literal"))
            literal_any_query = " OR ".join(
                f'"{term.replace(chr(34), chr(34) * 2)}"' for term in literal_terms
            )
            if literal_any_query not in {query_text, literal_query}:
                queries.append((literal_any_query, "literal-any"))
        for fts_query, mode in queries:
            try:
                fts_rows = self.conn.execute(
                    f"""SELECT facts_fts.rowid AS rowid, facts_fts.rank AS rank
                        FROM facts_fts
                        JOIN facts_meta AS m ON m.fact_id=facts_fts.rowid
                        WHERE facts_fts MATCH ? {lifecycle_filter} {session_filter}
                        ORDER BY facts_fts.rank LIMIT ?""",
                    [fts_query, *session_params, max_candidates],
                ).fetchall()
                diagnostics["fts"] = mode if fts_rows else f"{mode}: no-results"
                if fts_rows or mode == "literal-any":
                    break
            except Exception as exc:
                structured_error = exc
                diagnostics["fts"] = f"{mode} failed: {type(exc).__name__}"
                logger.debug("FTS memory %s query failed: %s", mode, exc)
        for rank, row in enumerate(fts_rows, start=1):
            fact_id = int(row["rowid"])
            result = results.setdefault(fact_id, {"source": "fts"})
            result["keyword_rank"] = rank
            result["keyword_score"] = 1.0 / rank
            result["fts_rank"] = float(row["rank"])
            if result.get("source") == "vector":
                result["source"] = "both"

        if vector_rows and fts_rows:
            diagnostics["mode"] = "hybrid"
        elif vector_rows:
            diagnostics["mode"] = "vector"
        elif fts_rows:
            diagnostics["mode"] = "fts"
        elif structured_error is not None:
            diagnostics["mode"] = "degraded"
        diagnostics["candidate_counts"] = {
            "vector": len(vector_rows),
            "fts": len(fts_rows),
            "fused": len(results),
        }
        if not results:
            diagnostics["selected_ids"] = []
            diagnostics["elapsed_ms"] = round((time.monotonic() - started) * 1_000, 3)
            self.last_search_diagnostics = diagnostics
            return []

        row_ids = list(results)
        placeholders = ",".join("?" for _ in row_ids)
        meta_rows = self.conn.execute(
            f"""SELECT * FROM facts_meta AS m WHERE fact_id IN ({placeholders})
                {lifecycle_filter}""",
            row_ids,
        ).fetchall()
        query_tokens = {token.casefold() for token in _WORD_RE.findall(query_text)}
        decay_enabled = bool(getattr(config, "temporal_decay_enabled", True))
        enriched: list[dict] = []
        decay_effects: list[dict[str, Any]] = []
        for meta in meta_rows:
            channels = results[int(meta["fact_id"])]
            vector_score = float(channels.get("vector_score") or 0.0)
            keyword_score = float(channels.get("keyword_score") or 0.0)
            metadata_score = self._metadata_relevance(meta, query_tokens)
            fused = (
                vector_weight * vector_score
                + keyword_weight * keyword_score
                + metadata_weight * metadata_score
            )
            decay = self._temporal_decay(meta, decay_enabled)
            relevance = max(0.0, min(fused * decay, 1.0))
            entry = self._row_to_memory(meta)
            entry.update({
                "_source": channels.get("source"),
                "_vector_rank": channels.get("vector_rank"),
                "_keyword_rank": channels.get("keyword_rank"),
                "_metadata_score": round(metadata_score, 6),
                "_temporal_decay": round(decay, 6),
                "_relevance": round(relevance, 6),
                # Compatibility: callers historically treated lower _score as better.
                "_score": round(1.0 - relevance, 6),
            })
            decay_effects.append({"fact_id": int(meta["fact_id"]), "factor": round(decay, 6)})
            enriched.append(entry)
        enriched.sort(key=lambda item: (-float(item["_relevance"]), int(item["fact_id"])))
        candidate_pool = enriched[:max_candidates]
        if bool(getattr(config, "mmr_enabled", True)) and len(candidate_pool) > 2:
            selected = self._mmr_select(
                candidate_pool,
                limit=bounded_limit,
                lambda_value=float(getattr(config, "mmr_lambda", 0.70)),
            )
            diagnostics["mmr"] = {
                "enabled": True,
                "lambda": float(getattr(config, "mmr_lambda", 0.70)),
                "selected_ids": [int(item["fact_id"]) for item in selected],
            }
        else:
            selected = candidate_pool[:bounded_limit]
            diagnostics["mmr"] = {"enabled": False, "selected_ids": [int(item["fact_id"]) for item in selected]}
        diagnostics["temporal_decay_effects"] = decay_effects
        diagnostics["selected_ids"] = [int(item["fact_id"]) for item in selected]
        diagnostics["elapsed_ms"] = round((time.monotonic() - started) * 1_000, 3)
        self.last_search_diagnostics = diagnostics
        self._queue_access_stats([int(entry["fact_id"]) for entry in selected])
        self._queue_candidate_feedback(selected, query_text)
        return selected

    def explain_last_retrieval(self) -> dict[str, Any]:
        """Return safe retrieval diagnostics without private memory content."""

        return dict(getattr(self, "last_search_diagnostics", {}) or {})

    def search_advanced(
        self,
        query: str = "",
        *,
        mode: str = "relevant",
        limit: int = 12,
        memory_id: int | None = None,
        task: str = "",
        filters: dict[str, object] | None = None,
    ) -> list[dict]:
        """Search durable facts using temporal, relationship, and task modes."""
        selected_mode = str(mode or "relevant").strip().casefold()
        allowed = {"relevant", "timeline", "related", "contradictions", "changes", "task_context"}
        if selected_mode not in allowed:
            raise ValueError(f"mode must be one of: {', '.join(sorted(allowed))}")
        bounded = max(1, min(int(limit), 100))
        criteria = dict(filters or {})

        if selected_mode == "changes":
            if memory_id is None:
                raise ValueError("memory_id is required for changes mode")
            return [{
                "fact_id": int(memory_id),
                "match_reason": "revision history for the requested memory",
                "history": self.revision_history(int(memory_id)),
            }]

        if selected_mode in {"related", "contradictions"}:
            if memory_id is None:
                raise ValueError(f"memory_id is required for {selected_mode} mode")
            target = self.get(int(memory_id))
            if target is None:
                return []
            candidate_ids: set[int] = set()
            for relation in target.get("related_memories", []):
                if selected_mode == "contradictions" and relation.get("relation") != "contradiction":
                    continue
                other = int(relation["target_fact_id"] if int(relation["source_fact_id"]) == int(memory_id) else relation["source_fact_id"])
                candidate_ids.add(other)
            if selected_mode == "related":
                for kind, values in target.get("links", {}).items():
                    for entity_id in values:
                        rows = self.conn.execute(
                            "SELECT fact_id FROM memory_links WHERE entity_type = ? AND entity_id = ? AND fact_id != ?",
                            (kind, entity_id, int(memory_id)),
                        ).fetchall()
                        candidate_ids.update(int(row["fact_id"]) for row in rows)
            records = [self.get(candidate_id) for candidate_id in candidate_ids]
            output = [record for record in records if record]
            for record in output:
                record["match_reason"] = (
                    "stored contradiction relation" if selected_mode == "contradictions"
                    else "shared entity link or stored memory relation"
                )
            return self._filter_memories(output, criteria)[:bounded]

        search_text = str(task or query).strip() if selected_mode == "task_context" else str(query or "").strip()
        if search_text:
            records = self.search(
                search_text,
                limit=max(bounded * 4, 40),
                include_outdated=bool(criteria.get("include_outdated", False)),
            )
        else:
            records = self.list_all()
        records = self._filter_memories(records, criteria)
        if selected_mode == "timeline":
            records.sort(key=lambda item: (str(item.get("valid_from") or item.get("created_at") or ""), int(item["fact_id"])))
        for record in records:
            created = _parse_db_datetime(record.get("created_at"))
            record["age_days"] = max((datetime.now(timezone.utc) - created).days, 0) if created else None
            if selected_mode == "timeline":
                reason = "chronological durable-memory timeline"
            elif selected_mode == "task_context":
                reason = f"semantic or keyword match for task: {search_text}"
            else:
                reason = f"semantic or keyword match for query: {search_text}" if search_text else "recent durable memory"
            record["match_reason"] = reason
            record["source_age"] = {"source": record.get("source"), "age_days": record["age_days"]}
        return records[:bounded]

    @staticmethod
    def _filter_memories(records: list[dict], filters: dict[str, object]) -> list[dict]:
        tags = {str(value).casefold() for value in (filters.get("tags") or [])}
        categories = filters.get("category") or filters.get("categories") or []
        if isinstance(categories, str):
            categories = [categories]
        category_set = {str(value).casefold() for value in categories}
        minimum_confidence = float(filters.get("min_confidence", 0.0) or 0.0)
        minimum_importance = float(filters.get("min_importance", 0.0) or 0.0)
        source = str(filters.get("source") or "").casefold()
        project = str(filters.get("project") or "").casefold()
        date_from = _parse_db_datetime(str(filters.get("date_from") or ""))
        date_to = _parse_db_datetime(str(filters.get("date_to") or ""))
        link_filters = {
            kind: str(filters.get(kind) or "")
            for kind in ("person", "goal", "action", "file")
            if filters.get(kind) not in (None, "")
        }
        include_outdated = bool(filters.get("include_outdated", False))
        output: list[dict] = []
        for record in records:
            if not include_outdated and record.get("may_be_outdated"):
                continue
            if category_set and str(record.get("category") or "").casefold() not in category_set:
                continue
            record_tags = {str(value).casefold() for value in record.get("tags", [])}
            if tags and not tags.issubset(record_tags):
                continue
            if float(record.get("confidence") or 0.0) < minimum_confidence:
                continue
            if float(record.get("importance") or 0.0) < minimum_importance:
                continue
            if source and str(record.get("source") or "").casefold() != source:
                continue
            if project and str(record.get("project") or "").casefold() != project:
                continue
            created = _parse_db_datetime(record.get("created_at"))
            if date_from and (created is None or created < date_from):
                continue
            if date_to and (created is None or created > date_to):
                continue
            links = record.get("links", {})
            if any(value not in {str(item) for item in links.get(kind, [])} for kind, value in link_filters.items()):
                continue
            output.append(record)
        return output

    def merge_memories(
        self,
        target_id: int,
        source_ids: list[int],
        *,
        expected_revision: int | None = None,
    ) -> dict:
        """Merge source memories into a target while preserving provenance."""
        target = self.get(int(target_id))
        if target is None:
            raise ValueError(f"Memory #{target_id} was not found.")
        sources = [self.get(int(source_id)) for source_id in source_ids if int(source_id) != int(target_id)]
        if any(source is None for source in sources):
            raise ValueError("One or more source memories were not found.")
        source_records = [source for source in sources if source]
        lines = [str(target["fact_text"]).strip()]
        for source in source_records:
            text = str(source["fact_text"]).strip()
            if text and _normalize_memory_text(text) not in {_normalize_memory_text(line) for line in lines}:
                lines.append(text)
        tags = self._normalize_tags([*target.get("tags", []), *(tag for item in source_records for tag in item.get("tags", []))])
        links: dict[str, list[str]] = {kind: list(values) for kind, values in target.get("links", {}).items()}
        for source in source_records:
            for kind, values in source.get("links", {}).items():
                links.setdefault(kind, [])
                links[kind] = list(dict.fromkeys([*links[kind], *values]))
        self.update(
            int(target_id), fact_text="\n".join(lines), tags=tags, links=links,
            expected_revision=expected_revision, change_summary=f"merged memories {source_ids}",
        )
        for source in source_records:
            source_id = int(source["fact_id"])
            self.update(source_id, mark_outdated=True, change_summary=f"merged into memory {target_id}")
            self.conn.execute("UPDATE facts_meta SET superseded_by = ? WHERE fact_id = ?", (int(target_id), source_id))
            self._add_relation(int(target_id), source_id, "merged_from", 1.0)
        self.conn.commit()
        return self.get(int(target_id)) or {}

    def delete(self, fact_id: int) -> bool:
        """Delete a fact by ID. Returns True if deleted, False if not found."""
        existing = self.conn.execute(
            "SELECT fact_id, fact_text FROM facts_meta WHERE fact_id = ?", (fact_id,)
        ).fetchone()
        if not existing:
            return False

        self._delete_fts_entry(fact_id, existing["fact_text"])
        self.conn.execute("DELETE FROM facts_meta WHERE fact_id = ?", (fact_id,))
        self.conn.execute("DELETE FROM user_facts WHERE rowid = ?", (fact_id,))
        self._pending_access_counts.pop(int(fact_id), None)
        self.conn.execute("DELETE FROM memory_links WHERE fact_id = ?", (fact_id,))
        self.conn.execute("DELETE FROM memory_revisions WHERE fact_id = ?", (fact_id,))
        self.conn.execute(
            "DELETE FROM memory_relations WHERE source_fact_id = ? OR target_fact_id = ?",
            (fact_id, fact_id),
        )
        self.conn.commit()
        self._notify_deleted((int(fact_id),))
        return True

    def archive(self, fact_id: int, *, reason: str = "lifecycle_cleanup") -> bool:
        """Soft-delete an ordinary memory while preserving provenance."""

        existing = self.get(int(fact_id))
        if existing is None:
            return False
        if existing.get("archived_at"):
            return True
        self.conn.execute(
            """INSERT OR IGNORE INTO memory_revisions
               (fact_id, revision, snapshot_json, change_summary, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (
                int(fact_id),
                int(existing.get("revision") or 1),
                json.dumps(existing, ensure_ascii=False, sort_keys=True, default=str),
                f"archived: {str(reason or 'lifecycle_cleanup')[:400]}",
                utc_now(),
            ),
        )
        self.conn.execute(
            """UPDATE facts_meta SET archived_at=?, updated_at=datetime('now'),
               revision=revision+1 WHERE fact_id=?""",
            (utc_now(), int(fact_id)),
        )
        self.conn.commit()
        self._pending_access_counts.pop(int(fact_id), None)
        return True

    def restore(self, fact_id: int) -> bool:
        """Restore an archived memory to normal retrieval."""

        existing = self.get(int(fact_id))
        if existing is None or not existing.get("archived_at"):
            return False
        self.conn.execute(
            """INSERT OR IGNORE INTO memory_revisions
               (fact_id, revision, snapshot_json, change_summary, created_at)
               VALUES (?, ?, ?, 'restored from archive', ?)""",
            (
                int(fact_id),
                int(existing.get("revision") or 1),
                json.dumps(existing, ensure_ascii=False, sort_keys=True, default=str),
                utc_now(),
            ),
        )
        self.conn.execute(
            """UPDATE facts_meta SET archived_at=NULL, updated_at=datetime('now'),
               revision=revision+1 WHERE fact_id=?""",
            (int(fact_id),),
        )
        self.conn.commit()
        return True

    def list_all(self, *, include_archived: bool = False) -> list[dict]:
        """Return stored memories, excluding archived rows by default."""

        where = "" if include_archived else "WHERE archived_at IS NULL"
        rows = self.conn.execute(
            f"SELECT * FROM facts_meta {where} ORDER BY created_at DESC"
        ).fetchall()
        return [self._row_to_memory(r) for r in rows]

    def bulk_delete(self, fact_ids: list[int]) -> int:
        """Delete multiple facts by ID. Returns count deleted."""
        if not fact_ids:
            return 0
        unique_ids = list(dict.fromkeys(int(fact_id) for fact_id in fact_ids))
        placeholders = ",".join("?" * len(unique_ids))
        existing_rows = [
            row
            for row in self.conn.execute(
                f"SELECT fact_id, fact_text FROM facts_meta WHERE fact_id IN ({placeholders})", unique_ids
            ).fetchall()
        ]
        if not existing_rows:
            return 0
        existing = [int(row["fact_id"]) for row in existing_rows]
        existing_placeholders = ",".join("?" * len(existing))
        for row in existing_rows:
            self._delete_fts_entry(int(row["fact_id"]), str(row["fact_text"]))
        cursor = self.conn.execute(
            f"DELETE FROM facts_meta WHERE fact_id IN ({existing_placeholders})",
            existing,
        )
        for fid in existing:
            self.conn.execute("DELETE FROM user_facts WHERE rowid = ?", (fid,))
            self._pending_access_counts.pop(int(fid), None)
        self.conn.execute(f"DELETE FROM memory_links WHERE fact_id IN ({existing_placeholders})", existing)
        self.conn.execute(f"DELETE FROM memory_revisions WHERE fact_id IN ({existing_placeholders})", existing)
        self.conn.execute(
            f"DELETE FROM memory_relations WHERE source_fact_id IN ({existing_placeholders}) OR target_fact_id IN ({existing_placeholders})",
            [*existing, *existing],
        )
        self.conn.commit()
        self._notify_deleted(tuple(int(fact_id) for fact_id in existing))
        return max(0, int(cursor.rowcount))

    def find_similar_to(self, fact_id: int, limit: int = 5) -> list[dict]:
        """Find memories vector-similar to the given fact."""
        target = self.get(fact_id)
        if not target:
            return []
        results = self.search(target["fact_text"], limit=limit + 1)
        return [r for r in results if r["fact_id"] != fact_id][:limit]

    def import_memories(self, memories: list[dict]) -> int:
        """Import memories, skipping exact text/category duplicates."""
        existing = {
            (m["fact_text"], m.get("category", "note"))
            for m in self.list_all()
        }
        imported = 0
        for memory in memories:
            text = memory.get("fact_text") or memory.get("content")
            category = memory.get("category", "note")
            if not text or (text, category) in existing:
                continue
            self.store(
                text,
                category=category,
                confidence=float(memory.get("confidence", 1.0)),
                importance=float(memory.get("importance", 0.5)),
                source=memory.get("source", "import"),
                session_id=memory.get("session_id"),
                source_conversation_id=memory.get("source_conversation_id"),
                source_reflection_id=memory.get("source_reflection_id"),
                source_candidate_id=memory.get("source_candidate_id"),
            )
            existing.add((text, category))
            imported += 1
        return imported

    def get_recent(self, limit: int = 10, *, include_archived: bool = False) -> list[dict]:
        """Return recently created active memories unless explicitly requested."""

        where = "" if include_archived else "WHERE archived_at IS NULL"
        rows = self.conn.execute(
            f"SELECT * FROM facts_meta {where} ORDER BY created_at DESC, fact_id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [self._row_to_memory(r) for r in rows]

    def get_standing_memories(
        self,
        limit: int = 8,
        min_importance: float = 0.3,
    ) -> list[dict]:
        """Return the most important active memories for proactive context injection.

        These are memories that should be surfaced in every conversation turn
        regardless of the user's query — personality facts, preferences, habits,
        relationships, and high-importance notes that make Ares feel like it
        *knows* the user.
        """
        rows = self.conn.execute(
            """SELECT * FROM facts_meta
               WHERE archived_at IS NULL
                 AND outdated_at IS NULL
                 AND (expires_at IS NULL OR datetime(expires_at) > datetime('now'))
                 AND (valid_from IS NULL OR datetime(valid_from) <= datetime('now'))
                 AND importance >= ?
               ORDER BY importance DESC, confidence DESC, created_at DESC
               LIMIT ?""",
            (min_importance, limit),
        ).fetchall()
        return [self._row_to_memory(r) for r in rows]

    def count(self, *, include_archived: bool = False) -> int:
        """Return the active durable-memory count by default."""

        where = "" if include_archived else "WHERE archived_at IS NULL"
        row = self.conn.execute(f"SELECT COUNT(*) FROM facts_meta {where}").fetchone()
        return int(row[0]) if row else 0

    def recall_context(
        self,
        query: str,
        session_id: str | None = None,
        limit: int = 5,
    ) -> str:
        """FIX: Build context from long-term memory for recall requests.

        This method handles "do you remember" style queries by searching
        for entities and providing relevant context from durable memories.
        """
        context_parts: list[str] = []
        try:
            # Extract entities to search for
            entities = self._extract_recall_entities(query)
            for entity in entities[:3]:
                results = self.search(entity, limit=2, session_id=session_id, scope="all")
                for result in results:
                    fact_text = result.get("fact_text", "")
                    if fact_text and fact_text not in context_parts:
                        context_parts.append(f"- {fact_text}")

            # Also do a general semantic search
            general_results = self.search(query, limit=limit, session_id=session_id, scope="all")
            for result in general_results:
                fact_text = result.get("fact_text", "")
                if fact_text and fact_text not in context_parts:
                    context_parts.append(f"- {fact_text}")
        except Exception:
            pass

        if context_parts:
            return "## Long-term Memory Context\n" + "\n".join(context_parts[:limit])
        return ""

    @staticmethod
    def _extract_recall_entities(text: str) -> list[str]:
        """Extract entities that should be searched in long-term memory."""
        entities: list[str] = []
        # Extract quoted phrases
        quoted = re.findall(r'"([^"]+)"', text)
        entities.extend(quoted)
        # Extract capitalized words (likely proper nouns)
        proper_nouns = re.findall(r"\b[A-Z][a-z]+\b", text)
        entities.extend(proper_nouns)
        # Extract common entity patterns
        entity_patterns = (
            re.compile(r"\b(?:file|document|folder)\s+(?:called|named|titled)\s+(\S+)\b", re.I),
            re.compile(r"\b(?:email|message)\s+(?:to|from|about)\s+(\S+)\b", re.I),
            re.compile(r"\b(?:task|project)\s+(?:called|named|titled)\s+(\S+)\b", re.I),
        )
        for pattern in entity_patterns:
            matches = pattern.findall(text)
            entities.extend(matches)
        return list(dict.fromkeys(entities))[:5]  # Deduplicate, limit to 5

    def close(self):
        """Close the database connection."""
        try:
            self.flush_access_stats()
        finally:
            self._deletion_observers.clear()
            self.conn.close()


def _normalize_memory_text(text: str) -> str:
    return " ".join(_WORD_RE.findall((text or "").lower()))


def _memory_subject(normalized: str) -> str:
    stop = {"the", "a", "an", "is", "are", "was", "were", "likes", "prefers", "user", "users"}
    tokens = [token for token in normalized.split() if token not in stop]
    return " ".join(tokens[:3])


def _memory_relation(normalized: str) -> str:
    tokens = normalized.split()
    if len(tokens) >= 2 and tokens[0] in {"user", "users"}:
        return " ".join(tokens[:2])
    return _memory_subject(normalized)


def _token_overlap(left: str, right: str) -> float:
    left_tokens = set(left.split())
    right_tokens = set(right.split())
    if not left_tokens or not right_tokens:
        return 0.0
    return len(left_tokens & right_tokens) / max(len(left_tokens | right_tokens), 1)
