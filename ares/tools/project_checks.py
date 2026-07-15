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


_UNSAFE_SHELL = re.compile(r"[|&;<>`$()\r\n]")
_NETWORK_OR_MUTATION = re.compile(
    r"\b(?:curl|wget|invoke-webrequest|invoke-restmethod|git\s+push|npm\s+(?:install|publish)|"
    r"pip\s+install|uv\s+(?:add|sync|pip)|docker\s+push)\b",
    re.IGNORECASE,
)
_ALLOWED_EXECUTABLES = frozenset({"python", "python3", "py", "pytest", "npm", "node"})


def _safe_command(command: str) -> str | None:
    """Return a configured check command only when its grammar is bounded."""
    command = str(command or "").strip()
    if not command or _UNSAFE_SHELL.search(command) or _NETWORK_OR_MUTATION.search(command):
        return None
    try:
        parts = shlex.split(command, posix=os.name != "nt")
    except ValueError:
        return None
    if not parts or Path(parts[0]).name.casefold() not in _ALLOWED_EXECUTABLES:
        return None
    # Python checks may only use the standard test/compile modules. Npm is
    # deliberately limited to an existing named script, never install/exec.
    executable = Path(parts[0]).name.casefold()
    if executable in {"python", "python3", "py"}:
        if len(parts) < 3 or parts[1] != "-m" or parts[2] not in {"pytest", "compileall"}:
            return None
    elif executable == "npm" and (len(parts) != 3 or parts[1] != "run"):
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
