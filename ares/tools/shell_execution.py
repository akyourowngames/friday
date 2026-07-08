"""Shell command execution with output capture."""

from __future__ import annotations

import os
import json
import re
import signal
import subprocess
import sys
from pathlib import Path
import tomllib


# ── Unix → Windows command translation ─────────────────────────
# Maps common Unix commands to PowerShell equivalents so LLM-generated
# bash-style commands work on Windows out of the box.

_UNIX_TO_PS: list[tuple[re.Pattern, str, str]] = [
    # ls [flags] [path] → Get-ChildItem [flags] [path]
    (re.compile(r"^\s*ls(\s)"), "Get-ChildItem", "\\1"),
    # cat → Get-Content
    (re.compile(r"^\s*cat(\s)"), "Get-Content", "\\1"),
    # pwd → Get-Location
    (re.compile(r"^\s*pwd\s*$"), "Get-Location", ""),
    # grep → Select-String
    (re.compile(r"^\s*grep(\s)"), "Select-String", "\\1"),
    # head → Select-Object -First
    (re.compile(r"^\s*head(\s)"), "Select-Object -First", "\\1"),
    # tail → Select-Object -Last
    (re.compile(r"^\s*tail(\s)"), "Select-Object -Last", "\\1"),
    # wc → Measure-Object
    (re.compile(r"^\s*wc(\s)"), "Measure-Object", "\\1"),
    # mkdir -p → New-Item -ItemType Directory -Force
    (re.compile(r"^\s*mkdir\s+-p\s+"), "New-Item -ItemType Directory -Force ", ""),
    # touch → New-Item -ItemType File -Force
    (re.compile(r"^\s*touch\s+"), "New-Item -ItemType File -Force ", ""),
    # rm -rf → Remove-Item -Recurse -Force
    (re.compile(r"^\s*rm\s+-rf\s+"), "Remove-Item -Recurse -Force ", ""),
    # rm → Remove-Item
    (re.compile(r"^\s*rm(\s)"), "Remove-Item", "\\1"),
    # cp → Copy-Item
    (re.compile(r"^\s*cp(\s)"), "Copy-Item", "\\1"),
    # mv → Move-Item
    (re.compile(r"^\s*mv(\s)"), "Move-Item", "\\1"),
    # which → Get-Command
    (re.compile(r"^\s*which(\s)"), "Get-Command", "\\1"),
    # env | grep → Get-ChildItem env: | Select-String
    (re.compile(r"^\s*env\s*\|\s*grep\s+"), "Get-ChildItem env: | Select-String ", ""),
    # df → Get-PSDrive
    (re.compile(r"^\s*df(\s)"), "Get-PSDrive", "\\1"),
    # du → Get-ChildItem -Recurse | Measure-Object
    (re.compile(r"^\s*du(\s)"), "Get-ChildItem -Recurse | Measure-Object", "\\1"),
    # diff → Compare-Object
    (re.compile(r"^\s*diff(\s)"), "Compare-Object", "\\1"),
    # echo → Write-Output
    (re.compile(r"^\s*echo(\s)"), "Write-Output", "\\1"),
]

COMMAND_PROFILES: dict[str, dict[str, int]] = {
    "quick": {"timeout": 15},
    "test": {"timeout": 120},
    "build": {"timeout": 180},
    "long": {"timeout": 300},
}


def _translate_to_powershell(command: str) -> str:
    """Translate common Unix commands to PowerShell equivalents on Windows."""
    if sys.platform != "win32":
        return command

    stripped = command.strip()

    # Don't translate commands that already look like PowerShell
    ps_prefixes = ("powershell", "pwsh", "Get-", "Set-", "New-", "Remove-",
                   "Invoke-", "Select-", "Where-", "ForEach-", "Test-",
                   "Write-", "Import-", "Export-", "Start-", "Stop-")
    if any(stripped.startswith(p) for p in ps_prefixes):
        return command

    # Don't translate if it's a known Windows command (but allow -p/-rf flags
    # which are Unix-specific and break on Windows)
    win_cmds = ("dir", "type", "copy", "del", "ren", "move", "mkdir", "rmdir",
                "cls", "echo", "cd", "chdir", "path", "set", "date", "time",
                "find", "findstr", "sort", "tasklist", "taskkill", "ipconfig",
                "ping", "tracert", "netstat", "systeminfo", "whoami", "hostname")
    first_word = stripped.split()[0].lower().split("/")[-1].split("\\")[-1]
    args = stripped.split()[1:] if len(stripped.split()) > 1 else []
    has_unix_flags = any(a.startswith("-") and len(a) > 1 and a[1] in "prf" for a in args)
    if first_word in win_cmds and not has_unix_flags:
        return command

    for pattern, replacement, suffix in _UNIX_TO_PS:
        if pattern.match(stripped):
            translated = pattern.sub(replacement + suffix, stripped, count=1)
            # Wrap in powershell -Command so shell=True can run it
            return f"powershell -NoProfile -Command \"{translated}\""

    return command


def _profile_timeout(profile: str | None, fallback: int) -> int:
    if not profile:
        return fallback
    return COMMAND_PROFILES.get(profile.lower(), {}).get("timeout", fallback)


def load_project_command_registry(cwd: str | None = None) -> dict[str, str]:
    """Load reusable project commands from common repo config files."""
    root = Path(cwd or os.getcwd()).expanduser().resolve()
    registry: dict[str, str] = {}
    pyproject = root / "pyproject.toml"
    if pyproject.exists():
        try:
            data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
            commands = data.get("tool", {}).get("ares", {}).get("commands", {})
            if isinstance(commands, dict):
                registry.update({str(key): str(value) for key, value in commands.items()})
        except (OSError, tomllib.TOMLDecodeError):
            pass
    package_json = root / "package.json"
    if package_json.exists():
        try:
            data = json.loads(package_json.read_text(encoding="utf-8"))
            scripts = data.get("scripts", {})
            if isinstance(scripts, dict):
                registry.update({f"npm:{key}": f"npm run {key}" for key in scripts})
        except (OSError, json.JSONDecodeError):
            pass
    return registry


def resolve_project_command(command_or_key: str, cwd: str | None = None) -> str:
    """Resolve @command-key aliases from the project command registry."""
    value = (command_or_key or "").strip()
    if not value.startswith("@"):
        return command_or_key
    key = value[1:]
    registry = load_project_command_registry(cwd)
    return registry.get(key, command_or_key)


def _summary(stdout: str, stderr: str, exit_code: int | None) -> str:
    stdout_lines = len(stdout.splitlines()) if stdout else 0
    stderr_lines = len(stderr.splitlines()) if stderr else 0
    status = "ok" if exit_code == 0 else "failed"
    return f"Summary: status={status}; stdout_lines={stdout_lines}; stderr_lines={stderr_lines}"


def run_command(command: str, timeout: int = 30, cwd: str | None = None, profile: str | None = None) -> str:
    """Execute a shell command with output capture.

    Args:
        command: Shell command to execute.
        timeout: Max seconds before kill (1-300, default 30).
        cwd: Working directory for the command.

    Returns:
        Formatted string with exit code and output.
    """
    timeout = max(1, min(_profile_timeout(profile, timeout), 300))
    command = resolve_project_command(command, cwd)
    command = command.replace("~", os.path.expanduser("~"))
    command = _translate_to_powershell(command)

    kwargs = {
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "text": True,
        "cwd": cwd,
        "bufsize": 1,
    }

    if sys.platform != "win32":
        kwargs["start_new_session"] = True

    try:
        proc = subprocess.Popen(command, shell=True, **kwargs)

        try:
            stdout, stderr = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            if sys.platform != "win32":
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                    proc.wait(timeout=2)
                except (ProcessLookupError, subprocess.TimeoutExpired):
                    try:
                        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                    except ProcessLookupError:
                        pass
            else:
                proc.kill()

            stdout, stderr = proc.communicate()
            partial = stdout.rstrip() if stdout else ""
            return (
                f"Error: Command timed out after {timeout}s\n{partial}"
                if partial
                else f"Error: Command timed out after {timeout}s"
            )

        output_parts = []
        if stdout and stdout.strip():
            output_parts.append(f"--- stdout ---\n{stdout.rstrip()}")
        if stderr and stderr.strip():
            output_parts.append(f"--- stderr ---\n{stderr.rstrip()}")

        if not output_parts:
            return f"Exit code: {proc.returncode}\n{_summary(stdout, stderr, proc.returncode)}\n(No output)"

        result = f"Exit code: {proc.returncode}\n{_summary(stdout, stderr, proc.returncode)}\n" + "\n".join(output_parts)

        max_chars = 50_000
        if len(result) > max_chars:
            result = result[:max_chars] + f"\n... (output truncated at {max_chars} chars)"

        return result

    except Exception as e:
        return f"Error running command: {e}"
