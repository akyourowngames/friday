from __future__ import annotations

import ast
import hashlib
import json
import mimetypes
import tomllib
from pathlib import Path

from .configuration import WatcherConfig
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
        content = _extract_content(resolved, mime_type, self.config)
        tags = _tags_for(resolved, mime_type, stat.st_size, self.config)
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
        return self.index.upsert_file(record, content, tags, event_type)

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


def _python_metadata(path: Path) -> dict:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return {"language": "python", "parse_status": "unreadable"}
    result: dict[str, object] = {
        "language": "python",
        "line_count": len(text.splitlines()),
        "imports": [],
        "functions": [],
        "classes": [],
        "parse_status": "ok",
    }
    try:
        tree = ast.parse(text)
    except SyntaxError as exc:
        result["parse_status"] = "syntax_error"
        result["syntax_error"] = str(exc)
        return result
    imports: list[str] = []
    functions: list[str] = []
    classes: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                imports.append(node.module)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            functions.append(node.name)
        elif isinstance(node, ast.ClassDef):
            classes.append(node.name)
    result["imports"] = sorted(set(imports))
    result["functions"] = sorted(set(functions))
    result["classes"] = sorted(set(classes))
    return result


def _json_metadata(path: Path) -> dict:
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        return {"format": "json", "parse_status": "parse_error"}
    if isinstance(parsed, dict):
        return {"format": "json", "parse_status": "ok", "top_level_keys": sorted(str(key) for key in parsed.keys())}
    if isinstance(parsed, list):
        return {"format": "json", "parse_status": "ok", "top_level_type": "list", "item_count": len(parsed)}
    return {"format": "json", "parse_status": "ok", "top_level_type": type(parsed).__name__}


def _toml_metadata(path: Path) -> dict:
    try:
        parsed = tomllib.loads(path.read_text(encoding="utf-8"))
    except (tomllib.TOMLDecodeError, OSError, UnicodeDecodeError):
        return {"format": "toml", "parse_status": "parse_error"}
    return {"format": "toml", "parse_status": "ok", "top_level_keys": sorted(str(key) for key in parsed.keys())}


def _extract_content(path: Path, mime_type: str, config: WatcherConfig) -> str:
    suffix = path.suffix.lower()
    if suffix not in config.text_extensions and not mime_type.startswith("text/") and mime_type not in ("application/json", "application/xml"):
        return ""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
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
