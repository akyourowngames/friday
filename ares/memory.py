"""Memory system: SQLite + sqlite-vec for vector search + FTS5 for keyword search."""

from datetime import datetime, timezone
import json
import logging
import re
import sqlite3
from pathlib import Path

import sqlite_vec

from ares.config import get_db_path, load_config
from ares.embeddings import (
    DEFAULT_EMBEDDING_MODEL,
    EmbeddingProvider,
)
from ares.sqlite_utils import connect_sqlite

EMBEDDING_MODEL_NAME = DEFAULT_EMBEDDING_MODEL

_default_provider: EmbeddingProvider | None = None
logger = logging.getLogger(__name__)
_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_WORD_RE = re.compile(r"[A-Za-z0-9']+")
_UNSET = object()


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
    ):
        config = load_config()
        self.db_path = db_path or get_db_path()
        self.embedding_provider = embedding_provider or EmbeddingProvider(
            model_name=embedding_model or config.embedding_model,
            backend=embedding_backend or config.embedding_backend,
            provider=config.embedding_provider,
            file_name=config.embedding_file_name,
        )
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = connect_sqlite(self.db_path)
        self.vector_enabled = False
        self._init_db()

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
    ) -> int:
        """Store a new fact. Returns the fact_id."""
        # Insert metadata
        cursor = self.conn.execute(
            """INSERT INTO facts_meta
               (fact_text, category, confidence, importance, source, session_id,
                source_conversation_id, source_reflection_id, source_message_id,
                tags_json, valid_from, expires_at, project)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                fact_text, category, confidence, importance, source, session_id,
                source_conversation_id, source_reflection_id, source_message_id,
                json.dumps(self._normalize_tags(tags), ensure_ascii=False),
                valid_from, expires_at, project,
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
        if memory.get("outdated_at") or memory.get("superseded_by"):
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

    def search(self, query: str, limit: int = 5, scope: str = "all",
               session_id: str | None = None, recent_sessions: int = 3) -> list[dict]:
        """Hybrid search: vector similarity + FTS5 keyword match, merged.

        Args:
            query: Search text.
            limit: Max results.
            scope: "session" to search current + recent N, "all" for everything.
            session_id: Current session ID (required when scope="session").
            recent_sessions: Number of recent sessions to include (default 3).
        """
        results: dict[int, dict[str, object]] = {}
        bounded_limit = max(1, min(int(limit), 100))
        diagnostics: dict[str, object] = {"query": query, "scope": scope, "vector": "disabled", "fts": "not-run", "mode": "degraded"}

        # Build session filter for scoped search
        session_filter = ""
        session_params: list[object] = []
        if scope == "session" and session_id:
            session_filter = """AND (m.session_id = ? OR m.session_id IS NULL
                OR m.session_id IN (
                    SELECT DISTINCT session_id FROM facts_meta
                    WHERE session_id IS NOT NULL
                    ORDER BY created_at DESC
                    LIMIT ?
                ))"""
            session_params = [session_id, recent_sessions]

        # 1. Vector search (semantic)
        if self.vector_enabled:
            try:
                query_vec = self._embed(query)
                # Pull a larger bounded candidate window when a vec0 join is
                # unavailable, then apply the scope before accepting entries.
                candidate_limit = max(bounded_limit * 20, 100)
                vec_rows = self.conn.execute(
                    """
                    SELECT rowid, distance FROM user_facts
                    WHERE embedding MATCH ? ORDER BY distance LIMIT ?
                    """,
                    (query_vec, candidate_limit),
                ).fetchall()
                if session_filter and vec_rows:
                    meta_rows = self.conn.execute(
                        f"SELECT fact_id FROM facts_meta AS m WHERE fact_id IN ({','.join('?' for _ in vec_rows)}) {session_filter}",
                        [row["rowid"] for row in vec_rows] + session_params,
                    ).fetchall()
                    valid_ids = {row["fact_id"] for row in meta_rows}
                    vec_rows = [row for row in vec_rows if row["rowid"] in valid_ids]
                for row in vec_rows:
                    results[row["rowid"]] = {"distance": row["distance"], "source": "vector"}
                diagnostics["vector"] = "ok" if vec_rows else "no-results"
            except Exception as exc:
                diagnostics["vector"] = f"failed: {type(exc).__name__}"
                logger.debug("Vector memory search failed; falling back to FTS only: %s", exc)

        # 2. FTS5 keyword search
        candidate_limit = max(bounded_limit * 4, 50)
        structured_error = None
        fts_rows = []
        queries: list[tuple[str, str]] = [(str(query), "structured")]
        literal_terms = re.findall(r"[\w]+", str(query), flags=re.UNICODE)
        if literal_terms:
            literal_query = " AND ".join(f'"{term.replace(chr(34), chr(34) * 2)}"' for term in literal_terms)
            if literal_query != query:
                queries.append((literal_query, "literal"))
        for fts_query, mode in queries:
            try:
                fts_rows = self.conn.execute(
                    f"""
                    SELECT facts_fts.rowid AS rowid, facts_fts.rank AS rank
                    FROM facts_fts
                    JOIN facts_meta AS m ON m.fact_id = facts_fts.rowid
                    WHERE facts_fts MATCH ? {session_filter}
                    ORDER BY facts_fts.rank LIMIT ?
                    """,
                    [fts_query] + session_params + [candidate_limit],
                ).fetchall()
                diagnostics["fts"] = mode if fts_rows else f"{mode}: no-results"
                if fts_rows or mode == "literal":
                    break
            except Exception as exc:
                structured_error = exc
                diagnostics["fts"] = f"{mode} failed: {type(exc).__name__}"
                logger.debug("FTS memory %s query failed: %s", mode, exc)
                continue
        for row in fts_rows:
            rid = row["rowid"]
            if rid in results:
                results[rid]["fts_rank"] = row["rank"]
                results[rid]["source"] = "both"
            else:
                results[rid] = {"fts_rank": row["rank"], "source": "fts"}
        if fts_rows:
            diagnostics["mode"] = "hybrid" if any(value.get("source") == "both" for value in results.values()) else "fts"
        elif results:
            diagnostics["mode"] = "vector"
        elif structured_error:
            diagnostics["mode"] = "degraded"

        self.last_search_diagnostics = diagnostics

        if not results:
            return []

        # 3. Merge and fetch metadata
        row_ids = list(results.keys())
        placeholders = ",".join("?" * len(row_ids))
        meta_rows = self.conn.execute(
            f"SELECT * FROM facts_meta WHERE fact_id IN ({placeholders})",
            row_ids,
        ).fetchall()

        enriched = []
        for meta in meta_rows:
            entry = self._row_to_memory(meta)
            # Boost score: both sources > vector-only > fts-only
            src = results[meta["fact_id"]]["source"]
            if src == "both":
                base_score = 0
            elif "distance" in results[meta["fact_id"]]:
                base_score = results[meta["fact_id"]]["distance"]
            else:
                base_score = 0.5
            entry["_score"] = self._rank_score(base_score, meta)
            enriched.append(entry)

        # Sort by score (lower distance = more similar)
        enriched.sort(key=lambda x: x["_score"])

        # 4. Update access stats
        for entry in enriched[:bounded_limit]:
            self.conn.execute(
                "UPDATE facts_meta SET last_accessed = datetime('now'), access_count = access_count + 1 WHERE fact_id = ?",
                (entry["fact_id"],),
            )
        self.conn.commit()

        return enriched[:bounded_limit]

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
            records = self.search(search_text, limit=max(bounded * 4, 40))
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
        self.conn.execute("DELETE FROM memory_links WHERE fact_id = ?", (fact_id,))
        self.conn.execute("DELETE FROM memory_revisions WHERE fact_id = ?", (fact_id,))
        self.conn.execute(
            "DELETE FROM memory_relations WHERE source_fact_id = ? OR target_fact_id = ?",
            (fact_id, fact_id),
        )
        self.conn.commit()
        return True

    def list_all(self) -> list[dict]:
        """Return all stored memories."""
        rows = self.conn.execute(
            "SELECT * FROM facts_meta ORDER BY created_at DESC"
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
        self.conn.execute(f"DELETE FROM memory_links WHERE fact_id IN ({existing_placeholders})", existing)
        self.conn.execute(f"DELETE FROM memory_revisions WHERE fact_id IN ({existing_placeholders})", existing)
        self.conn.execute(
            f"DELETE FROM memory_relations WHERE source_fact_id IN ({existing_placeholders}) OR target_fact_id IN ({existing_placeholders})",
            [*existing, *existing],
        )
        self.conn.commit()
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
            )
            existing.add((text, category))
            imported += 1
        return imported

    def get_recent(self, limit: int = 10) -> list[dict]:
        """Return the most recently created memories."""
        rows = self.conn.execute(
            "SELECT * FROM facts_meta ORDER BY created_at DESC, fact_id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [self._row_to_memory(r) for r in rows]

    def count(self) -> int:
        """Return the total number of stored memories."""
        row = self.conn.execute("SELECT COUNT(*) FROM facts_meta").fetchone()
        return int(row[0]) if row else 0

    def close(self):
        """Close the database connection."""
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
