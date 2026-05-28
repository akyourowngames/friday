from __future__ import annotations

import ast
import hashlib
import json
import mimetypes
import tomllib
from pathlib import Path

from .configuration import WatcherConfig
from .extractors import extract_document_or_media
from .index import FolderIndex


class IngestPipeline:
    def __init__(self, config: WatcherConfig, index: FolderIndex):
        self.config = config
        self.index = index

    def ingest_path(self, path: str | Path, event_type: str | None = None) -> dict | None:
        resolved = Path(path).expanduser().resolve()
        if self.config.should_ignore(resolved):
            return None
        if resolved.is_dir():
            return {"event": self.index.touch_directory(resolved, event_type or "DIR_CREATED"), "file": None}
        if not resolved.exists() or not resolved.is_file():
            return None

        stat = resolved.stat()
        digest = _sha256(resolved, self.config.hash_chunk_bytes)
        mime_type = _sniff_mime(resolved)
        metadata = _metadata(resolved, mime_type)
        extracted_metadata, extracted_content = extract_document_or_media(resolved, mime_type, self.config)
        metadata.update(extracted_metadata)
        content = extracted_content or _extract_content(resolved, mime_type, self.config)
        metadata.update(_content_understanding(resolved, content, self.config))
        tags = _tags_for(resolved, mime_type, stat.st_size, self.config)
        anomaly = _directory_anomaly(resolved, self.config)
        if anomaly:
            metadata["anomaly"] = anomaly
            tags.append({"tag": "anomaly", "source": "auto:directory-intent"})
        record = {
            "path": str(resolved),
            "filename": resolved.name,
            "extension": resolved.suffix.lower(),
            "mime_type": mime_type,
            "size_bytes": stat.st_size,
            "sha256": digest,
            "created_ts": stat.st_ctime,
            "modified_ts": stat.st_mtime,
            "metadata": metadata,
            "summary": "",
        }
        result = self.index.upsert_file(record, content, tags, event_type)
        file_item = result.get("file") if result else None
        if file_item and metadata.get("imports"):
            self.index.update_relationships(file_item["id"], _dependency_edges(resolved, metadata, self.config))
        if file_item and anomaly and self.config.anomaly_events_enabled:
            result["anomaly_event"] = self.index.log_anomaly(file_item["id"], str(resolved), anomaly)
        if file_item and mime_type.startswith("audio/"):
            self.index.write_playlist(self.config.playlist_path)
        return result

    def delete_path(self, path: str | Path) -> dict:
        return self.index.mark_deleted(path)

    def move_path(self, old_path: str | Path, new_path: str | Path) -> dict:
        return self.index.mark_moved(old_path, new_path)

    def scan_once(self) -> dict:
        scanned = 0
        skipped = 0
        for path in sorted(self.config.watch_path.rglob("*")):
            if self.config.should_ignore(path):
                skipped += 1
                continue
            if path.is_file():
                result = self.ingest_path(path, "FILE_CREATED")
                if result is not None:
                    scanned += 1
        return {"scanned": scanned, "skipped": skipped, "watch_path": str(self.config.watch_path)}

    def reconcile_deletions(self) -> dict:
        from datetime import datetime as _dt

        removed: list[str] = []
        with self.index._lock:
            rows = self.index._conn.execute(
                "SELECT id, path FROM files WHERE status = ?", ("active",)
            ).fetchall()
        for row in rows:
            stored_path = Path(row["path"])
            if self.config.should_ignore(stored_path):
                continue
            if not stored_path.exists():
                self.index.mark_deleted(stored_path)
                removed.append(str(stored_path))
        return {
            "removed_count": len(removed),
            "removed_paths": removed[:50],
            "reconciled_at": _dt.now().isoformat(timespec="seconds"),
        }

    def daily_maintenance(self) -> dict:
        from datetime import datetime as _dt

        scan_result = self.scan_once()
        reconcile_result = self.reconcile_deletions()
        playlist_result = self.index.write_playlist(self.config.playlist_path)
        stats_after = self.index.stats()
        return {
            "completed_at": _dt.now().isoformat(timespec="seconds"),
            "watch_path": str(self.config.watch_path),
            "scan": scan_result,
            "reconcile": reconcile_result,
            "playlist": playlist_result,
            "stats_after": stats_after,
        }


def _sha256(path: Path, chunk_size: int) -> str:
    digest = hashlib.sha256()
    size = max(1024, int(chunk_size or 65536))
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _sniff_mime(path: Path) -> str:
    try:
        head = path.read_bytes()[:512]
    except OSError:
        head = b""
    signatures = (
        (b"%PDF", "application/pdf"),
        (b"PK\x03\x04", "application/zip"),
        (b"\x89PNG\r\n\x1a\n", "image/png"),
        (b"\xff\xd8\xff", "image/jpeg"),
        (b"GIF87a", "image/gif"),
        (b"GIF89a", "image/gif"),
        (b"RIFF", "audio/wav"),
    )
    for prefix, mime_type in signatures:
        if head.startswith(prefix):
            return mime_type
    guessed, _ = mimetypes.guess_type(str(path))
    if guessed:
        return guessed
    if _looks_text(head):
        return "text/plain"
    return "application/octet-stream"


def _looks_text(data: bytes) -> bool:
    if not data:
        return True
    if b"\x00" in data:
        return False
    try:
        data.decode("utf-8")
        return True
    except UnicodeDecodeError:
        return False


def _metadata(path: Path, mime_type: str) -> dict:
    metadata: dict[str, object] = {
        "parent": str(path.parent),
        "suffix": path.suffix.lower(),
    }
    if path.suffix.lower() == ".py":
        metadata.update(_python_metadata(path))
    elif path.suffix.lower() == ".json":
        metadata.update(_json_metadata(path))
    elif path.suffix.lower() == ".toml":
        metadata.update(_toml_metadata(path))
    if mime_type.startswith("image/"):
        metadata["media_kind"] = "image"
    elif mime_type.startswith("audio/"):
        metadata["media_kind"] = "audio"
    elif mime_type.startswith("video/"):
        metadata["media_kind"] = "video"
    return metadata


def _dependency_edges(path: Path, metadata: dict, config: WatcherConfig) -> list[dict]:
    edges = []
    for imported in metadata.get("imports", []):
        target_path = _resolve_import(imported, config)
        edges.append(
            {
                "target_name": str(imported),
                "target_path": str(target_path) if target_path else "",
                "relation": "python_import",
                "source_path": str(path),
            }
        )
    return edges


def _resolve_import(imported: str, config: WatcherConfig) -> Path | None:
    parts = [part for part in str(imported or "").split(".") if part]
    if not parts:
        return None
    module_path = config.watch_path.joinpath(*parts)
    candidates = [module_path.with_suffix(".py"), module_path / "__init__.py"]
    for candidate in candidates:
        if candidate.exists():
            return candidate.resolve()
    return None


def _directory_anomaly(path: Path, config: WatcherConfig) -> dict | None:
    try:
        relative = path.relative_to(config.watch_path)
    except ValueError:
        return None
    parts = [part.lower() for part in relative.parts[:-1]]
    extension = path.suffix.lower()
    for intent in config.directory_intents:
        directory = intent.directory.lower().strip("/")
        if directory in parts and extension not in intent.extensions:
            return {
                "directory": intent.directory,
                "extension": extension,
                "expected_extensions": list(intent.extensions),
                "reason": "extension_outside_directory_intent",
            }
    return None


def _python_metadata(path: Path) -> dict:
    try:
        text = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError):
        return {"language": "python", "parse_status": "unreadable"}
    result: dict[str, object] = {
        "language": "python",
        "line_count": len(text.splitlines()),
        "imports": [],
        "functions": [],
        "classes": [],
        "function_details": [],
        "class_details": [],
        "parse_status": "ok",
    }
    try:
        tree = ast.parse(text)
    except SyntaxError as exc:
        result["parse_status"] = "syntax_error"
        result["syntax_error"] = str(exc)
        return result
    module_docstring = ast.get_docstring(tree) or ""
    if module_docstring:
        result["module_docstring"] = module_docstring[:700]
    imports: list[str] = []
    functions: list[str] = []
    classes: list[str] = []
    function_details: list[dict] = []
    class_details: list[dict] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                imports.append(node.module)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            functions.append(node.name)
            if len(function_details) < 80:
                function_details.append(_function_detail(node))
        elif isinstance(node, ast.ClassDef):
            classes.append(node.name)
            if len(class_details) < 60:
                class_details.append(_class_detail(node))
    result["imports"] = sorted(set(imports))
    result["functions"] = sorted(set(functions))
    result["classes"] = sorted(set(classes))
    result["import_count"] = len(result["imports"])
    result["function_count"] = len(result["functions"])
    result["class_count"] = len(result["classes"])
    result["function_details"] = function_details
    result["class_details"] = class_details
    return result


def _function_detail(node: ast.FunctionDef | ast.AsyncFunctionDef) -> dict:
    args = [item.arg for item in getattr(node.args, "posonlyargs", [])]
    args.extend(item.arg for item in node.args.args)
    args.extend(item.arg for item in node.args.kwonlyargs)
    if node.args.vararg:
        args.append("*" + node.args.vararg.arg)
    if node.args.kwarg:
        args.append("**" + node.args.kwarg.arg)
    return {
        "name": node.name,
        "line": getattr(node, "lineno", 0),
        "end_line": getattr(node, "end_lineno", getattr(node, "lineno", 0)),
        "async": isinstance(node, ast.AsyncFunctionDef),
        "args": args[:24],
        "docstring": bool(ast.get_docstring(node)),
    }


def _class_detail(node: ast.ClassDef) -> dict:
    methods = [
        item.name
        for item in node.body
        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]
    bases = []
    for base in node.bases:
        if isinstance(base, ast.Name):
            bases.append(base.id)
        elif isinstance(base, ast.Attribute):
            bases.append(base.attr)
        else:
            bases.append(type(base).__name__)
    return {
        "name": node.name,
        "line": getattr(node, "lineno", 0),
        "end_line": getattr(node, "end_lineno", getattr(node, "lineno", 0)),
        "bases": bases[:12],
        "methods": methods[:40],
        "docstring": bool(ast.get_docstring(node)),
    }


def _json_metadata(path: Path) -> dict:
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        return {"format": "json", "parse_status": "parse_error"}
    if isinstance(parsed, dict):
        return {
            "format": "json",
            "parse_status": "ok",
            "top_level_keys": sorted(str(key) for key in parsed.keys()),
            "nested_key_paths": _nested_key_paths(parsed),
        }
    if isinstance(parsed, list):
        return {
            "format": "json",
            "parse_status": "ok",
            "top_level_type": "list",
            "item_count": len(parsed),
            "nested_key_paths": _nested_key_paths(parsed),
        }
    return {"format": "json", "parse_status": "ok", "top_level_type": type(parsed).__name__}


def _toml_metadata(path: Path) -> dict:
    try:
        parsed = tomllib.loads(path.read_text(encoding="utf-8"))
    except (tomllib.TOMLDecodeError, OSError, UnicodeDecodeError):
        return {"format": "toml", "parse_status": "parse_error"}
    return {
        "format": "toml",
        "parse_status": "ok",
        "top_level_keys": sorted(str(key) for key in parsed.keys()),
        "nested_key_paths": _nested_key_paths(parsed),
    }


def _nested_key_paths(value: object, prefix: str = "", limit: int = 80) -> list[str]:
    paths: list[str] = []

    def walk(item: object, current: str) -> None:
        if len(paths) >= limit:
            return
        if isinstance(item, dict):
            for key in sorted(item.keys(), key=lambda part: str(part)):
                clean = str(key)
                next_path = clean if not current else current + "." + clean
                paths.append(next_path)
                walk(item[key], next_path)
                if len(paths) >= limit:
                    return
        elif isinstance(item, list):
            for index, child in enumerate(item[:10]):
                next_path = current + "[]" if current else "[]"
                if index == 0:
                    paths.append(next_path)
                walk(child, next_path)
                if len(paths) >= limit:
                    return

    walk(value, prefix)
    return paths[:limit]


def _content_understanding(path: Path, content: str, config: WatcherConfig) -> dict:
    text = str(content or "")
    lines = text.splitlines()
    non_empty = [line.strip() for line in lines if line.strip()]
    profile = {
        "content_available": bool(text),
        "chars_indexed": len(text),
        "line_count": len(lines),
        "non_empty_line_count": len(non_empty),
        "word_count": _word_count(text),
        "content_truncated": bool(text) and len(text) >= max(1, int(config.max_content_chars or 1)),
        "first_non_empty_lines": non_empty[:5],
        "headings": _markdown_headings(lines),
        "code_block_count": _fenced_code_block_count(lines),
        "source_suffix": path.suffix.lower(),
    }
    if not text:
        profile["reading_status"] = "no_text_content"
    elif profile["content_truncated"]:
        profile["reading_status"] = "truncated_to_config_limit"
    else:
        profile["reading_status"] = "indexed"
    return {"content_profile": profile}


def _word_count(text: str) -> int:
    count = 0
    in_word = False
    for char in text:
        if char.isalnum() or char in ("_", "-"):
            if not in_word:
                count += 1
                in_word = True
        else:
            in_word = False
    return count


def _markdown_headings(lines: list[str]) -> list[dict]:
    headings = []
    for index, line in enumerate(lines, start=1):
        stripped = line.strip()
        if not stripped.startswith("#"):
            continue
        level = 0
        for char in stripped:
            if char == "#":
                level += 1
                continue
            break
        if level <= 0 or level > 6:
            continue
        title = stripped[level:].strip()
        if not title:
            continue
        headings.append({"level": level, "text": title[:160], "line": index})
        if len(headings) >= 40:
            break
    return headings


def _fenced_code_block_count(lines: list[str]) -> int:
    count = 0
    for line in lines:
        if line.strip().startswith("```"):
            count += 1
    return count // 2


def _extract_content(path: Path, mime_type: str, config: WatcherConfig) -> str:
    suffix = path.suffix.lower()
    if suffix not in config.text_extensions and not mime_type.startswith("text/") and mime_type not in ("application/json", "application/xml"):
        return ""
    try:
        text = path.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return ""
    return text[: max(1, int(config.max_content_chars))]


def _tags_for(path: Path, mime_type: str, size_bytes: int, config: WatcherConfig) -> list[dict]:
    tags: list[dict] = []
    suffix = path.suffix.lower()
    parts = {part.lower() for part in path.parts}
    for rule in config.tag_rules:
        matched = False
        if rule.kind == "extension":
            matched = suffix == rule.value.lower()
        elif rule.kind == "mime-prefix":
            matched = mime_type.startswith(rule.value)
        elif rule.kind == "directory":
            matched = rule.value.lower() in parts
        elif rule.kind == "size-over":
            matched = size_bytes > _size_rule(rule.value, config.large_file_size)
        if matched:
            tags.append({"tag": rule.tag, "source": "auto:" + rule.kind})
    return tags


def _size_rule(value: str, default: int) -> int:
    text = str(value or "").strip().lower()
    units = (
        ("gb", 1024 * 1024 * 1024),
        ("mb", 1024 * 1024),
        ("kb", 1024),
        ("b", 1),
    )
    for suffix, multiplier in units:
        if text.endswith(suffix):
            number = text[: -len(suffix)].strip()
            try:
                return int(float(number) * multiplier)
            except ValueError:
                return default
    try:
        return int(text)
    except ValueError:
        return default
