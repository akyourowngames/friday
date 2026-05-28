from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class AllowedZone:
    name: str
    path: Path
    enabled: bool = True


@dataclass
class TelegramWatcherConfig:
    repo_root: Path
    config_path: Path
    token_env: str = "TELEGRAM_BOT_TOKEN"
    authorized_user_ids_env: str = "KING_TELEGRAM_AUTHORIZED_USER_IDS"
    authorized_chat_ids_env: str = "KING_TELEGRAM_AUTHORIZED_CHAT_IDS"
    unlock_pin_env: str = "KING_TELEGRAM_UNLOCK_PIN"
    state_path: Path | None = None
    session_log_path: Path | None = None
    polling_timeout_seconds: int = 30
    request_timeout_ms: int = 15000
    api_host: str = "127.0.0.1"
    api_port: int = 7480
    service_base_url: str = "http://127.0.0.1:7480"
    main_cli_autostart: bool = True
    cli_bridge_enabled: bool = True
    api_startup_wait_ms: int = 3000
    local_cli_chat_id: int = -9001
    local_cli_user_id: int = -9001
    folder_watcher_base_url: str = "client_active_target"
    folder_watcher_auth_env: str = "KING_FOLDER_WATCHER_AUTH_TOKEN"
    folder_watcher_timeout_ms: int = 12000
    max_file_size_bytes: int = 50 * 1024 * 1024
    max_results: int = 10
    max_scan_files: int = 5000
    default_new_window_seconds: int = 86400
    rate_limit_queries_per_minute: int = 30
    rate_limit_sends_per_minute: int = 10
    semantic_min_score: float = 0.35
    semantic_min_margin: float = 0.05
    fallback_action: str = "ask"
    push_check_interval_seconds: int = 30
    push_event_limit: int = 20
    push_auto_send: bool = False
    startup_notice_enabled: bool = True
    startup_notice_text: str = "KING Telegram watcher is online. You can talk naturally."
    zones: list[AllowedZone] = field(default_factory=list)
    blocked_suffixes: set[str] = field(default_factory=set)
    blocked_name_fragments: set[str] = field(default_factory=set)
    blocked_path_parts: set[str] = field(default_factory=set)
    command_aliases: dict[str, str] = field(default_factory=dict)
    action_semantics: dict[str, str] = field(default_factory=dict)
    cli_forward_actions: set[str] = field(default_factory=set)
    env_overrides: dict[str, str] = field(default_factory=dict)

    @property
    def token(self) -> str:
        return str(os.getenv(self.token_env, "") or "").strip()

    @property
    def unlock_pin(self) -> str:
        return str(os.getenv(self.unlock_pin_env, "") or "").strip()

    @property
    def authorized_user_ids(self) -> set[int]:
        return _parse_id_list(os.getenv(self.authorized_user_ids_env, "") or "")

    @property
    def authorized_chat_ids(self) -> set[int]:
        return _parse_id_list(os.getenv(self.authorized_chat_ids_env, "") or "")

    def enabled_zones(self) -> list[AllowedZone]:
        return [zone for zone in self.zones if zone.enabled]


def load_config(repo_root: str | Path = ".", config_path: str | Path | None = None) -> TelegramWatcherConfig:
    root = Path(repo_root).expanduser().resolve()
    requested = Path(config_path or os.getenv("KING_TELEGRAM_CONFIG_FILE", "tools/TELEGRAM_WATCHER_CONFIG.md"))
    path = requested if requested.is_absolute() else root / requested
    path = path.resolve()

    values: dict[str, str] = {}
    zones: list[AllowedZone] = []
    blocked_suffixes: set[str] = set()
    blocked_name_fragments: set[str] = set()
    blocked_path_parts: set[str] = set()
    command_aliases: dict[str, str] = {}
    action_semantics: dict[str, str] = {}
    cli_forward_actions: set[str] = set()

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
            if section == "runtime":
                key, found, value = item.partition(":")
                if found:
                    values[key.strip()] = value.strip()
            elif section == "allowed_zones":
                zone = _parse_zone(root, item)
                if zone is not None:
                    zones.append(zone)
            elif section == "blocked_suffixes":
                clean = _normalize_suffix(item)
                if clean:
                    blocked_suffixes.add(clean)
            elif section == "blocked_name_fragments":
                clean = item.strip().casefold()
                if clean:
                    blocked_name_fragments.add(clean)
            elif section == "blocked_path_parts":
                clean = item.strip().casefold()
                if clean:
                    blocked_path_parts.add(clean)
            elif section == "command_aliases":
                alias, found, action = item.partition(":")
                if found and alias.strip() and action.strip():
                    command_aliases[alias.strip().casefold()] = action.strip().casefold()
            elif section == "action_semantics":
                action, found, text = item.partition(":")
                if found and action.strip() and text.strip():
                    action_semantics[action.strip().casefold()] = text.strip()
            elif section == "cli_forward_actions":
                clean = item.strip().casefold()
                if clean:
                    cli_forward_actions.add(clean)

    env_overrides: dict[str, str] = {}
    state_path = _resolve_path(root, _config_value(values, "state_path", "storage/telegram_watcher_state.json", "KING_TELEGRAM_STATE_PATH", env_overrides))
    session_log_path = _resolve_path(root, _config_value(values, "session_log_path", "storage/telegram_watcher_session.jsonl", "KING_TELEGRAM_SESSION_LOG", env_overrides))
    api_host = _config_value(values, "api_host", "127.0.0.1", "KING_TELEGRAM_API_HOST", env_overrides)
    api_port = _parse_int(_config_value(values, "api_port", "7480", "KING_TELEGRAM_API_PORT", env_overrides), 7480)
    default_base_url = "http://" + api_host + ":" + str(api_port)
    service_base_url = _config_value(values, "service_base_url", default_base_url, "KING_TELEGRAM_SERVICE_BASE_URL", env_overrides).rstrip("/")
    base_url = _config_value(values, "folder_watcher_base_url", "client_active_target", "KING_TELEGRAM_FOLDER_WATCHER_BASE_URL", env_overrides)
    base_url = _resolve_folder_watcher_base_url(root, base_url).rstrip("/")

    return TelegramWatcherConfig(
        repo_root=root,
        config_path=path,
        token_env=values.get("token_env", "TELEGRAM_BOT_TOKEN"),
        authorized_user_ids_env=values.get("authorized_user_ids_env", "KING_TELEGRAM_AUTHORIZED_USER_IDS"),
        authorized_chat_ids_env=values.get("authorized_chat_ids_env", "KING_TELEGRAM_AUTHORIZED_CHAT_IDS"),
        unlock_pin_env=values.get("unlock_pin_env", "KING_TELEGRAM_UNLOCK_PIN"),
        state_path=state_path,
        session_log_path=session_log_path,
        polling_timeout_seconds=_parse_int(values.get("polling_timeout_seconds"), 30),
        request_timeout_ms=_parse_int(values.get("request_timeout_ms"), 15000),
        api_host=api_host,
        api_port=api_port,
        service_base_url=service_base_url,
        main_cli_autostart=_parse_bool(
            _config_value(values, "main_cli_autostart", "true", "KING_TELEGRAM_MAIN_CLI_AUTOSTART", env_overrides),
            True,
        ),
        cli_bridge_enabled=_parse_bool(
            _config_value(values, "cli_bridge_enabled", "true", "KING_TELEGRAM_CLI_BRIDGE_ENABLED", env_overrides),
            True,
        ),
        api_startup_wait_ms=_parse_int(
            _config_value(values, "api_startup_wait_ms", "3000", "KING_TELEGRAM_API_STARTUP_WAIT_MS", env_overrides),
            3000,
        ),
        local_cli_chat_id=_parse_int(
            _config_value(values, "local_cli_chat_id", "-9001", "KING_TELEGRAM_LOCAL_CLI_CHAT_ID", env_overrides),
            -9001,
        ),
        local_cli_user_id=_parse_int(
            _config_value(values, "local_cli_user_id", "-9001", "KING_TELEGRAM_LOCAL_CLI_USER_ID", env_overrides),
            -9001,
        ),
        folder_watcher_base_url=base_url,
        folder_watcher_auth_env=values.get("folder_watcher_auth_env", "KING_FOLDER_WATCHER_AUTH_TOKEN"),
        folder_watcher_timeout_ms=_parse_int(values.get("folder_watcher_timeout_ms"), 12000),
        max_file_size_bytes=_parse_size(values.get("max_file_size"), 50 * 1024 * 1024),
        max_results=_parse_int(values.get("max_results"), 10),
        max_scan_files=_parse_int(values.get("max_scan_files"), 5000),
        default_new_window_seconds=_parse_duration(values.get("default_new_window"), 86400),
        rate_limit_queries_per_minute=_parse_int(values.get("rate_limit_queries_per_minute"), 30),
        rate_limit_sends_per_minute=_parse_int(values.get("rate_limit_sends_per_minute"), 10),
        semantic_min_score=_parse_float(values.get("semantic_min_score"), 0.35),
        semantic_min_margin=_parse_float(values.get("semantic_min_margin"), 0.05),
        fallback_action=values.get("fallback_action", "ask").strip().casefold() or "ask",
        push_check_interval_seconds=_parse_int(values.get("push_check_interval_seconds"), 30),
        push_event_limit=_parse_int(values.get("push_event_limit"), 20),
        push_auto_send=_parse_bool(values.get("push_auto_send"), False),
        startup_notice_enabled=_parse_bool(values.get("startup_notice_enabled"), True),
        startup_notice_text=values.get("startup_notice_text", "KING Telegram watcher is online. You can talk naturally."),
        zones=zones,
        blocked_suffixes=blocked_suffixes,
        blocked_name_fragments=blocked_name_fragments,
        blocked_path_parts=blocked_path_parts,
        command_aliases=command_aliases,
        action_semantics=action_semantics,
        cli_forward_actions=cli_forward_actions,
        env_overrides=env_overrides,
    )


def _section_name(value: str) -> str:
    return value.strip().casefold().replace(" ", "_")


def _expand_path_value(value: str) -> str:
    text = os.path.expandvars(str(value or "").strip())
    return os.path.expanduser(text)


def _resolve_path(root: Path, value: str) -> Path:
    path = Path(_expand_path_value(value or "."))
    if not path.is_absolute():
        path = root / path
    return path.resolve()


def _config_value(values: dict[str, str], key: str, default: str, env_name: str, env_overrides: dict[str, str]) -> str:
    env_value = os.getenv(env_name)
    if env_value is not None and str(env_value).strip():
        env_overrides[key] = env_name
        return str(env_value).strip()
    return values.get(key, default)


def _resolve_folder_watcher_base_url(root: Path, value: str) -> str:
    text = str(value or "").strip()
    if text.casefold() not in ("client_active_target", "folder_client_active_target"):
        return text or "http://127.0.0.1:7474"
    client_file = Path(os.getenv("KING_FOLDER_WATCHER_CLIENT_FILE", "tools/FOLDER_WATCHER_CLIENT.md"))
    if not client_file.is_absolute():
        client_file = root / client_file
    active_target = ""
    targets: dict[str, str] = {}
    if not client_file.exists():
        return "http://127.0.0.1:7474"
    section = ""
    for raw_line in client_file.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("## "):
            section = _section_name(line[3:])
            continue
        if not line.startswith("- "):
            continue
        item = line[2:].strip()
        if section == "runtime":
            key, found, configured = item.partition(":")
            if found and key.strip().casefold() == "active_target":
                active_target = configured.strip().casefold()
        elif section == "targets":
            name, found, rest = item.partition(":")
            if not found:
                continue
            pieces = [piece.strip() for piece in rest.split("|") if piece.strip()]
            if pieces:
                targets[name.strip().casefold()] = pieces[0]
    if active_target and active_target in targets:
        return targets[active_target]
    return "http://127.0.0.1:7474"


def _parse_zone(root: Path, item: str) -> AllowedZone | None:
    name, found, rest = item.partition(":")
    if not found:
        return None
    zone_name = name.strip().casefold()
    if not zone_name:
        return None
    pieces = [piece.strip() for piece in rest.split("|") if piece.strip()]
    if not pieces:
        return None
    zone_path = _resolve_path(root, pieces[0])
    enabled = True
    for piece in pieces[1:]:
        key, separator, value = piece.partition(":")
        if separator and key.strip().casefold() == "enabled":
            enabled = _parse_bool(value, True)
    return AllowedZone(zone_name, zone_path, enabled)


def _normalize_suffix(value: str) -> str:
    text = value.strip().casefold()
    if not text:
        return ""
    if text.startswith("."):
        return text
    return "." + text


def _parse_bool(value: object, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    text = str(value).strip().casefold()
    if text in ("1", "true", "yes", "on", "enabled"):
        return True
    if text in ("0", "false", "no", "off", "disabled"):
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
    text = str(value).strip().casefold()
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


def _parse_duration(value: object, default: int) -> int:
    if value is None:
        return default
    text = str(value).strip().casefold()
    units = (
        ("day", 86400),
        ("days", 86400),
        ("d", 86400),
        ("hour", 3600),
        ("hours", 3600),
        ("h", 3600),
        ("minute", 60),
        ("minutes", 60),
        ("min", 60),
        ("m", 60),
        ("second", 1),
        ("seconds", 1),
        ("s", 1),
    )
    for suffix, multiplier in units:
        if text.endswith(suffix):
            number = text[: -len(suffix)].strip()
            try:
                return int(float(number) * multiplier)
            except ValueError:
                return default
    return _parse_int(text, default)


def _parse_id_list(value: object) -> set[int]:
    raw = str(value or "")
    ids: set[int] = set()
    for chunk in raw.replace(";", ",").split(","):
        text = chunk.strip()
        if not text:
            continue
        try:
            ids.add(int(text))
        except ValueError:
            continue
    return ids
