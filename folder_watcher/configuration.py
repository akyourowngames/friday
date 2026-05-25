from __future__ import annotations

from dataclasses import dataclass, field
from fnmatch import fnmatch
from pathlib import Path


@dataclass
class TagRule:
    kind: str
    value: str
    tag: str


@dataclass
class WatcherConfig:
    repo_root: Path
    config_path: Path
    watch_path: Path
    database_path: Path
    api_host: str = "127.0.0.1"
    api_port: int = 7474
    debounce_ms: int = 300
    scan_on_start: bool = True
    auth_token: str = ""
    max_content_chars: int = 200000
    hash_chunk_bytes: int = 65536
    large_file_size: int = 100 * 1024 * 1024
    ai_summaries_enabled: bool = False
    ignore_globs: list[str] = field(default_factory=list)
    text_extensions: list[str] = field(default_factory=list)
    tag_rules: list[TagRule] = field(default_factory=list)

    def should_ignore(self, path: Path) -> bool:
        path = path.expanduser().resolve()
        try:
            relative = path.relative_to(self.watch_path)
        except ValueError:
            relative = Path(path.name)
        value = relative.as_posix()
        name = path.name
        for pattern in self.ignore_globs:
            clean = pattern.strip()
            if not clean:
                continue
            if clean.endswith("/**"):
                prefix = clean[:-3].strip("/")
                if value == prefix or value.startswith(prefix + "/"):
                    return True
            if fnmatch(value, clean) or fnmatch(name, clean):
                return True
        return False

    def public_dict(self) -> dict:
        return {
            "config_path": str(self.config_path),
            "watch_path": str(self.watch_path),
            "database_path": str(self.database_path),
            "api_host": self.api_host,
            "api_port": self.api_port,
            "debounce_ms": self.debounce_ms,
            "scan_on_start": self.scan_on_start,
            "auth_enabled": bool(self.auth_token),
            "max_content_chars": self.max_content_chars,
            "hash_chunk_bytes": self.hash_chunk_bytes,
            "large_file_size": self.large_file_size,
            "ai_summaries_enabled": self.ai_summaries_enabled,
            "ignore_globs": list(self.ignore_globs),
            "text_extensions": list(self.text_extensions),
            "tag_rules": [
                {"kind": rule.kind, "value": rule.value, "tag": rule.tag}
                for rule in self.tag_rules
            ],
        }

    def apply_runtime_patch(self, patch: dict) -> dict:
        changed: dict[str, object] = {}
        if "ignore_globs" in patch and isinstance(patch["ignore_globs"], list):
            self.ignore_globs = [str(item).strip() for item in patch["ignore_globs"] if str(item).strip()]
            changed["ignore_globs"] = list(self.ignore_globs)
        if "debounce_ms" in patch:
            value = _parse_int(patch["debounce_ms"], self.debounce_ms)
            if value >= 0:
                self.debounce_ms = value
                changed["debounce_ms"] = value
        if "ai_summaries_enabled" in patch:
            self.ai_summaries_enabled = _parse_bool(patch["ai_summaries_enabled"], self.ai_summaries_enabled)
            changed["ai_summaries_enabled"] = self.ai_summaries_enabled
        if "max_content_chars" in patch:
            value = _parse_int(patch["max_content_chars"], self.max_content_chars)
            if value > 0:
                self.max_content_chars = value
                changed["max_content_chars"] = value
        return changed


def load_config(repo_root: str | Path = ".", config_path: str | Path | None = None) -> WatcherConfig:
    root = Path(repo_root).expanduser().resolve()
    if config_path is None:
        path = root / "tools" / "FOLDER_WATCHER_CONFIG.md"
    else:
        path = Path(config_path).expanduser()
        if not path.is_absolute():
            path = root / path
        path = path.resolve()

    values: dict[str, str] = {}
    ignore_globs: list[str] = []
    text_extensions: list[str] = []
    tag_rules: list[TagRule] = []

    if path.exists():
        section = ""
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line:
                continue
            if line.startswith("## "):
                section = _section_name(line[3:])
                continue
            if not line.startswith("- "):
                continue
            item = line[2:].strip()
            if section == "paths_and_runtime":
                key, found, value = item.partition(":")
                if found:
                    values[key.strip()] = value.strip()
            elif section == "ignore_globs":
                ignore_globs.append(item)
            elif section == "text_extensions":
                if item:
                    text_extensions.append(_normalize_extension(item))
            elif section == "tag_rules":
                rule = _parse_tag_rule(item)
                if rule is not None:
                    tag_rules.append(rule)

    watch_path = _resolve_path(root, values.get("watch_path", "."))
    database_path = _resolve_path(root, values.get("database_path", "storage/folder_watcher.sqlite3"))

    return WatcherConfig(
        repo_root=root,
        config_path=path,
        watch_path=watch_path,
        database_path=database_path,
        api_host=values.get("api_host", "127.0.0.1") or "127.0.0.1",
        api_port=_parse_int(values.get("api_port"), 7474),
        debounce_ms=_parse_int(values.get("debounce_ms"), 300),
        scan_on_start=_parse_bool(values.get("scan_on_start"), True),
        auth_token=values.get("auth_token", ""),
        max_content_chars=_parse_int(values.get("max_content_chars"), 200000),
        hash_chunk_bytes=_parse_int(values.get("hash_chunk_bytes"), 65536),
        large_file_size=_parse_size(values.get("large_file_size"), 100 * 1024 * 1024),
        ai_summaries_enabled=_parse_bool(values.get("ai_summaries_enabled"), False),
        ignore_globs=ignore_globs,
        text_extensions=text_extensions,
        tag_rules=tag_rules,
    )


def _section_name(value: str) -> str:
    return value.strip().lower().replace(" ", "_")


def _resolve_path(root: Path, value: str) -> Path:
    path = Path(value or ".").expanduser()
    if not path.is_absolute():
        path = root / path
    return path.resolve()


def _normalize_extension(value: str) -> str:
    item = value.strip().lower()
    if not item:
        return item
    if item.startswith("."):
        return item
    return "." + item


def _parse_tag_rule(item: str) -> TagRule | None:
    left, found, tag = item.partition("->")
    if not found:
        return None
    kind, kind_found, value = left.strip().partition(":")
    if not kind_found:
        return None
    clean_kind = kind.strip().lower()
    clean_value = value.strip()
    clean_tag = tag.strip()
    if not clean_kind or not clean_value or not clean_tag:
        return None
    if clean_kind == "extension":
        clean_value = _normalize_extension(clean_value)
    return TagRule(kind=clean_kind, value=clean_value, tag=clean_tag)


def _parse_bool(value: object, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    text = str(value).strip().lower()
    if text in ("1", "true", "yes", "on"):
        return True
    if text in ("0", "false", "no", "off"):
        return False
    return default


def _parse_int(value: object, default: int) -> int:
    if value is None or isinstance(value, bool):
        return default
    try:
        return int(str(value).strip())
    except ValueError:
        return default


def _parse_size(value: object, default: int) -> int:
    if value is None:
        return default
    text = str(value).strip().lower()
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
    return _parse_int(text, default)
