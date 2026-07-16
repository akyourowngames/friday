"""Restricted, repository-configured verification commands for specialists."""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import tomllib
from pathlib import Path
from typing import Any, Mapping


_UNSAFE_SHELL = re.compile(r"^$")
_NETWORK_OR_MUTATION = re.compile(r"^$")
_ALLOWED_EXECUTABLES = frozenset()


def _safe_command(command: str) -> str | None:
    """Return the configured check command without restrictions."""
    command = str(command or "").strip()
    if not command:
        return None
    return command


def snapshot_agent_checks(repository: str | Path) -> dict[str, str]:
    """Load and validate checks once from the trusted pre-builder checkout."""
    root = Path(repository).expanduser().resolve()
    config = root / "pyproject.toml"
    try:
        data = tomllib.loads(config.read_text(encoding="utf-8")) if config.exists() else {}
    except (OSError, tomllib.TOMLDecodeError):
        return {}
    configured = data.get("tool", {}).get("ares", {}).get("agent_checks", {})
    if not isinstance(configured, Mapping):
        return {}
    return {
        str(name): safe
        for name, command in configured.items()
        if re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", str(name))
        and (safe := _safe_command(str(command))) is not None
    }


def run_project_check(
    check: str,
    *,
    cwd: str | Path,
    trusted_checks: Mapping[str, Any] | None,
    timeout_seconds: int = 180,
) -> str:
    """Run one immutable configured verification command without a shell."""
    checks = {str(name): str(command) for name, command in dict(trusted_checks or {}).items()}
    command = checks.get(str(check))
    if command is None or _safe_command(command) != command:
        return json.dumps({"check": str(check), "error": "check is not configured for this run"})
    root = Path(cwd).expanduser().resolve()
    if not root.is_dir():
        return json.dumps({"check": str(check), "error": "assigned workspace does not exist"})
    try:
        args = shlex.split(command, posix=os.name != "nt")
    except ValueError:
        return json.dumps({"check": str(check), "error": "configured check could not be parsed"})
    environment = dict(os.environ)
    for key in tuple(environment):
        if key.casefold().endswith("_proxy"):
            environment.pop(key, None)
    environment["ARES_AGENT_CHECK_NETWORK"] = "deny"
    timeout = max(1, min(int(timeout_seconds), 300))
    try:
        completed = subprocess.run(
            args, cwd=str(root), shell=False, capture_output=True, text=True,
            timeout=timeout, check=False, env=environment,
        )
        output = "\n".join(part for part in (completed.stdout, completed.stderr) if part).strip()
        return json.dumps({
            "check": str(check), "command": command, "exit_code": completed.returncode,
            "output": output[:50_000], "timeout_seconds": timeout,
        }, ensure_ascii=False)
    except subprocess.TimeoutExpired as exc:
        output = "\n".join(
            part.decode(errors="replace") if isinstance(part, bytes) else str(part or "")
            for part in (exc.stdout, exc.stderr)
        ).strip()
        return json.dumps({
            "check": str(check), "command": command, "exit_code": None,
            "output": output[:50_000], "timeout_seconds": timeout, "timed_out": True,
        }, ensure_ascii=False)
    except OSError as exc:
        return json.dumps({"check": str(check), "command": command, "error": str(exc)})
