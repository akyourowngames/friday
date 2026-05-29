from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from config import settings

from .configuration import TelegramWatcherConfig, load_config


def load_cli_config(config_path: str | None = None) -> TelegramWatcherConfig:
    return load_config(".", config_path or settings.telegram_watcher_config_file)


def service_health(config: TelegramWatcherConfig, timeout_seconds: float = 0.75) -> dict[str, Any]:
    try:
        with urllib.request.urlopen(config.service_base_url + "/health", timeout=timeout_seconds) as response:
            payload = json.loads(response.read().decode("utf-8", errors="replace"))
    except (OSError, TimeoutError, urllib.error.URLError, json.JSONDecodeError):
        return {"ok": False, "status": "unavailable"}
    if isinstance(payload, dict) and payload.get("status") == "ok":
        return {"ok": True, "status": "ok", "data": payload}
    return {"ok": False, "status": "unexpected_response", "data": payload}


def ensure_service_for_cli(config_path: str | None = None) -> tuple[TelegramWatcherConfig, dict[str, Any]]:
    config = load_cli_config(config_path)
    if not config.cli_bridge_enabled:
        return config, {"status": "disabled", "reason": "cli_bridge_disabled"}
    if not config.main_cli_autostart:
        return config, {"status": "disabled", "reason": "main_cli_autostart_disabled"}
    if not config.token:
        return config, {"status": "disabled", "reason": "missing_token"}
    if not config.authorized_user_ids and not config.authorized_chat_ids:
        return config, {"status": "disabled", "reason": "missing_authorized_ids"}

    health = service_health(config)
    if health.get("ok"):
        return config, {"status": "running", "health": health}

    started = _start_background_service(config)
    if started.get("status") != "started":
        return config, started

    deadline = time.time() + max(0.5, config.api_startup_wait_ms / 1000)
    while time.time() < deadline:
        health = service_health(config)
        if health.get("ok"):
            started["health"] = health
            return config, started
        time.sleep(0.2)
    started["health"] = {"ok": False, "status": "startup_timeout"}
    return config, started


def ensure_service_for_tool(config_path: str | None = None) -> tuple[TelegramWatcherConfig, dict[str, Any]]:
    config = load_cli_config(config_path)
    if not config.cli_bridge_enabled:
        return config, {"status": "disabled", "reason": "cli_bridge_disabled"}
    if not config.token:
        return config, {"status": "disabled", "reason": "missing_token"}
    if not config.authorized_user_ids and not config.authorized_chat_ids:
        return config, {"status": "disabled", "reason": "missing_authorized_ids"}

    health = service_health(config)
    if health.get("ok"):
        return config, {"status": "running", "health": health}

    started = _start_background_service(config)
    if started.get("status") != "started":
        return config, started

    deadline = time.time() + max(0.5, config.api_startup_wait_ms / 1000)
    while time.time() < deadline:
        health = service_health(config)
        if health.get("ok"):
            started["health"] = health
            return config, started
        time.sleep(0.2)
    started["health"] = {"ok": False, "status": "startup_timeout"}
    return config, started


def send_cli_message(config: TelegramWatcherConfig, message: str, session_id: str = "main_cli") -> dict[str, Any]:
    payload = json.dumps({"message": message, "session_id": session_id}).encode("utf-8")
    request = urllib.request.Request(
        config.service_base_url + "/cli/message",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=max(5, int(config.request_timeout_ms / 1000))) as response:
            parsed = json.loads(response.read().decode("utf-8", errors="replace"))
    except (OSError, TimeoutError, urllib.error.URLError, json.JSONDecodeError) as exc:
        return {"handled": False, "status": "unavailable", "error": exc.__class__.__name__}
    return parsed if isinstance(parsed, dict) else {"handled": False, "status": "unexpected_response"}


def _start_background_service(config: TelegramWatcherConfig) -> dict[str, Any]:
    script = config.repo_root / "telegram_watcher_service.py"
    if not script.exists():
        return {"status": "blocked", "reason": "missing_service_script", "script": str(script)}
    log_path = (config.repo_root / "storage" / "telegram_watcher_service.log").resolve()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    args = [
        sys.executable,
        str(script),
        "run",
        "--config",
        str(config.config_path),
    ]
    creationflags = 0
    if os.name == "nt":
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "DETACHED_PROCESS", 0)
    try:
        with log_path.open("ab") as log_handle:
            process = subprocess.Popen(
                args,
                cwd=str(config.repo_root),
                stdin=subprocess.DEVNULL,
                stdout=log_handle,
                stderr=log_handle,
                creationflags=creationflags,
                close_fds=False if os.name == "nt" else True,
            )
    except OSError as exc:
        return {"status": "blocked", "reason": "start_failed", "error": exc.__class__.__name__}
    return {"status": "started", "pid": process.pid, "log_path": str(log_path)}
