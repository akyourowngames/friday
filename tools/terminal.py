import os
import subprocess
import sys
import time

from config import settings
from tools.registry import tool
from tools.runtime import (
    coerce_bool,
    emit_trace,
    error_payload,
    make_trace,
    normalize_int,
    normalize_response_format,
    normalize_timeout_ms,
    structured_error,
    structured_success,
    utc_now_iso,
)


_TERMINAL_VERSION = "2.0.0"


def _strip_ansi(text: str) -> str:
    cleaned = []
    i = 0
    while i < len(text):
        char = text[i]
        if char != "\x1b":
            cleaned.append(char)
            i += 1
            continue
        i += 1
        if i < len(text) and text[i] == "[":
            i += 1
            while i < len(text):
                final = text[i]
                i += 1
                if "@" <= final <= "~":
                    break
        elif i < len(text):
            i += 1
    return "".join(cleaned)


def _safe_with_meta(text: str, max_len: int = 5000) -> tuple[str, bool]:
    text = _strip_ansi(text)
    if len(text) > max_len:
        return text[:max_len] + "\n...[truncated]", True
    return text, False


def _safe(text: str, max_len: int = 5000) -> str:
    text, _ = _safe_with_meta(text, max_len)
    return text


def _detect_shell(command: str) -> list[str]:
    if sys.platform == "win32":
        return ["powershell", "-NoProfile", "-Command", command]
    return ["bash", "-c", command]


def _run_command(command: str, timeout: float, max_output_chars: int) -> dict:
    try:
        opened = _start_existing_path(command)
        if opened is not None:
            return opened
        command = _normalize_command(command)
        shell_cmd = _detect_shell(command)
        cp = subprocess.run(shell_cmd, capture_output=True, text=True, timeout=timeout)
        stdout, stdout_truncated = _safe_with_meta(cp.stdout or "", max_output_chars)
        stderr, stderr_truncated = _safe_with_meta(cp.stderr or "", max_output_chars)
        return {
            "code": cp.returncode,
            "stdout": stdout,
            "stderr": stderr,
            "error_code": None if cp.returncode == 0 else "COMMAND_FAILED",
            "execution_path": "shell",
            "opened_path": None,
            "truncated": stdout_truncated or stderr_truncated,
        }
    except subprocess.TimeoutExpired:
        return {
            "code": -1,
            "stdout": "",
            "stderr": f"Command timed out after {timeout}s",
            "error_code": "COMMAND_TIMEOUT",
            "execution_path": "shell",
            "opened_path": None,
            "truncated": False,
        }
    except FileNotFoundError as e:
        return {
            "code": -1,
            "stdout": "",
            "stderr": f"Command not found: {e}",
            "error_code": "COMMAND_NOT_FOUND",
            "execution_path": "shell",
            "opened_path": None,
            "truncated": False,
        }
    except PermissionError as e:
        return {
            "code": -1,
            "stdout": "",
            "stderr": f"Permission denied: {e}",
            "error_code": "PERMISSION_DENIED",
            "execution_path": "shell",
            "opened_path": None,
            "truncated": False,
        }
    except OSError as e:
        return {
            "code": -1,
            "stdout": "",
            "stderr": f"System error: {e}",
            "error_code": "SYSTEM_COMMAND_ERROR",
            "execution_path": "shell",
            "opened_path": None,
            "truncated": False,
        }
    except Exception as e:
        return {
            "code": -1,
            "stdout": "",
            "stderr": f"Unexpected error: {e.__class__.__name__}",
            "error_code": "SYSTEM_COMMAND_ERROR",
            "execution_path": "shell",
            "opened_path": None,
            "truncated": False,
        }


def _normalize_command(command: str) -> str:
    if sys.platform != "win32":
        return command
    stripped = command.strip()
    prefix = "start "
    if not stripped.lower().startswith(prefix):
        return command
    target = stripped[len(prefix):].strip()
    if not target or target.startswith('"'):
        return command
    if os.path.exists(target):
        return f'start "" "{target}"'
    return command


def _start_existing_path(command: str) -> dict | None:
    if sys.platform != "win32":
        return None
    stripped = command.strip()
    prefix = "start "
    if not stripped.lower().startswith(prefix):
        return None
    target = stripped[len(prefix):].strip()
    if target.startswith('""'):
        target = target[2:].strip()
    if target.startswith('"') and target.endswith('"') and len(target) >= 2:
        target = target[1:-1]
    if not target or not os.path.exists(target):
        return None
    try:
        os.startfile(target)
        return {
            "code": 0,
            "stdout": f"Opened: {target}",
            "stderr": "",
            "error_code": None,
            "execution_path": "open_path",
            "opened_path": target,
            "truncated": False,
        }
    except PermissionError as e:
        return {
            "code": -1,
            "stdout": "",
            "stderr": f"Permission denied: {e}",
            "error_code": "PERMISSION_DENIED",
            "execution_path": "open_path",
            "opened_path": target,
            "truncated": False,
        }
    except Exception as e:
        return {
            "code": -1,
            "stdout": "",
            "stderr": f"Could not open path: {e.__class__.__name__}",
            "error_code": "SYSTEM_COMMAND_ERROR",
            "execution_path": "open_path",
            "opened_path": target,
            "truncated": False,
        }


def _format_result(res: dict) -> str:
    if res["code"] == 0:
        out = res["stdout"].strip()
        if out:
            return out
        return "Command completed successfully (no output)."
    parts = [f"Exit code: {res['code']}"]
    if res["stdout"].strip():
        parts.append(res["stdout"].strip())
    if res["stderr"].strip():
        parts.append(res["stderr"].strip())
    return "\n".join(parts)


def _trace(started_at: str, started: float, inputs_received: int, schema_valid: bool, execution_path: str, status: str, output_fields: int, error_code: str | None = None) -> dict:
    external_calls = {"count": 0, "systems": []}
    if execution_path in ("shell", "open_path"):
        external_calls = {"count": 1, "systems": [execution_path]}
    return make_trace(
        "terminal",
        _TERMINAL_VERSION,
        started_at,
        started,
        inputs_received,
        schema_valid,
        execution_path,
        status,
        output_fields,
        external_calls,
        error_code,
    )


def _terminal_error(error: dict, response_format: str, trace_enabled: bool, started: float, started_at: str, inputs_received: int, execution_path: str, legacy: str):
    trace = _trace(started_at, started, inputs_received, False, execution_path, "FAILED", 1, error["code"])
    emit_trace(trace, trace_enabled)
    if response_format == "structured":
        return structured_error("terminal", _TERMINAL_VERSION, error, started, trace)
    return legacy


@tool(
    name="terminal",
    description="Open any application, run any command or script, launch programs and files. Full system access. Use for: opening apps (notepad, chrome, code, explorer, word, excel, calculator, cmd), running scripts (python, node, npm, pip), launching programs (file explorer, task manager, control panel), shell commands, package management, system info, process management, file operations, system settings (volume, brightness, display, network), checking or changing ANY system state via command line. Returns command output or error details. This is the tool to use when the user says 'open', 'launch', 'start', 'run', 'change', 'set', 'adjust', 'check', or 'show' anything about the system",
    examples=[
        "open notepad",
        "open me notepad",
        "open file explorer",
        "open me file explorer",
        "launch chrome",
        "launch calculator",
        "start command prompt",
        "open image.png",
        "open the generated image",
        "show imagine_20260517_xxx.png",
        "run python script.py",
        "run npm install express",
        "run pip install requests",
        "show all running processes",
        "check disk space",
        "create a new directory",
        "list files in current directory",
        "whoami",
        "systeminfo",
        "what is the current system volume",
        "increase the volume to 75",
        "set volume to 50 percent",
        "check remaining battery",
        "show wifi signal strength",
    ],
    param_descriptions={
        "command": "Shell command to execute (PowerShell syntax on Windows, bash on Linux/macOS)",
        "workdir": "Working directory for the command (default: project root)",
        "timeout": "Max execution time in seconds. Uses configured default when omitted or set to 0",
        "dry_run": "When true, report what would run without executing the command",
        "max_output_chars": "Maximum stdout/stderr characters to return, from 200 to 20000",
        "timeout_ms": "Optional millisecond timeout override, from 1 to 60000. Use 0 to keep timeout seconds",
        "response_format": "legacy or structured. Default legacy preserves existing output",
        "trace_enabled": "When true, emit a machine-readable trace entry",
    },
)
def terminal(
    command: str,
    workdir: str = ".",
    timeout: int = 30,
    dry_run: bool = False,
    max_output_chars: int = 5000,
    timeout_ms: int = 0,
    response_format: str = "legacy",
    trace_enabled: bool = False,
):
    started = time.perf_counter()
    started_at = utc_now_iso()
    inputs_received = 8
    response_format = normalize_response_format(response_format)
    trace_enabled = coerce_bool(trace_enabled)
    dry_run = coerce_bool(dry_run)

    output_limit, output_error = normalize_int(
        max_output_chars,
        "max_output_chars",
        5000,
        200,
        20000,
        "Use a maximum output size between 200 and 20000 characters.",
        "INVALID_OUTPUT_LIMIT",
    )
    if output_error is not None:
        return _terminal_error(
            output_error,
            response_format,
            trace_enabled,
            started,
            started_at,
            inputs_received,
            "input_validation",
            "Error executing 'terminal': invalid max_output_chars",
        )

    timeout_input = timeout
    if not isinstance(timeout, bool):
        try:
            if int(timeout) <= 0:
                timeout_input = settings.terminal_default_timeout
        except (TypeError, ValueError):
            timeout_input = timeout
    timeout_seconds, timeout_error = normalize_int(
        timeout_input,
        "timeout",
        settings.terminal_default_timeout,
        1,
        settings.terminal_max_timeout,
        "Use a timeout in seconds within the configured terminal bounds.",
        "INVALID_TIMEOUT",
    )
    if timeout_error is not None:
        return _terminal_error(
            timeout_error,
            response_format,
            trace_enabled,
            started,
            started_at,
            inputs_received,
            "input_validation",
            "Error executing 'terminal': invalid timeout",
        )

    timeout_seconds = max(1, min(timeout_seconds, settings.terminal_max_timeout))
    timeout_override_ms, timeout_ms_error = normalize_timeout_ms(timeout_ms, None)
    if timeout_ms_error is not None:
        return _terminal_error(
            timeout_ms_error,
            response_format,
            trace_enabled,
            started,
            started_at,
            inputs_received,
            "input_validation",
            "Error executing 'terminal': invalid timeout_ms",
        )
    effective_timeout_ms = timeout_override_ms if timeout_override_ms is not None else int(timeout_seconds * 1000)
    run_timeout_seconds = effective_timeout_ms / 1000

    if not str(command or "").strip():
        error = error_payload(
            "EMPTY_COMMAND",
            "command must not be empty.",
            "command",
            command,
            "non-empty shell command",
            False,
            "Pass the command or application launch request to run.",
        )
        return _terminal_error(
            error,
            response_format,
            trace_enabled,
            started,
            started_at,
            inputs_received,
            "input_validation",
            "Error executing 'terminal': command is required",
        )

    cwd = os.path.abspath(workdir) if workdir else os.getcwd()
    if not os.path.isdir(cwd):
        error = error_payload(
            "DIRECTORY_NOT_FOUND",
            "The requested working directory does not exist.",
            "workdir",
            workdir,
            "existing directory",
            False,
            "Use an existing directory or omit workdir to use the current project.",
        )
        return _terminal_error(
            error,
            response_format,
            trace_enabled,
            started,
            started_at,
            inputs_received,
            "input_validation",
            f"Directory not found: {cwd}",
        )

    if dry_run:
        result = {
            "command": command,
            "cwd": cwd,
            "timeout_ms": effective_timeout_ms,
            "exit_code": None,
            "stdout": "",
            "stderr": "",
            "opened_path": None,
            "dry_run": True,
            "truncated": False,
            "status": "DRY_RUN",
        }
        trace = _trace(started_at, started, inputs_received, True, "dry_run", "SUCCESS", len(result))
        emit_trace(trace, trace_enabled)
        if response_format == "structured":
            return structured_success("terminal", _TERMINAL_VERSION, result, started, trace)
        return f"Dry run: would run in {cwd}\nCommand: {command}\nTimeout: {effective_timeout_ms}ms"

    old_cwd = os.getcwd()
    try:
        os.chdir(cwd)
        result = _run_command(command, run_timeout_seconds, output_limit)
    finally:
        os.chdir(old_cwd)

    if result["code"] != 0:
        formatted = _format_result(result)
        if "UnauthorizedAccess" in result.get("stderr", "") or "Access is denied" in result.get("stderr", ""):
            formatted += "\n\nTry running with elevated privileges or use a different command."
        trace = _trace(started_at, started, inputs_received, True, result.get("execution_path", "shell"), "FAILED", 1, result.get("error_code"))
        emit_trace(trace, trace_enabled)
        if response_format == "structured":
            error = error_payload(
                result.get("error_code") or "COMMAND_FAILED",
                "The terminal command did not complete successfully.",
                "command",
                {
                    "command": command,
                    "cwd": cwd,
                    "exit_code": result["code"],
                    "stdout": result.get("stdout", ""),
                    "stderr": result.get("stderr", ""),
                },
                "exit code 0",
                result.get("error_code") == "COMMAND_TIMEOUT",
                "Inspect stdout and stderr, adjust the command or timeout, then retry only if the operation is safe.",
            )
            return structured_error("terminal", _TERMINAL_VERSION, error, started, trace)
        return formatted
    structured = {
        "command": command,
        "cwd": cwd,
        "timeout_ms": effective_timeout_ms,
        "exit_code": result["code"],
        "stdout": result["stdout"],
        "stderr": result["stderr"],
        "opened_path": result.get("opened_path"),
        "dry_run": False,
        "truncated": result.get("truncated", False),
        "status": "SUCCESS",
    }
    trace = _trace(started_at, started, inputs_received, True, result.get("execution_path", "shell"), "SUCCESS", len(structured))
    emit_trace(trace, trace_enabled)
    if response_format == "structured":
        return structured_success("terminal", _TERMINAL_VERSION, structured, started, trace)
    return _format_result(result)
