"""Memory system: SQLite + sqlite-vec for vector search + FTS5 for keyword search."""

from datetime import datetime, timezone
import sqlite3
from pathlib import Path

import sqlite_vec

from ares.config import get_db_path, load_config
from ares.embeddings import (
    DEFAULT_EMBEDDING_MODEL,
    EMBEDDING_DIMS,
    EmbeddingProvider,
)

EMBEDDING_MODEL_NAME = DEFAULT_EMBEDDING_MODEL

_default_provider: EmbeddingProvider | None = None


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
        self.conn = sqlite3.connect(str(self.db_path))
        self.conn.row_factory = sqlite3.Row
        self._init_db()

    def _init_db(self):
        """Initialize database tables if they don't exist."""
        self.conn.enable_load_extension(True)
        sqlite_vec.load(self.conn)
        self.conn.enable_load_extension(False)

        # Vector table for semantic search
        self.conn.execute("""
            CREATE VIRTUAL TABLE IF NOT EXISTS user_facts USING vec0(
                embedding float[384]
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
    ) -> int:
        """Store a new fact. Returns the fact_id."""
        # Insert metadata
        cursor = self.conn.execute(
            """INSERT INTO facts_meta (fact_text, category, confidence, importance, source)
               VALUES (?, ?, ?, ?, ?)""",
            (fact_text, category, confidence, importance, source),
        )
        fact_id = cursor.lastrowid

        # Insert embedding into vec0
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
        confidence = float(meta["confidence"] or 1.0)
        access_count = int(meta["access_count"] or 0)
        created = _parse_db_datetime(meta["created_at"])
        age_days = 0.0
        if created:
            age_days = max((datetime.now(timezone.utc) - created.astimezone(timezone.utc)).days, 0)

        age_penalty = min(age_days, 365) / 365 * (1.0 - importance) * 0.05
        access_boost = min(access_count, 20) * 0.002
        importance_boost = importance * 0.05
        confidence_boost = confidence * 0.02
        return base_score + age_penalty - importance_boost - confidence_boost - access_boost

    def search(self, query: str, limit: int = 5) -> list[dict]:
        """Hybrid search: vector similarity + FTS5 keyword match, merged."""
        results = {}

        # 1. Vector search (semantic)
        try:
            query_vec = self._embed(query)
            vec_rows = self.conn.execute(
                """
                SELECT rowid, distance FROM user_facts
                WHERE embedding MATCH ? ORDER BY distance LIMIT ?
                """,
                (query_vec, limit * 2),
            ).fetchall()
            for row in vec_rows:
                results[row["rowid"]] = {"distance": row["distance"], "source": "vector"}
        except Exception:
            pass  # vec0 may fail on empty table

        # 2. FTS5 keyword search
        try:
            fts_rows = self.conn.execute(
                """
                SELECT rowid, rank FROM facts_fts
                WHERE facts_fts MATCH ? ORDER BY rank LIMIT ?
                """,
                (query, limit * 2),
            ).fetchall()
            for row in fts_rows:
                rid = row["rowid"]
                if rid in results:
                    results[rid]["fts_rank"] = row["rank"]
                    results[rid]["source"] = "both"
                else:
                    results[rid] = {"fts_rank": row["rank"], "source": "fts"}
        except Exception:
            pass  # FTS may fail on empty table or bad query

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
        for entry in enriched[:limit]:
            self.conn.execute(
                "UPDATE facts_meta SET last_accessed = datetime('now'), access_count = access_count + 1 WHERE fact_id = ?",
                (entry["fact_id"],),
            )
        self.conn.commit()

        return enriched[:limit]

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

    def close(self):
        """Close the database connection."""
        self.conn.close()
