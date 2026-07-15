"""Memory system: SQLite + sqlite-vec for vector search + FTS5 for keyword search."""

from datetime import datetime, timezone
import logging
import re
import sqlite3
from pathlib import Path

import sqlite_vec

from ares.config import get_db_path, load_config
from ares.embeddings import (
    DEFAULT_EMBEDDING_MODEL,
    EMBEDDING_DIMS,
    EmbeddingProvider,
)
from ares.sqlite_utils import connect_sqlite

EMBEDDING_MODEL_NAME = DEFAULT_EMBEDDING_MODEL

_default_provider: EmbeddingProvider | None = None
logger = logging.getLogger(__name__)
_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_WORD_RE = re.compile(r"[A-Za-z0-9']+")


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
    ) -> int:
        """Store a new fact. Returns the fact_id."""
        # Insert metadata
        cursor = self.conn.execute(
            """INSERT INTO facts_meta
               (fact_text, category, confidence, importance, source, session_id,
                source_conversation_id, source_reflection_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                fact_text, category, confidence, importance, source, session_id,
                source_conversation_id, source_reflection_id,
            ),
        )
        fact_id = cursor.lastrowid

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

        self.conn.commit()
        return fact_id

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
        return dict(row) if row else None

    def update(
        self,
        fact_id: int,
        *,
        fact_text: str | None = None,
        category: str | None = None,
        confidence: float | None = None,
        importance: float | None = None,
        source: str | None = None,
    ) -> bool:
        """Update a memory and refresh search indexes when text changes."""
        existing = self.get(fact_id)
        if not existing:
            return False

        new_text = fact_text if fact_text is not None else existing["fact_text"]
        updates = {
            "fact_text": new_text,
            "category": category if category is not None else existing["category"],
            "confidence": confidence if confidence is not None else existing["confidence"],
            "importance": importance if importance is not None else existing.get("importance", 0.5),
            "source": source if source is not None else existing.get("source", "conversation"),
        }
        self.conn.execute(
            """UPDATE facts_meta
               SET fact_text = ?, category = ?, confidence = ?, importance = ?,
                   source = ?, updated_at = datetime('now')
               WHERE fact_id = ?""",
            (
                updates["fact_text"],
                updates["category"],
                updates["confidence"],
                updates["importance"],
                updates["source"],
                fact_id,
            ),
        )

        if new_text != existing["fact_text"]:
            if self.vector_enabled:
                embedding = self._embed(new_text)
                self.conn.execute("DELETE FROM user_facts WHERE rowid = ?", (fact_id,))
                self.conn.execute(
                    "INSERT INTO user_facts (rowid, embedding) VALUES (?, ?)",
                    (fact_id, embedding),
                )
            self.conn.execute("DELETE FROM facts_fts WHERE rowid = ?", (fact_id,))
            self.conn.execute(
                "INSERT INTO facts_fts (rowid, fact_text) VALUES (?, ?)",
                (fact_id, new_text),
            )

        self.conn.commit()
        return True

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
            entry = dict(meta)
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

    def delete(self, fact_id: int) -> bool:
        """Delete a fact by ID. Returns True if deleted, False if not found."""
        existing = self.conn.execute(
            "SELECT fact_id FROM facts_meta WHERE fact_id = ?", (fact_id,)
        ).fetchone()
        if not existing:
            return False

        self.conn.execute("DELETE FROM facts_meta WHERE fact_id = ?", (fact_id,))
        self.conn.execute("DELETE FROM user_facts WHERE rowid = ?", (fact_id,))
        self.conn.execute("DELETE FROM facts_fts WHERE rowid = ?", (fact_id,))
        self.conn.commit()
        return True

    def list_all(self) -> list[dict]:
        """Return all stored memories."""
        rows = self.conn.execute(
            "SELECT * FROM facts_meta ORDER BY created_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]

    def bulk_delete(self, fact_ids: list[int]) -> int:
        """Delete multiple facts by ID. Returns count deleted."""
        if not fact_ids:
            return 0
        unique_ids = list(dict.fromkeys(int(fact_id) for fact_id in fact_ids))
        placeholders = ",".join("?" * len(unique_ids))
        existing = [
            row["fact_id"]
            for row in self.conn.execute(
                f"SELECT fact_id FROM facts_meta WHERE fact_id IN ({placeholders})", unique_ids
            ).fetchall()
        ]
        if not existing:
            return 0
        existing_placeholders = ",".join("?" * len(existing))
        cursor = self.conn.execute(
            f"DELETE FROM facts_meta WHERE fact_id IN ({existing_placeholders})",
            existing,
        )
        for fid in existing:
            self.conn.execute("DELETE FROM user_facts WHERE rowid = ?", (fid,))
            self.conn.execute("DELETE FROM facts_fts WHERE rowid = ?", (fid,))
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
        return [dict(r) for r in rows]

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
