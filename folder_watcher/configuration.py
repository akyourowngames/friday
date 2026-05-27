from __future__ import annotations

import os
from dataclasses import dataclass, field
from fnmatch import fnmatch
from pathlib import Path


@dataclass
class TagRule:
    kind: str
    value: str
    tag: str


@dataclass
class DirectoryIntent:
    directory: str
    extensions: list[str]


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
    llm_queries_enabled: bool = True
    llm_policy_path: Path | None = None
    hot_file_event_threshold: int = 5
    hot_file_window_seconds: int = 86400
    anomaly_events_enabled: bool = True
    ocr_enabled: bool = True
    transcription_enabled: bool = True
    subscriber_rate_limit_per_sec: float = 20.0
    webhook_rate_limit_per_sec: float = 5.0
    playlist_path: Path | None = None
    ignore_globs: list[str] = field(default_factory=list)
    text_extensions: list[str] = field(default_factory=list)
    tag_rules: list[TagRule] = field(default_factory=list)
    directory_intents: list[DirectoryIntent] = field(default_factory=list)
    env_overrides: dict[str, str] = field(default_factory=dict)

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
            "llm_queries_enabled": self.llm_queries_enabled,
            "llm_policy_path": str(self.llm_policy_path) if self.llm_policy_path else "",
            "hot_file_event_threshold": self.hot_file_event_threshold,
            "hot_file_window_seconds": self.hot_file_window_seconds,
            "anomaly_events_enabled": self.anomaly_events_enabled,
            "ocr_enabled": self.ocr_enabled,
            "transcription_enabled": self.transcription_enabled,
            "subscriber_rate_limit_per_sec": self.subscriber_rate_limit_per_sec,
            "webhook_rate_limit_per_sec": self.webhook_rate_limit_per_sec,
            "playlist_path": str(self.playlist_path) if self.playlist_path else "",
            "ignore_globs": list(self.ignore_globs),
            "text_extensions": list(self.text_extensions),
            "tag_rules": [
                {"kind": rule.kind, "value": rule.value, "tag": rule.tag}
                for rule in self.tag_rules
            ],
            "directory_intents": [
                {"directory": item.directory, "extensions": list(item.extensions)}
                for item in self.directory_intents
            ],
            "env_overrides": dict(self.env_overrides),
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
        if "llm_queries_enabled" in patch:
            self.llm_queries_enabled = _parse_bool(patch["llm_queries_enabled"], self.llm_queries_enabled)
            changed["llm_queries_enabled"] = self.llm_queries_enabled
        if "max_content_chars" in patch:
            value = _parse_int(patch["max_content_chars"], self.max_content_chars)
            if value > 0:
                self.max_content_chars = value
                changed["max_content_chars"] = value
        if "hot_file_event_threshold" in patch:
            value = _parse_int(patch["hot_file_event_threshold"], self.hot_file_event_threshold)
            if value > 0:
                self.hot_file_event_threshold = value
                changed["hot_file_event_threshold"] = value
        return changed

    def refresh_from(self, other: "WatcherConfig"):
        self.debounce_ms = other.debounce_ms
        self.scan_on_start = other.scan_on_start
        self.auth_token = other.auth_token
        self.max_content_chars = other.max_content_chars
        self.hash_chunk_bytes = other.hash_chunk_bytes
        self.large_file_size = other.large_file_size
        self.ai_summaries_enabled = other.ai_summaries_enabled
        self.llm_queries_enabled = other.llm_queries_enabled
        self.llm_policy_path = other.llm_policy_path
        self.hot_file_event_threshold = other.hot_file_event_threshold
        self.hot_file_window_seconds = other.hot_file_window_seconds
        self.anomaly_events_enabled = other.anomaly_events_enabled
        self.ocr_enabled = other.ocr_enabled
        self.transcription_enabled = other.transcription_enabled
        self.subscriber_rate_limit_per_sec = other.subscriber_rate_limit_per_sec
        self.webhook_rate_limit_per_sec = other.webhook_rate_limit_per_sec
        self.playlist_path = other.playlist_path
        self.ignore_globs = list(other.ignore_globs)
        self.text_extensions = list(other.text_extensions)
        self.tag_rules = list(other.tag_rules)
        self.directory_intents = list(other.directory_intents)
        self.env_overrides = dict(other.env_overrides)


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
    directory_intents: list[DirectoryIntent] = []

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
            elif section == "directory_intent_rules":
                intent = _parse_directory_intent(item)
                if intent is not None:
                    directory_intents.append(intent)

    env_overrides: dict[str, str] = {}
    watch_path_value = _config_value(values, "watch_path", ".", "KING_FOLDER_WATCHER_WATCH_PATH", env_overrides)
    database_path_value = _config_value(values, "database_path", "storage/folder_watcher.sqlite3", "KING_FOLDER_WATCHER_DATABASE_PATH", env_overrides)
    api_host_value = _config_value(values, "api_host", "127.0.0.1", "KING_FOLDER_WATCHER_API_HOST", env_overrides)
    api_port_value = _config_value(values, "api_port", "7474", "KING_FOLDER_WATCHER_API_PORT", env_overrides)
    max_content_value = _config_value(values, "max_content_chars", "200000", "KING_FOLDER_WATCHER_MAX_CONTENT_CHARS", env_overrides)

    watch_path = _resolve_path(root, watch_path_value)
    database_path = _resolve_path(root, database_path_value)
    llm_policy_path = _resolve_path(root, values.get("llm_policy_file", "tools/FOLDER_WATCHER_LLM_POLICY.md"))
    playlist_path = _resolve_path(root, values.get("playlist_path", "storage/folder_watcher_new_arrivals.m3u"))

    return WatcherConfig(
        repo_root=root,
        config_path=path,
        watch_path=watch_path,
        database_path=database_path,
        api_host=api_host_value or "127.0.0.1",
        api_port=_parse_int(api_port_value, 7474),
        debounce_ms=_parse_int(values.get("debounce_ms"), 300),
        scan_on_start=_parse_bool(values.get("scan_on_start"), True),
        auth_token=values.get("auth_token", ""),
        max_content_chars=_parse_int(max_content_value, 200000),
        hash_chunk_bytes=_parse_int(values.get("hash_chunk_bytes"), 65536),
        large_file_size=_parse_size(values.get("large_file_size"), 100 * 1024 * 1024),
        ai_summaries_enabled=_parse_bool(values.get("ai_summaries_enabled"), False),
        llm_queries_enabled=_parse_bool(values.get("llm_queries_enabled"), True),
        llm_policy_path=llm_policy_path,
        hot_file_event_threshold=_parse_int(values.get("hot_file_event_threshold"), 5),
        hot_file_window_seconds=_parse_int(values.get("hot_file_window_seconds"), 86400),
        anomaly_events_enabled=_parse_bool(values.get("anomaly_events_enabled"), True),
        ocr_enabled=_parse_bool(values.get("ocr_enabled"), True),
        transcription_enabled=_parse_bool(values.get("transcription_enabled"), True),
        subscriber_rate_limit_per_sec=_parse_float(values.get("subscriber_rate_limit_per_sec"), 20.0),
        webhook_rate_limit_per_sec=_parse_float(values.get("webhook_rate_limit_per_sec"), 5.0),
        playlist_path=playlist_path,
        ignore_globs=ignore_globs,
        text_extensions=text_extensions,
        tag_rules=tag_rules,
        directory_intents=directory_intents,
        env_overrides=env_overrides,
    )


def _section_name(value: str) -> str:
    return value.strip().lower().replace(" ", "_")


def _resolve_path(root: Path, value: str) -> Path:
    path = Path(value or ".").expanduser()
    if not path.is_absolute():
        path = root / path
    return path.resolve()


def _config_value(values: dict[str, str], key: str, default: str, env_name: str, env_overrides: dict[str, str]) -> str:
    env_value = os.getenv(env_name)
    if env_value is not None and str(env_value).strip():
        env_overrides[key] = env_name
        return str(env_value).strip()
    return values.get(key, default)


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


def _parse_directory_intent(item: str) -> DirectoryIntent | None:
    directory, found, extensions = item.partition(":")
    if not found:
        return None
    clean_directory = directory.strip().strip("/")
    clean_extensions = [
        _normalize_extension(value)
        for value in extensions.split(",")
        if value.strip()
    ]
    if not clean_directory or not clean_extensions:
        return None
    return DirectoryIntent(clean_directory, clean_extensions)


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


def _parse_float(value: object, default: float) -> float:
    if value is None or isinstance(value, bool):
        return default
    try:
        return float(str(value).strip())
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
