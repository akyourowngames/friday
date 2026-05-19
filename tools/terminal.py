import os
import subprocess
import sys

from config import settings
from tools.registry import tool


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


def _safe(text: str, max_len: int = 5000) -> str:
    text = _strip_ansi(text)
    if len(text) > max_len:
        text = text[:max_len] + "\n...[truncated]"
    return text


def _detect_shell(command: str) -> list[str]:
    if sys.platform == "win32":
        return ["powershell", "-NoProfile", "-Command", command]
    return ["bash", "-c", command]


def _run_command(command: str, timeout: int) -> dict:
    try:
        opened = _start_existing_path(command)
        if opened is not None:
            return opened
        command = _normalize_command(command)
        shell_cmd = _detect_shell(command)
        cp = subprocess.run(shell_cmd, capture_output=True, text=True, timeout=timeout)
        stdout = _safe(cp.stdout or "")
        stderr = _safe(cp.stderr or "")
        return {"code": cp.returncode, "stdout": stdout, "stderr": stderr}
    except subprocess.TimeoutExpired:
        return {"code": -1, "stdout": "", "stderr": f"Command timed out after {timeout}s"}
    except FileNotFoundError as e:
        return {"code": -1, "stdout": "", "stderr": f"Command not found: {e}"}
    except PermissionError as e:
        return {"code": -1, "stdout": "", "stderr": f"Permission denied: {e}"}
    except OSError as e:
        return {"code": -1, "stdout": "", "stderr": f"System error: {e}"}
    except Exception as e:
        return {"code": -1, "stdout": "", "stderr": f"Unexpected error: {e}"}


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
        return {"code": 0, "stdout": f"Opened: {target}", "stderr": ""}
    except Exception as e:
        return {"code": -1, "stdout": "", "stderr": f"Could not open path: {e}"}


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
    },
)
def terminal(command: str, workdir: str = ".", timeout: int = 30) -> str:
    timeout = settings.terminal_default_timeout if timeout <= 0 else timeout
    timeout = max(1, min(timeout, settings.terminal_max_timeout))
    cwd = os.path.abspath(workdir) if workdir else os.getcwd()
    if not os.path.isdir(cwd):
        return f"Directory not found: {cwd}"

    old_cwd = os.getcwd()
    try:
        os.chdir(cwd)
        result = _run_command(command, timeout)
    finally:
        os.chdir(old_cwd)

    if result["code"] != 0:
        formatted = _format_result(result)
        if "UnauthorizedAccess" in result.get("stderr", "") or "Access is denied" in result.get("stderr", ""):
            formatted += "\n\nTry running with elevated privileges or use a different command."
        return formatted
    return _format_result(result)
