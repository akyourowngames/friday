from __future__ import annotations

import csv
import io
import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path


class FolderIndex:
    def __init__(self, database_path: str | Path):
        self.database_path = Path(database_path).expanduser().resolve()
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.database_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._fts_enabled = False
        self._init_schema()

    @property
    def fts_enabled(self) -> bool:
        return self._fts_enabled

    def close(self):
        with self._lock:
            self._conn.close()

    def _init_schema(self):
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS files (
                    id TEXT PRIMARY KEY,
                    path TEXT UNIQUE NOT NULL,
                    filename TEXT NOT NULL,
                    extension TEXT NOT NULL,
                    mime_type TEXT NOT NULL,
                    size_bytes INTEGER NOT NULL,
                    sha256 TEXT NOT NULL,
                    created_ts REAL NOT NULL,
                    modified_ts REAL NOT NULL,
                    indexed_ts REAL NOT NULL,
                    last_seen_ts REAL NOT NULL,
                    metadata_json TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    tags_json TEXT NOT NULL,
                    status TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS events (
                    id TEXT PRIMARY KEY,
                    timestamp REAL NOT NULL,
                    event_type TEXT NOT NULL,
                    file_id TEXT,
                    old_path TEXT,
                    new_path TEXT,
                    processed INTEGER NOT NULL DEFAULT 0,
                    payload_json TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS tags (
                    file_id TEXT NOT NULL,
                    tag TEXT NOT NULL,
                    source TEXT NOT NULL,
                    UNIQUE(file_id, tag, source)
                );

                CREATE TABLE IF NOT EXISTS file_contents (
                    file_id TEXT PRIMARY KEY,
                    content TEXT NOT NULL
                );
                """
            )
            try:
                self._conn.execute(
                    "CREATE VIRTUAL TABLE IF NOT EXISTS file_fts USING fts5(file_id UNINDEXED, path, filename, content)"
                )
                self._fts_enabled = True
            except sqlite3.OperationalError:
                self._fts_enabled = False
            self._conn.commit()

    def upsert_file(self, record: dict, content: str, tags: list[dict], event_type: str | None = None) -> dict:
        now = time.time()
        path = str(Path(record["path"]).expanduser().resolve())
        with self._lock:
            existing = self._conn.execute("SELECT * FROM files WHERE path = ?", (path,)).fetchone()
            file_id = existing["id"] if existing else uuid.uuid4().hex
            indexed_ts = existing["indexed_ts"] if existing else now
            incoming_hash = str(record.get("sha256", ""))
            unchanged = bool(existing and existing["sha256"] == incoming_hash)
            status = "active"
            tag_names = sorted({str(item["tag"]) for item in tags if item.get("tag")})
            payload = (
                file_id,
                path,
                str(record.get("filename", Path(path).name)),
                str(record.get("extension", Path(path).suffix.lower())),
                str(record.get("mime_type", "application/octet-stream")),
                int(record.get("size_bytes", 0)),
                incoming_hash,
                float(record.get("created_ts", now)),
                float(record.get("modified_ts", now)),
                float(indexed_ts),
                now,
                json.dumps(record.get("metadata", {}), ensure_ascii=False, sort_keys=True),
                str(record.get("summary", "")),
                json.dumps(tag_names, ensure_ascii=False),
                status,
            )
            self._conn.execute(
                """
                INSERT INTO files (
                    id, path, filename, extension, mime_type, size_bytes, sha256,
                    created_ts, modified_ts, indexed_ts, last_seen_ts, metadata_json,
                    summary, tags_json, status
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(path) DO UPDATE SET
                    filename=excluded.filename,
                    extension=excluded.extension,
                    mime_type=excluded.mime_type,
                    size_bytes=excluded.size_bytes,
                    sha256=excluded.sha256,
                    created_ts=excluded.created_ts,
                    modified_ts=excluded.modified_ts,
                    last_seen_ts=excluded.last_seen_ts,
                    metadata_json=excluded.metadata_json,
                    summary=excluded.summary,
                    tags_json=excluded.tags_json,
                    status=excluded.status
                """,
                payload,
            )
            self._conn.execute("DELETE FROM tags WHERE file_id = ?", (file_id,))
            for item in tags:
                tag = str(item.get("tag", "")).strip()
                source = str(item.get("source", "")).strip() or "auto"
                if tag:
                    self._conn.execute(
                        "INSERT OR IGNORE INTO tags(file_id, tag, source) VALUES (?, ?, ?)",
                        (file_id, tag, source),
                    )
            self._conn.execute(
                """
                INSERT INTO file_contents(file_id, content)
                VALUES (?, ?)
                ON CONFLICT(file_id) DO UPDATE SET content=excluded.content
                """,
                (file_id, content),
            )
            self._sync_fts(file_id, path, str(record.get("filename", Path(path).name)), content)
            snapshot = {
                "id": file_id,
                "path": path,
                "filename": str(record.get("filename", Path(path).name)),
                "extension": str(record.get("extension", Path(path).suffix.lower())),
                "mime_type": str(record.get("mime_type", "application/octet-stream")),
                "size_bytes": int(record.get("size_bytes", 0)),
                "sha256": incoming_hash,
                "tags": tag_names,
                "metadata": record.get("metadata", {}),
            }
            resolved_event = event_type
            if resolved_event is None:
                resolved_event = "FILE_UNCHANGED" if unchanged else ("FILE_MODIFIED" if existing else "FILE_CREATED")
            event = self._log_event_locked(
                resolved_event,
                file_id=file_id,
                old_path=None,
                new_path=path,
                payload={"unchanged": unchanged, "sha256": incoming_hash, "file": snapshot},
            )
            self._conn.commit()
            return {"file": self.get_file(file_id), "event": event, "unchanged": unchanged}

    def touch_directory(self, path: str | Path, event_type: str) -> dict:
        resolved = str(Path(path).expanduser().resolve())
        with self._lock:
            event = self._log_event_locked(event_type, None, None, resolved, {"path": resolved, "kind": "directory"})
            self._conn.commit()
            return event

    def mark_deleted(self, path: str | Path) -> dict:
        resolved = str(Path(path).expanduser().resolve())
        with self._lock:
            row = self._conn.execute("SELECT id FROM files WHERE path = ?", (resolved,)).fetchone()
            file_id = row["id"] if row else None
            if file_id:
                self._conn.execute("UPDATE files SET status = ?, last_seen_ts = ? WHERE id = ?", ("deleted", time.time(), file_id))
            event = self._log_event_locked("FILE_DELETED", file_id, resolved, None, {"path": resolved})
            self._conn.commit()
            return event

    def mark_moved(self, old_path: str | Path, new_path: str | Path) -> dict:
        old_resolved = str(Path(old_path).expanduser().resolve())
        new_resolved = str(Path(new_path).expanduser().resolve())
        with self._lock:
            row = self._conn.execute("SELECT id FROM files WHERE path = ?", (old_resolved,)).fetchone()
            file_id = row["id"] if row else None
            if file_id:
                self._conn.execute(
                    "UPDATE files SET path = ?, filename = ?, extension = ?, last_seen_ts = ? WHERE id = ?",
                    (new_resolved, Path(new_resolved).name, Path(new_resolved).suffix.lower(), time.time(), file_id),
                )
            event = self._log_event_locked("FILE_MOVED", file_id, old_resolved, new_resolved, {})
            self._conn.commit()
            return event

    def delete_file_record(self, file_id: str) -> bool:
        with self._lock:
            row = self._conn.execute("SELECT id FROM files WHERE id = ?", (file_id,)).fetchone()
            if row is None:
                return False
            self._conn.execute("DELETE FROM files WHERE id = ?", (file_id,))
            self._conn.execute("DELETE FROM tags WHERE file_id = ?", (file_id,))
            self._conn.execute("DELETE FROM file_contents WHERE file_id = ?", (file_id,))
            if self._fts_enabled:
                self._conn.execute("DELETE FROM file_fts WHERE file_id = ?", (file_id,))
            self._conn.commit()
            return True

    def add_user_tag(self, file_id: str, tag: str) -> dict | None:
        clean = str(tag or "").strip()
        if not clean:
            return None
        with self._lock:
            row = self._conn.execute("SELECT tags_json FROM files WHERE id = ?", (file_id,)).fetchone()
            if row is None:
                return None
            tags = set(_json_list(row["tags_json"]))
            tags.add(clean)
            self._conn.execute("UPDATE files SET tags_json = ? WHERE id = ?", (json.dumps(sorted(tags), ensure_ascii=False), file_id))
            self._conn.execute("INSERT OR IGNORE INTO tags(file_id, tag, source) VALUES (?, ?, ?)", (file_id, clean, "user"))
            self._conn.commit()
            return self.get_file(file_id)

    def get_file(self, file_id: str) -> dict | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM files WHERE id = ?", (file_id,)).fetchone()
            return self._row_to_file(row) if row else None

    def get_content(self, file_id: str) -> str | None:
        with self._lock:
            row = self._conn.execute("SELECT content FROM file_contents WHERE file_id = ?", (file_id,)).fetchone()
            return row["content"] if row else None

    def latest(self, limit: int = 10, extension: str | None = None, since: float | None = None, directory: str | None = None) -> list[dict]:
        limit = _bounded_limit(limit)
        clauses = ["status = ?"]
        params: list[object] = ["active"]
        if extension:
            ext = str(extension).strip().lower()
            if ext and not ext.startswith("."):
                ext = "." + ext
            clauses.append("extension = ?")
            params.append(ext)
        if since is not None:
            clauses.append("indexed_ts >= ?")
            params.append(float(since))
        if directory:
            clean = str(directory).strip().replace("\\", "/").strip("/")
            if clean:
                clauses.append("path LIKE ?")
                params.append("%/" + clean + "/%")
        query = "SELECT * FROM files WHERE " + " AND ".join(clauses) + " ORDER BY indexed_ts DESC, modified_ts DESC LIMIT ?"
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(query, params).fetchall()
            return [self._row_to_file(row) for row in rows]

    def diff(self, since: float | None = None, from_ts: float | None = None, to_ts: float | None = None, limit: int = 200) -> list[dict]:
        clauses: list[str] = []
        params: list[object] = []
        if since is not None:
            clauses.append("timestamp > ?")
            params.append(float(since))
        if from_ts is not None:
            clauses.append("timestamp >= ?")
            params.append(float(from_ts))
        if to_ts is not None:
            clauses.append("timestamp <= ?")
            params.append(float(to_ts))
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        query = "SELECT * FROM events" + where + " ORDER BY timestamp ASC LIMIT ?"
        params.append(_bounded_limit(limit, 1, 1000))
        with self._lock:
            rows = self._conn.execute(query, params).fetchall()
            return [self._row_to_event(row) for row in rows]

    def search(self, query_text: str, limit: int = 20) -> list[dict]:
        terms = _plain_terms(query_text)
        if not terms:
            return []
        limit = _bounded_limit(limit, 1, 100)
        with self._lock:
            if self._fts_enabled:
                fts_query = " ".join(terms)
                try:
                    rows = self._conn.execute(
                        """
                        SELECT files.*, snippet(file_fts, 3, '[', ']', '...', 16) AS snippet,
                               bm25(file_fts) AS rank
                        FROM file_fts
                        JOIN files ON files.id = file_fts.file_id
                        WHERE file_fts MATCH ? AND files.status = ?
                        ORDER BY rank
                        LIMIT ?
                        """,
                        (fts_query, "active", limit),
                    ).fetchall()
                    return [self._row_to_file(row, extra={"snippet": row["snippet"], "rank": row["rank"]}) for row in rows]
                except sqlite3.OperationalError:
                    pass
            rows = self._conn.execute(
                """
                SELECT files.*, file_contents.content AS content
                FROM files
                JOIN file_contents ON files.id = file_contents.file_id
                WHERE files.status = ?
                """,
                ("active",),
            ).fetchall()
            matches = []
            for row in rows:
                haystack = " ".join([row["path"], row["filename"], row["content"]]).lower()
                if all(term.lower() in haystack for term in terms):
                    snippet = _snippet(row["content"], terms)
                    matches.append(self._row_to_file(row, extra={"snippet": snippet, "rank": 0}))
                if len(matches) >= limit:
                    break
            return matches

    def duplicates(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT sha256, COUNT(*) AS count
                FROM files
                WHERE status = ? AND sha256 != ''
                GROUP BY sha256
                HAVING COUNT(*) > 1
                ORDER BY count DESC
                """,
                ("active",),
            ).fetchall()
            groups = []
            for row in rows:
                files = self._conn.execute(
                    "SELECT * FROM files WHERE sha256 = ? AND status = ? ORDER BY path",
                    (row["sha256"], "active"),
                ).fetchall()
                groups.append({"sha256": row["sha256"], "count": row["count"], "files": [self._row_to_file(item) for item in files]})
            return groups

    def stats(self) -> dict:
        now = time.time()
        day_ago = now - 86400
        week_ago = now - (86400 * 7)
        with self._lock:
            active_count = self._scalar("SELECT COUNT(*) FROM files WHERE status = ?", ("active",))
            deleted_count = self._scalar("SELECT COUNT(*) FROM files WHERE status = ?", ("deleted",))
            total_size = self._scalar("SELECT COALESCE(SUM(size_bytes), 0) FROM files WHERE status = ?", ("active",))
            added_today = self._scalar("SELECT COUNT(*) FROM files WHERE status = ? AND indexed_ts >= ?", ("active", day_ago))
            added_week = self._scalar("SELECT COUNT(*) FROM files WHERE status = ? AND indexed_ts >= ?", ("active", week_ago))
            summarized = self._scalar("SELECT COUNT(*) FROM files WHERE status = ? AND summary != ''", ("active",))
            events = self._scalar("SELECT COUNT(*) FROM events", ())
            return {
                "database_path": str(self.database_path),
                "active_files": active_count,
                "deleted_files": deleted_count,
                "total_size_bytes": total_size,
                "events": events,
                "added_today": added_today,
                "added_this_week": added_week,
                "summary_coverage": (summarized / active_count) if active_count else 0.0,
                "fts_enabled": self._fts_enabled,
                "by_extension": self._breakdown("extension"),
                "by_mime_type": self._breakdown("mime_type"),
            }

    def export_json(self) -> dict:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM files ORDER BY path").fetchall()
            return {"files": [self._row_to_file(row) for row in rows], "stats": self.stats()}

    def export_csv(self) -> str:
        fields = ["id", "path", "filename", "extension", "mime_type", "size_bytes", "sha256", "status", "indexed_ts", "modified_ts"]
        output = io.StringIO()
        writer = csv.DictWriter(output, fieldnames=fields)
        writer.writeheader()
        with self._lock:
            rows = self._conn.execute("SELECT * FROM files ORDER BY path").fetchall()
            for row in rows:
                item = self._row_to_file(row)
                writer.writerow({field: item.get(field, "") for field in fields})
        return output.getvalue()

    def _sync_fts(self, file_id: str, path: str, filename: str, content: str):
        if not self._fts_enabled:
            return
        self._conn.execute("DELETE FROM file_fts WHERE file_id = ?", (file_id,))
        self._conn.execute(
            "INSERT INTO file_fts(file_id, path, filename, content) VALUES (?, ?, ?, ?)",
            (file_id, path, filename, content),
        )

    def _log_event_locked(self, event_type: str, file_id: str | None, old_path: str | None, new_path: str | None, payload: dict) -> dict:
        event = {
            "id": uuid.uuid4().hex,
            "timestamp": time.time(),
            "event_type": event_type,
            "file_id": file_id,
            "old_path": old_path,
            "new_path": new_path,
            "processed": False,
            "payload": payload,
        }
        self._conn.execute(
            """
            INSERT INTO events(id, timestamp, event_type, file_id, old_path, new_path, processed, payload_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event["id"],
                event["timestamp"],
                event["event_type"],
                event["file_id"],
                event["old_path"],
                event["new_path"],
                0,
                json.dumps(payload, ensure_ascii=False, sort_keys=True),
            ),
        )
        return event

    def _row_to_file(self, row: sqlite3.Row, extra: dict | None = None) -> dict:
        item = {
            "id": row["id"],
            "path": row["path"],
            "filename": row["filename"],
            "extension": row["extension"],
            "mime_type": row["mime_type"],
            "size_bytes": row["size_bytes"],
            "sha256": row["sha256"],
            "created_ts": row["created_ts"],
            "modified_ts": row["modified_ts"],
            "indexed_ts": row["indexed_ts"],
            "last_seen_ts": row["last_seen_ts"],
            "metadata": _json_dict(row["metadata_json"]),
            "summary": row["summary"],
            "tags": _json_list(row["tags_json"]),
            "status": row["status"],
        }
        if extra:
            item.update(extra)
        return item

    def _row_to_event(self, row: sqlite3.Row) -> dict:
        return {
            "id": row["id"],
            "timestamp": row["timestamp"],
            "event_type": row["event_type"],
            "file_id": row["file_id"],
            "old_path": row["old_path"],
            "new_path": row["new_path"],
            "processed": bool(row["processed"]),
            "payload": _json_dict(row["payload_json"]),
        }

    def _scalar(self, sql: str, params: tuple) -> int:
        row = self._conn.execute(sql, params).fetchone()
        return int(row[0] or 0)

    def _breakdown(self, column: str) -> dict:
        rows = self._conn.execute(
            f"SELECT {column}, COUNT(*) AS count FROM files WHERE status = ? GROUP BY {column} ORDER BY count DESC",
            ("active",),
        ).fetchall()
        return {str(row[column] or "unknown"): row["count"] for row in rows}


def _json_dict(value: str) -> dict:
    try:
        parsed = json.loads(value or "{}")
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _json_list(value: str) -> list:
    try:
        parsed = json.loads(value or "[]")
    except json.JSONDecodeError:
        return []
    return parsed if isinstance(parsed, list) else []


def _bounded_limit(value: int, low: int = 1, high: int = 500) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return low
    return max(low, min(high, number))


def _plain_terms(value: str) -> list[str]:
    terms: list[str] = []
    current: list[str] = []
    for char in str(value or ""):
        if char.isalnum() or char in ("_", "-", "."):
            current.append(char)
        elif current:
            terms.append("".join(current))
            current = []
    if current:
        terms.append("".join(current))
    return terms[:12]


def _snippet(content: str, terms: list[str]) -> str:
    if not content:
        return ""
    lower = content.lower()
    start = 0
    for term in terms:
        found = lower.find(term.lower())
        if found >= 0:
            start = max(0, found - 60)
            break
    return content[start : start + 180]
