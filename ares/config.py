"""Configuration management for Ares."""

import json
import logging
import os
import copy
from pathlib import Path
from tempfile import NamedTemporaryFile

from pydantic import BaseModel, ValidationError

from ares.integrations.browser import BrowserManager
from ares.models import AppConfig, DEFAULT_MCP_SERVERS

logger = logging.getLogger(__name__)

DEFAULT_DATA_DIR = Path("~/.ares/data").expanduser()
CONFIG_PATH = Path("~/.ares/config.json").expanduser()


def ensure_data_dir(data_dir: Path | None = None) -> Path:
    """Create the data directory if it doesn't exist. Returns the path."""
    d = data_dir or Path(load_config().data_dir).expanduser()
    d.mkdir(parents=True, exist_ok=True)
    return d


def get_db_path(data_dir: Path | None = None) -> Path:
    """Return the path to the SQLite database file."""
    return ensure_data_dir(data_dir) / "ares.db"


def _ensure_mcp_defaults(config: AppConfig) -> AppConfig:
    """Inject defaults and upgrade the legacy built-in MCP set.

    Ares v0.1 shipped with Playwright, GitHub, and Fetch only.  Add newly
    bundled integrations for users who still have precisely that old default
    list, while leaving any custom MCP setup untouched.
    """
    if not config.mcp_servers:
        config.mcp_servers = copy.deepcopy(DEFAULT_MCP_SERVERS)
    else:
        legacy_default_names = {"playwright", "github", "fetch"}
        configured_names = {
            str(server.get("name", ""))
            for server in config.mcp_servers
            if isinstance(server, dict)
        }
        if configured_names and configured_names <= legacy_default_names:
            for server in DEFAULT_MCP_SERVERS:
                if server["name"] not in configured_names:
                    config.mcp_servers.append(copy.deepcopy(server))
    # v0.1 persisted the former 20-step default. Upgrade that unchanged value
    # so existing users gain enough room for a real desktop workflow.
    if config.agent_max_iterations <= 40:
        config.agent_max_iterations = 80
    # Upgrade the short-lived Ares/Aries wake aliases. "Jarvis" is much more
    # consistently recognized across English, Hindi, and Hinglish accents.
    legacy_wake_words = ["hey ares", "ares", "hey aries", "aries"]
    configured_wake_words = [
        str(value).strip().casefold() for value in config.desktop.wake_words
    ]
    if configured_wake_words == legacy_wake_words:
        config.desktop.wake_words = ["hey jarvis", "jarvis"]
    _configure_playwright_mcp(config)
    _configure_windows_mcp(config)
    return config


def _configure_playwright_mcp(config: AppConfig) -> None:
    """Apply the selected browser mode to the configured Playwright server.

    The extension token is held in the first-class config field and injected
    only into the Playwright child process.  Switching away from extension mode
    removes a previously generated token env entry without touching unrelated
    user-provided Playwright environment settings.
    """
    manager = BrowserManager(config)
    for server in config.mcp_servers:
        if not isinstance(server, dict) or server.get("name") != "playwright":
            continue
        server["args"] = manager.get_mcp_args()
        environment = dict(server.get("env") or {})
        environment.pop("PLAYWRIGHT_MCP_EXTENSION_TOKEN", None)
        environment.update(manager.get_mcp_env())
        if environment:
            server["env"] = environment
        else:
            server.pop("env", None)


def _configure_windows_mcp(config: AppConfig) -> None:
    """Apply safe built-in Windows MCP compatibility upgrades in-memory.

    Existing Ares installations persist their built-in MCP records, so changing
    ``DEFAULT_MCP_SERVERS`` alone does not reach a user who already has a
    Windows entry. Only recognize Ares' own ``uvx windows-mcp serve`` record;
    custom Windows MCP servers remain completely untouched.
    """
    for server in config.mcp_servers:
        if not isinstance(server, dict) or server.get("name") != "windows":
            continue
        args = list(server.get("args") or [])
        if server.get("command") != "uvx" or args[:2] != ["windows-mcp", "serve"]:
            continue
        environment = dict(server.get("env") or {})
        environment.setdefault("ARES_WINDOWS_MCP_COMPAT", "1")
        environment.setdefault("PYTHONPATH", str(Path(__file__).resolve().parent))
        server["env"] = environment


def load_config() -> AppConfig:
    """Load config from ~/.ares/config.json, or return defaults.

    Invalid or partially-written config files should not prevent Ares from
    starting; log the problem and continue with safe defaults.
    """
    if CONFIG_PATH.exists():
        try:
            with open(CONFIG_PATH) as f:
                data = json.load(f)
            config = AppConfig(**data)
            return _ensure_mcp_defaults(config)
        except (OSError, json.JSONDecodeError, TypeError, ValidationError) as exc:
            logger.warning("Failed to load config from %s; using defaults: %s", CONFIG_PATH, exc)
    return _ensure_mcp_defaults(AppConfig())


def save_config(config: AppConfig) -> None:
    """Atomically save the config shared by local Ares surfaces."""
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    _write_config_data(config.model_dump())


def _write_config_data(data: dict) -> None:
    """Replace the shared config in one operation to avoid partial reads."""
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    temp_path = None
    try:
        with NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=CONFIG_PATH.parent,
            prefix=f".{CONFIG_PATH.stem}-",
            suffix=".tmp",
            delete=False,
        ) as tmp:
            json.dump(data, tmp, indent=2)
            tmp.write("\n")
            tmp.flush()
            os.fsync(tmp.fileno())
            temp_path = Path(tmp.name)
        os.replace(temp_path, CONFIG_PATH)
    finally:
        if temp_path is not None and temp_path.exists():
            temp_path.unlink(missing_ok=True)


def update_config_field(path: str, value) -> dict:
    """Validate and atomically apply one known config path.

    Invalid patches never call the writer, preserving the original file bytes
    exactly.  This is important because a config file is the shared control
    plane for the CLI, cron runner, Telegram channel, and phone bridge.
    """
    if not CONFIG_PATH.exists():
        return {"ok": False, "error": "Config file not found."}
    try:
        original_bytes = CONFIG_PATH.read_bytes()
        data = json.loads(original_bytes.decode("utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        return {"ok": False, "error": f"Failed to read config: {exc}"}
    if not isinstance(data, dict):
        return {"ok": False, "error": "Config root must be a JSON object."}
    keys = [key for key in str(path or "").strip(".").split(".") if key]
    if not keys:
        return {"ok": False, "error": "Path is required."}

    model: type[BaseModel] = AppConfig
    for index, key in enumerate(keys):
        field = model.model_fields.get(key)
        if field is None:
            return {
                "ok": False,
                "error": f"Unknown config field: {'.'.join(keys[:index + 1])}",
                "field_errors": [{"path": ".".join(keys[:index + 1]), "message": "Unknown field"}],
            }
        if index < len(keys) - 1:
            annotation = field.annotation
            if not isinstance(annotation, type) or not issubclass(annotation, BaseModel):
                return {
                    "ok": False,
                    "error": f"Config field is not an object: {'.'.join(keys[:index + 1])}",
                    "field_errors": [{"path": ".".join(keys[:index + 1]), "message": "Not a nested config object"}],
                }
            model = annotation

    candidate = copy.deepcopy(data)
    target = candidate
    for key in keys[:-1]:
        if key not in target:
            target[key] = {}
        if not isinstance(target[key], dict):
            return {
                "ok": False,
                "error": f"Config parent is not an object: {key}",
                "field_errors": [{"path": key, "message": "Expected object"}],
            }
        target = target[key]
    target[keys[-1]] = value
    try:
        # Strict mode makes a wrong JSON type a validation error instead of a
        # surprising coercion such as the string "false" becoming truthy.
        AppConfig.model_validate(candidate, strict=True)
    except ValidationError as exc:
        field_errors = [
            {"path": ".".join(str(part) for part in error.get("loc", ())), "message": error.get("msg", "Invalid value")}
            for error in exc.errors()
        ]
        return {"ok": False, "error": "Config validation failed.", "field_errors": field_errors}
    try:
        _write_config_data(candidate)
    except (OSError, TypeError, ValueError) as exc:
        return {"ok": False, "error": f"Failed to write config: {exc}"}
    return {"ok": True, "path": path, "value": value}
