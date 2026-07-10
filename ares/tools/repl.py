"""Persistent, framed Python and shell execution sessions.

The Python worker keeps its protocol on a duplicated control descriptor while
temporarily redirecting OS file descriptors 1/2 for each execution.  That
captures child-process output and ``sys.stdout.buffer`` writes without letting
them corrupt the protocol response.
"""
from __future__ import annotations

import hashlib
import json
import os
import queue
import re
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from uuid import uuid4


COMMAND_PROFILES: dict[str, dict[str, int]] = {
    "quick": {"timeout": 15},
    "test": {"timeout": 120},
    "build": {"timeout": 180},
    "long": {"timeout": 300},
}
MAX_CAPTURE_BYTES = 1_000_000


def re_match_windows_prompt(value: str) -> bool:
    return bool(re.fullmatch(r"(?:[A-Za-z]:)?[^\r\n<>|]*>\s*", value))


_PYTHON_WORKER = r'''
import json, os, sys, tempfile, traceback
_MAX_CAPTURE = 1000000
_control = os.fdopen(os.dup(sys.stdout.fileno()), "w", encoding="utf-8", errors="backslashreplace", buffering=1)
_namespace = {}

def _read_limited(handle):
    handle.seek(0)
    data = handle.read(_MAX_CAPTURE + 1)
    truncated = len(data) > _MAX_CAPTURE
    text = data[:_MAX_CAPTURE].decode("utf-8", errors="replace")
    return text.replace("\r\n", "\n"), truncated

for _line in sys.stdin:
    _uid = "unknown"
    try:
        _message = json.loads(_line)
        _uid = _message["id"]
        _code = _message["code"]
        _error = None
        _original_stdout, _original_stderr = sys.stdout, sys.stderr
        with tempfile.TemporaryFile(mode="w+b") as _out, tempfile.TemporaryFile(mode="w+b") as _err:
            _saved_out, _saved_err = os.dup(1), os.dup(2)
            try:
                try:
                    _original_stdout.flush(); _original_stderr.flush()
                except Exception:
                    pass
                os.dup2(_out.fileno(), 1)
                os.dup2(_err.fileno(), 2)
                try:
                    exec(compile(_code, "<ares-run-code>", "exec"), _namespace, _namespace)
                except BaseException:
                    _error = traceback.format_exc()
            finally:
                try:
                    sys.stdout.flush(); sys.stderr.flush()
                except Exception:
                    pass
                sys.stdout, sys.stderr = _original_stdout, _original_stderr
                os.dup2(_saved_out, 1); os.dup2(_saved_err, 2)
                os.close(_saved_out); os.close(_saved_err)
            _stdout, _stdout_truncated = _read_limited(_out)
            _stderr, _stderr_truncated = _read_limited(_err)
        _control.write(json.dumps({"id": _uid, "stdout": _stdout, "stderr": _stderr, "error": _error, "stdout_truncated": _stdout_truncated, "stderr_truncated": _stderr_truncated}, ensure_ascii=False) + "\n")
    except BaseException:
        _control.write(json.dumps({"id": _uid, "stdout": "", "stderr": "", "error": traceback.format_exc()}) + "\n")
    _control.flush()
'''


class REPLSession:
    """One persistent subprocess with a collision-resistant result frame."""

    def __init__(self, lang: str = "python", cwd: str | None = None):
        if lang not in {"python", "shell"}:
            raise ValueError(f"Unsupported REPL language: {lang}")
        self.lang = lang
        self.cwd = cwd
        self.process: subprocess.Popen[str] | None = None
        self._lock = threading.Lock()
        self._stdout: queue.Queue[str | None] = queue.Queue()
        self._stderr: queue.Queue[str | None] = queue.Queue()
        self._reader_threads: list[threading.Thread] = []

    def start(self) -> None:
        if self.alive:
            return
        if self.lang == "python":
            args = [sys.executable, "-u", "-c", _PYTHON_WORKER]
            env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
        elif sys.platform == "win32":
            # /V:ON lets the sentinel capture the post-command errorlevel.
            args = ["cmd.exe", "/Q", "/D", "/V:ON"]
            env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
        else:
            args = ["bash", "--noprofile", "--norc"]
            env = {**os.environ, "LC_ALL": "C.UTF-8", "LANG": "C.UTF-8"}
        creation: dict[str, object] = {}
        if sys.platform == "win32":
            creation["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        else:
            creation["start_new_session"] = True
        self.process = subprocess.Popen(
            args,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=self.cwd,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            env=env,
            **creation,
        )
        self._stdout = queue.Queue()
        self._stderr = queue.Queue()
        self._reader_threads = [
            threading.Thread(target=self._read_stream, args=(self.process.stdout, self._stdout), daemon=True),
            threading.Thread(target=self._read_stream, args=(self.process.stderr, self._stderr), daemon=True),
        ]
        for thread in self._reader_threads:
            thread.start()
        if self.lang == "shell" and sys.platform == "win32":
            # cmd otherwise emits using the active OEM code page, which makes
            # Unicode command results nondeterministic across installations.
            self._write("chcp 65001 >nul\r\n")

    @staticmethod
    def _read_stream(stream, output: queue.Queue[str | None]) -> None:
        try:
            for line in iter(stream.readline, ""):
                output.put(line)
        except (OSError, ValueError):
            pass
        finally:
            output.put(None)

    @property
    def alive(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def ensure_alive(self) -> None:
        if not self.alive:
            self.close()
            self.start()

    def _terminate_tree(self) -> None:
        process = self.process
        if process is None or process.poll() is not None:
            return
        try:
            if sys.platform == "win32":
                subprocess.run(
                    ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=5,
                    check=False,
                )
            else:
                os.killpg(process.pid, signal.SIGKILL)
        except (OSError, subprocess.SubprocessError):
            with suppress_process_lookup():
                process.kill()

    def close(self) -> None:
        if self.process is None:
            return
        process = self.process
        try:
            if process.stdin:
                process.stdin.close()
        except (OSError, ValueError):
            pass
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            self._terminate_tree()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                pass
        self.process = None

    def execute(self, code: str, timeout: int = 30) -> dict:
        self.ensure_alive()
        uid = uuid4().hex
        if self.lang == "python":
            return self._execute_python(code, uid, max(1, int(timeout)))
        return self._execute_shell(code, uid, max(1, int(timeout)))

    def _execute_python(self, code: str, uid: str, timeout: int) -> dict:
        with self._lock:
            if not self._write(json.dumps({"id": uid, "code": code}, ensure_ascii=False) + "\n"):
                return {"id": uid, "stdout": "", "stderr": "", "error": "REPL crashed and restarted"}
            return self._read_python_result(uid, timeout)

    def _execute_shell(self, command: str, uid: str, timeout: int) -> dict:
        sentinel = f"__ARES_SENTINEL_{uid}__"
        if sys.platform == "win32":
            framed = f"{command}\r\nset \"__ARES_STATUS=!errorlevel!\"\r\necho {sentinel}:!__ARES_STATUS!\r\n"
        else:
            framed = f"{command}\n__ares_status=$?\nprintf '%s:%s\\n' '{sentinel}' \"$__ares_status\"\n"
        with self._lock:
            if not self._write(framed):
                return {"id": uid, "stdout": "", "stderr": "", "error": "REPL crashed and restarted", "exit_code": None}
            stdout, timed_out, exit_code = self._collect_until(self._stdout, sentinel, timeout)
            stderr = self._drain(self._stderr)
            if sys.platform == "win32":
                stdout = self._clean_windows_shell_output(stdout)
            if timed_out:
                self.close()  # complete process tree and force a clean session next call
                return {"id": uid, "stdout": stdout, "stderr": stderr, "error": f"Timeout after {timeout}s", "exit_code": None}
            error = None if exit_code == 0 else f"Command exited with status {exit_code}"
            return {"id": uid, "stdout": stdout, "stderr": stderr, "error": error, "exit_code": exit_code}

    def _write(self, text: str) -> bool:
        try:
            assert self.process is not None and self.process.stdin is not None
            self.process.stdin.write(text)
            self.process.stdin.flush()
            return True
        except (AssertionError, OSError, BrokenPipeError, ValueError):
            self.close()
            return False

    def _read_python_result(self, uid: str, timeout: int) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                line = self._stdout.get(timeout=min(0.1, max(0.01, deadline - time.monotonic())))
            except queue.Empty:
                if not self.alive:
                    break
                continue
            if line is None:
                break
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                # Control output is deliberately isolated; this indicates a
                # corrupted worker and merits a restart, not silent discard.
                self.close()
                return {"id": uid, "stdout": "", "stderr": self._drain(self._stderr), "error": "Python REPL protocol corruption; session reset"}
            if payload.get("id") == uid:
                payload["stderr"] = (payload.get("stderr") or "") + self._drain(self._stderr)
                return payload
        self.close()
        return {"id": uid, "stdout": "", "stderr": self._drain(self._stderr), "error": f"Timeout after {timeout}s"}

    def _collect_until(self, output: queue.Queue[str | None], sentinel: str, timeout: int) -> tuple[str, bool, int | None]:
        deadline = time.monotonic() + timeout
        lines: list[str] = []
        while time.monotonic() < deadline:
            try:
                line = output.get(timeout=min(0.1, max(0.01, deadline - time.monotonic())))
            except queue.Empty:
                if not self.alive:
                    break
                continue
            if line is None:
                break
            if sentinel in line:
                marker = line.strip().split(sentinel, 1)[-1].lstrip(":")
                try:
                    return "".join(lines), False, int(marker.split()[0])
                except (ValueError, IndexError):
                    self.close()
                    return "".join(lines), True, None
            lines.append(line)
        return "".join(lines), True, None

    @staticmethod
    def _drain(output: queue.Queue[str | None]) -> str:
        lines: list[str] = []
        while True:
            try:
                line = output.get_nowait()
            except queue.Empty:
                break
            if line is not None:
                lines.append(line)
        return "".join(lines)

    @staticmethod
    def _clean_windows_shell_output(output: str) -> str:
        """Remove cmd's interactive banner/prompts from captured command data."""
        cleaned: list[str] = []
        for line in output.replace("\r\n", "\n").splitlines():
            prompt = re.match(r"^(?:[A-Za-z]:)?[^\r\n<>|]*>", line)
            if prompt:
                line = line[prompt.end():]
            stripped = line.strip()
            if not stripped:
                continue
            if stripped.startswith("Microsoft Windows [Version") or stripped.startswith("(c) Microsoft Corporation"):
                continue
            if re_match_windows_prompt(stripped):
                continue
            cleaned.append(line)
        return "\n".join(cleaned) + ("\n" if cleaned else "")


class suppress_process_lookup:
    """Tiny local context manager to avoid importing contextlib for one call."""

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return exc_type in {ProcessLookupError, OSError}


class PersistentREPL:
    """Own the pinned Python namespace and one persistent command shell."""

    def __init__(self) -> None:
        self.python_session: REPLSession | None = None
        self.shell_session: REPLSession | None = None
        self._python_lock = threading.Lock()
        self._shell_lock = threading.Lock()
        self.python_generation = 0
        self.shell_generation = 0

    def execute_python(self, code: str, timeout: int = 30, cwd: str | None = None) -> str:
        with self._python_lock:
            if cwd and self.python_session and self.python_session.cwd != cwd:
                self.python_session.close()
                self.python_session = None
            if not self.python_session or not self.python_session.alive:
                self.python_session = REPLSession("python", cwd=cwd)
                self.python_session.start()
                self.python_generation += 1
            result = self.python_session.execute(code, timeout=timeout)
        return self._format_result(result)

    def execute_shell(self, command: str, timeout: int = 30, cwd: str | None = None, profile: str | None = None) -> str:
        timeout = COMMAND_PROFILES.get((profile or "").lower(), {}).get("timeout", timeout)
        with self._shell_lock:
            if cwd and self.shell_session and self.shell_session.cwd != cwd:
                self.shell_session.close()
                self.shell_session = None
            if not self.shell_session or not self.shell_session.alive:
                self.shell_session = REPLSession("shell", cwd=cwd)
                self.shell_session.start()
                self.shell_generation += 1
            result = self.shell_session.execute(command, timeout=timeout)
        return self._format_result(result)

    def reset_python(self) -> None:
        with self._python_lock:
            if self.python_session:
                self.python_session.close()
            self.python_session = None
            self.python_generation += 1

    def reset_shell(self) -> None:
        with self._shell_lock:
            if self.shell_session:
                self.shell_session.close()
            self.shell_session = None
            self.shell_generation += 1

    def dependency_fingerprint(self, cwd: str | None = None) -> str:
        root = Path(cwd or os.getcwd()).expanduser().resolve()
        material = [sys.executable, sys.version, str(root)]
        for name in ("pyproject.toml", "requirements.txt", "requirements-dev.txt", "package.json", "package-lock.json", "pnpm-lock.yaml", "yarn.lock"):
            path = root / name
            if not path.exists():
                continue
            try:
                file_stat = path.stat()
                material.append(f"{name}:{file_stat.st_size}:{hashlib.sha256(path.read_bytes()).hexdigest()}")
            except OSError:
                material.append(f"{name}:unreadable")
        return hashlib.sha256("\n".join(material).encode("utf-8")).hexdigest()[:16]

    def close(self) -> None:
        if self.python_session:
            self.python_session.close()
            self.python_session = None
        if self.shell_session:
            self.shell_session.close()
            self.shell_session = None

    @staticmethod
    def _format_result(result: dict) -> str:
        parts: list[str] = []
        stdout = (result.get("stdout") or "").rstrip()
        stderr = (result.get("stderr") or "").rstrip()
        error = result.get("error")
        exit_code = result.get("exit_code")
        if exit_code is not None:
            parts.append(f"Exit code: {exit_code}")
            parts.append(f"Summary: status={'ok' if exit_code == 0 and not error else 'failed'}; stdout_lines={len(stdout.splitlines()) if stdout else 0}; stderr_lines={len(stderr.splitlines()) if stderr else 0}")
        if stdout:
            parts.append(stdout)
        if result.get("stdout_truncated"):
            parts.append(f"[stdout truncated at {MAX_CAPTURE_BYTES} bytes]")
        if stderr:
            parts.append(f"stderr: {stderr}")
        if result.get("stderr_truncated"):
            parts.append(f"[stderr truncated at {MAX_CAPTURE_BYTES} bytes]")
        if error:
            parts.append(f"Error: {error}")
        return "\n".join(parts) if parts else "Executed successfully (no output)"
