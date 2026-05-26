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

                CREATE TABLE IF NOT EXISTS file_edges (
                    source_file_id TEXT NOT NULL,
                    target_file_id TEXT,
                    target_path TEXT NOT NULL,
                    target_name TEXT NOT NULL,
                    relation TEXT NOT NULL,
                    metadata_json TEXT NOT NULL,
                    UNIQUE(source_file_id, target_path, target_name, relation)
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

    def update_summary(self, file_id: str, summary: str, tags: list[str] | None = None) -> dict | None:
        clean_summary = str(summary or "").strip()
        clean_tags = [str(item).strip() for item in (tags or []) if str(item).strip()]
        with self._lock:
            row = self._conn.execute("SELECT tags_json FROM files WHERE id = ?", (file_id,)).fetchone()
            if row is None:
                return None
            merged_tags = set(_json_list(row["tags_json"]))
            for tag in clean_tags:
                merged_tags.add(tag)
                self._conn.execute(
                    "INSERT OR IGNORE INTO tags(file_id, tag, source) VALUES (?, ?, ?)",
                    (file_id, tag, "auto:ai"),
                )
            self._conn.execute(
                "UPDATE files SET summary = ?, tags_json = ? WHERE id = ?",
                (clean_summary, json.dumps(sorted(merged_tags), ensure_ascii=False), file_id),
            )
            self._conn.commit()
            return self.get_file(file_id)

    def update_relationships(self, file_id: str, edges: list[dict]):
        with self._lock:
            self._conn.execute("DELETE FROM file_edges WHERE source_file_id = ?", (file_id,))
            for edge in edges:
                target_path = str(edge.get("target_path", ""))
                target_file_id = None
                if target_path:
                    row = self._conn.execute("SELECT id FROM files WHERE path = ?", (target_path,)).fetchone()
                    target_file_id = row["id"] if row else None
                self._conn.execute(
                    """
                    INSERT OR IGNORE INTO file_edges(
                        source_file_id, target_file_id, target_path, target_name, relation, metadata_json
                    )
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        file_id,
                        target_file_id,
                        target_path,
                        str(edge.get("target_name", "")),
                        str(edge.get("relation", "related")),
                        json.dumps(edge, ensure_ascii=False, sort_keys=True),
                    ),
                )
            self._conn.commit()

    def dependencies(self, file_id: str) -> list[dict]:
        return self._edge_rows("source_file_id = ?", (file_id,))

    def dependents(self, file_id: str) -> list[dict]:
        return self._edge_rows("target_file_id = ?", (file_id,))

    def log_anomaly(self, file_id: str, path: str, anomaly: dict) -> dict:
        with self._lock:
            event = self._log_event_locked("ANOMALY", file_id, None, path, {"file_id": file_id, "path": path, "anomaly": anomaly})
            self._conn.commit()
            return event

    def get_file(self, file_id: str) -> dict | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM files WHERE id = ?", (file_id,)).fetchone()
            return self._row_to_file(row) if row else None

    def get_content(self, file_id: str) -> str | None:
        with self._lock:
            row = self._conn.execute("SELECT content FROM file_contents WHERE file_id = ?", (file_id,)).fetchone()
            return row["content"] if row else None

    def pending_summaries(self, limit: int = 10) -> list[dict]:
        bounded = _bounded_limit(limit, 1, 100)
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT files.*
                FROM files
                JOIN file_contents ON files.id = file_contents.file_id
                WHERE files.status = ? AND files.summary = '' AND file_contents.content != ''
                ORDER BY files.indexed_ts DESC
                LIMIT ?
                """,
                ("active", bounded),
            ).fetchall()
            return [self._row_to_file(row) for row in rows]

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

    def file_details(
        self,
        limit: int = 100,
        extension: str | None = None,
        directory: str | None = None,
        include_content: bool = False,
        max_content_chars: int = 2000,
    ) -> list[dict]:
        limit = _bounded_limit(limit, 1, 500)
        content_limit = _bounded_limit(max_content_chars, 1, 50000)
        clauses = ["status = ?"]
        params: list[object] = ["active"]
        if extension:
            ext = str(extension).strip().lower()
            if ext and not ext.startswith("."):
                ext = "." + ext
            clauses.append("extension = ?")
            params.append(ext)
        if directory:
            clean = str(directory).strip().replace("\\", "/").strip("/")
            if clean:
                clauses.append("path LIKE ?")
                params.append("%/" + clean + "/%")
        query = "SELECT * FROM files WHERE " + " AND ".join(clauses) + " ORDER BY extension, path LIMIT ?"
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(query, params).fetchall()
            result = []
            for row in rows:
                item = self._row_to_file(row)
                content = self.get_content(item["id"]) or ""
                dependencies = self.dependencies(item["id"])
                dependents = self.dependents(item["id"])
                event_summary = self._file_event_summary(item["id"])
                item["details"] = {
                    "content_available": bool(content),
                    "content_chars": len(content),
                    "dependency_count": len(dependencies),
                    "dependent_count": len(dependents),
                    "event_count": event_summary["count"],
                    "last_event": event_summary["recent"][0] if event_summary["recent"] else None,
                }
                if include_content:
                    item["content_excerpt"] = content[:content_limit]
                result.append(item)
            return result

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

    def snapshot(self, at: float | None = None) -> dict:
        cutoff = float(at if at is not None else time.time())
        files: dict[str, dict] = {}
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM events WHERE timestamp <= ? ORDER BY timestamp ASC",
                (cutoff,),
            ).fetchall()
            for row in rows:
                event = self._row_to_event(row)
                file_id = event.get("file_id")
                payload = event.get("payload", {})
                if event["event_type"] == "FILE_DELETED" and file_id:
                    files.pop(file_id, None)
                    continue
                snapshot = payload.get("file") if isinstance(payload, dict) else None
                if isinstance(snapshot, dict) and file_id:
                    item = dict(snapshot)
                    item["id"] = file_id
                    item["status"] = "active"
                    files[file_id] = item
        return {"at": cutoff, "files": list(files.values()), "count": len(files)}

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

    def public_schema(self, allowed_tables: list[str]) -> dict:
        schema: dict[str, list[dict]] = {}
        with self._lock:
            for table in allowed_tables:
                if not _safe_identifier(table):
                    continue
                rows = self._conn.execute(f"PRAGMA table_info({table})").fetchall()
                schema[table] = [
                    {
                        "name": row["name"],
                        "type": row["type"],
                        "notnull": bool(row["notnull"]),
                        "primary_key": bool(row["pk"]),
                    }
                    for row in rows
                ]
        return schema

    def readonly_query(self, sql: str, allowed_tables: list[str], allowed_functions: list[str], limit: int = 25) -> dict:
        clean_sql = str(sql or "").strip()
        if not clean_sql:
            return {"status": "blocked", "error": "empty_sql", "rows": [], "columns": []}
        allowed_table_set = {item for item in allowed_tables if _safe_identifier(item)}
        allowed_function_set = {item.lower() for item in allowed_functions if _safe_identifier(item)}
        denied: list[dict] = []
        step_count = 0
        max_steps = 20000

        def authorize(action, arg1, arg2, db_name, source):
            if action == sqlite3.SQLITE_SELECT:
                return sqlite3.SQLITE_OK
            if action == sqlite3.SQLITE_READ:
                table = str(arg1 or "")
                if table in allowed_table_set:
                    return sqlite3.SQLITE_OK
                denied.append({"action": "read", "target": table})
                return sqlite3.SQLITE_DENY
            if action == sqlite3.SQLITE_FUNCTION:
                function_name = str(arg2 or arg1 or "").lower()
                if function_name in allowed_function_set:
                    return sqlite3.SQLITE_OK
                denied.append({"action": "function", "target": function_name})
                return sqlite3.SQLITE_DENY
            denied.append({"action": str(action), "target": str(arg1 or arg2 or "")})
            return sqlite3.SQLITE_DENY

        def progress():
            nonlocal step_count
            step_count += 1
            return 1 if step_count > max_steps else 0

        with self._lock:
            previous_authorizer = None
            try:
                previous_authorizer = self._conn.set_authorizer(authorize)
                self._conn.set_progress_handler(progress, 1000)
                cursor = self._conn.execute(clean_sql)
                columns = [item[0] for item in (cursor.description or [])]
                rows = cursor.fetchmany(_bounded_limit(limit, 1, 500))
                return {
                    "status": "success",
                    "columns": columns,
                    "rows": [dict(row) for row in rows],
                    "denied": denied,
                }
            except sqlite3.DatabaseError as exc:
                return {
                    "status": "blocked",
                    "error": str(exc),
                    "columns": [],
                    "rows": [],
                    "denied": denied,
                }
            finally:
                self._conn.set_progress_handler(None, 0)
                self._conn.set_authorizer(previous_authorizer)

    def anomalies(self, limit: int = 100) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM events WHERE event_type = ? ORDER BY timestamp DESC LIMIT ?",
                ("ANOMALY", _bounded_limit(limit, 1, 500)),
            ).fetchall()
            return [self._row_to_event(row) for row in rows]

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

    def duplicate_symlink_suggestions(self) -> list[dict]:
        suggestions = []
        for group in self.duplicates():
            files = group.get("files", [])
            if len(files) < 2:
                continue
            canonical = sorted(files, key=lambda item: (len(item["path"]), item["path"]))[0]
            duplicates = [item for item in files if item["id"] != canonical["id"]]
            suggestions.append(
                {
                    "sha256": group["sha256"],
                    "canonical": canonical,
                    "duplicates": duplicates,
                    "suggested_action": "replace duplicates with links to canonical path after user approval",
                }
            )
        return suggestions

    def hot_files(self, threshold: int = 5, window_seconds: int = 86400, limit: int = 20) -> list[dict]:
        since = time.time() - max(1, int(window_seconds))
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT file_id, COUNT(*) AS event_count
                FROM events
                WHERE file_id IS NOT NULL
                  AND timestamp >= ?
                  AND event_type IN ('FILE_CREATED', 'FILE_MODIFIED', 'FILE_UNCHANGED')
                GROUP BY file_id
                HAVING COUNT(*) >= ?
                ORDER BY event_count DESC
                LIMIT ?
                """,
                (since, max(1, int(threshold)), _bounded_limit(limit, 1, 200)),
            ).fetchall()
            result = []
            for row in rows:
                item = self.get_file(row["file_id"])
                if not item:
                    continue
                tags = set(item.get("tags", []))
                tags.add("hot")
                item["tags"] = sorted(tags)
                item["event_count"] = row["event_count"]
                result.append(item)
            return result

    def audio_files(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM files WHERE status = ? AND mime_type LIKE ? ORDER BY indexed_ts DESC",
                ("active", "audio/%"),
            ).fetchall()
            return [self._row_to_file(row) for row in rows]

    def write_playlist(self, playlist_path: str | Path | None) -> dict:
        audio_files = self.audio_files()
        if playlist_path is None:
            return {"written": False, "count": len(audio_files), "path": ""}
        path = Path(playlist_path).expanduser().resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        lines = ["#EXTM3U"]
        for item in audio_files:
            lines.append("#EXTINF:-1," + item["filename"])
            lines.append(item["path"])
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return {"written": True, "count": len(audio_files), "path": str(path)}

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
                "by_extension_details": self._breakdown_details("extension"),
                "by_mime_type_details": self._breakdown_details("mime_type"),
                "largest_files": self._largest_files(10),
                "hot_files": self.hot_files(),
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

    def _edge_rows(self, where: str, params: tuple) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT file_edges.*, source.path AS source_path, target.path AS resolved_target_path
                FROM file_edges
                LEFT JOIN files AS source ON source.id = file_edges.source_file_id
                LEFT JOIN files AS target ON target.id = file_edges.target_file_id
                WHERE """ + where + """
                ORDER BY target_name
                """,
                params,
            ).fetchall()
            return [
                {
                    "source_file_id": row["source_file_id"],
                    "source_path": row["source_path"],
                    "target_file_id": row["target_file_id"],
                    "target_path": row["resolved_target_path"] or row["target_path"],
                    "target_name": row["target_name"],
                    "relation": row["relation"],
                    "metadata": _json_dict(row["metadata_json"]),
                }
                for row in rows
            ]

    def _scalar(self, sql: str, params: tuple) -> int:
        row = self._conn.execute(sql, params).fetchone()
        return int(row[0] or 0)

    def _breakdown(self, column: str) -> dict:
        rows = self._conn.execute(
            f"SELECT {column}, COUNT(*) AS count FROM files WHERE status = ? GROUP BY {column} ORDER BY count DESC",
            ("active",),
        ).fetchall()
        return {str(row[column] or "unknown"): row["count"] for row in rows}

    def _breakdown_details(self, column: str) -> dict:
        rows = self._conn.execute(
            f"""
            SELECT {column},
                   COUNT(*) AS count,
                   COALESCE(SUM(size_bytes), 0) AS size_bytes,
                   COALESCE(AVG(size_bytes), 0) AS avg_size_bytes,
                   COALESCE(MIN(size_bytes), 0) AS min_size_bytes,
                   COALESCE(MAX(size_bytes), 0) AS max_size_bytes,
                   COALESCE(MAX(modified_ts), 0) AS newest_modified_ts,
                   COALESCE(MAX(indexed_ts), 0) AS newest_indexed_ts
            FROM files
            WHERE status = ?
            GROUP BY {column}
            ORDER BY size_bytes DESC, count DESC
            """,
            ("active",),
        ).fetchall()
        return {
            str(row[column] or "unknown"): {
                "count": int(row["count"] or 0),
                "size_bytes": int(row["size_bytes"] or 0),
                "avg_size_bytes": int(row["avg_size_bytes"] or 0),
                "min_size_bytes": int(row["min_size_bytes"] or 0),
                "max_size_bytes": int(row["max_size_bytes"] or 0),
                "newest_modified_ts": float(row["newest_modified_ts"] or 0),
                "newest_indexed_ts": float(row["newest_indexed_ts"] or 0),
            }
            for row in rows
        }

    def _largest_files(self, limit: int) -> list[dict]:
        rows = self._conn.execute(
            """
            SELECT *
            FROM files
            WHERE status = ?
            ORDER BY size_bytes DESC, path
            LIMIT ?
            """,
            ("active", _bounded_limit(limit, 1, 100)),
        ).fetchall()
        return [self._row_to_file(row) for row in rows]

    def _file_event_summary(self, file_id: str) -> dict:
        count = self._scalar("SELECT COUNT(*) FROM events WHERE file_id = ?", (file_id,))
        rows = self._conn.execute(
            """
            SELECT *
            FROM events
            WHERE file_id = ?
            ORDER BY timestamp DESC
            LIMIT 5
            """,
            (file_id,),
        ).fetchall()
        return {"count": count, "recent": [self._row_to_event(row) for row in rows]}


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


def _safe_identifier(value: str) -> bool:
    text = str(value or "").strip()
    if not text:
        return False
    for char in text:
        if not (char.isalnum() or char == "_"):
            return False
    return True


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
