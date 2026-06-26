"""Persistent REPL sessions for stateful code and shell execution."""

from __future__ import annotations

import json
import os
import queue
import signal
import subprocess
import sys
import threading
import time
from uuid import uuid4


class REPLSession:
    """A single persistent subprocess (Python or shell) with sentinel-based framing."""

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
        """Start the persistent subprocess."""
        if self.alive:
            return

        if self.lang == "python":
            worker = 'import contextlib, io, json, sys, traceback\n_ns = {}\nfor _line in sys.stdin:\n    try:\n        _msg = json.loads(_line)\n        _uid = _msg["id"]\n        _code = _msg["code"]\n        _out = io.StringIO()\n        _err = io.StringIO()\n        _error = None\n        try:\n            with contextlib.redirect_stdout(_out), contextlib.redirect_stderr(_err):\n                exec(compile(_code, "<ares-run-code>", "exec"), _ns, _ns)\n        except BaseException:\n            _error = traceback.format_exc()\n        sys.stdout.write(json.dumps({"id": _uid, "stdout": _out.getvalue(), "stderr": _err.getvalue(), "error": _error}) + "\\n")\n        sys.stdout.write("__ARES_DONE_" + _uid + "__\\n")\n        sys.stdout.flush()\n    except BaseException:\n        traceback.print_exc(file=sys.stderr)\n        sys.stderr.flush()\n'
            args = [sys.executable, "-u", "-c", worker]
            env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
        else:
            args = ["cmd.exe"] if sys.platform == "win32" else ["bash", "--noprofile", "--norc"]
            env = None

        self.process = subprocess.Popen(
            args,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=self.cwd,
            text=True,
            bufsize=1,
            env=env,
        )
        self._stdout = queue.Queue()
        self._stderr = queue.Queue()
        self._reader_threads = [
            threading.Thread(target=self._read_stream, args=(self.process.stdout, self._stdout), daemon=True),
            threading.Thread(target=self._read_stream, args=(self.process.stderr, self._stderr), daemon=True),
        ]
        for thread in self._reader_threads:
            thread.start()

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
        """Auto-restart if the process died."""
        if not self.alive:
            self.close()
            self.start()

    def close(self) -> None:
        """Terminate the subprocess gracefully, then force-kill if needed."""
        if self.process is None:
            return
        process = self.process
        try:
            if process.stdin:
                process.stdin.close()
        except (OSError, ValueError):
            pass
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass
        self.process = None

    def execute(self, code: str, timeout: int = 30) -> dict:
        """Send code to the alive process and return a structured result."""
        self.ensure_alive()
        uid = uuid4().hex[:8]
        if self.lang == "python":
            return self._execute_python(code, uid, max(1, int(timeout)))
        return self._execute_shell(code, uid, max(1, int(timeout)))

    def _execute_python(self, code: str, uid: str, timeout: int) -> dict:
        """Execute Python code in the persistent worker process."""
        message = json.dumps({"id": uid, "code": code})
        with self._lock:
            if not self._write(message + "\n"):
                return {"id": uid, "stdout": "", "stderr": "", "error": "REPL crashed and restarted"}
            return self._read_python_result(uid, timeout)

    def _execute_shell(self, command: str, uid: str, timeout: int) -> dict:
        sentinel = f"__ARES_SENTINEL_{uid}__"
        if sys.platform == "win32":
            sentinel_cmd = f"{command}\necho {sentinel}:%ERRORLEVEL%\n"
        else:
            sentinel_cmd = f"{command}\n__ares_status=$?\nprintf '%s:%s\\n' '{sentinel}' \"$__ares_status\"\n"
        with self._lock:
            if not self._write(sentinel_cmd):
                return {"id": uid, "stdout": "", "stderr": "", "error": "REPL crashed and restarted"}
            stdout, stderr, timed_out, exit_code = self._collect_until(self._stdout, sentinel, timeout)
            stderr += self._drain(self._stderr)
            if timed_out:
                self._interrupt()
                return {"id": uid, "stdout": stdout, "stderr": stderr, "error": f"Timeout after {timeout}s", "exit_code": exit_code}
            error = None if exit_code in (None, 0) else f"Command exited with status {exit_code}"
            return {"id": uid, "stdout": stdout, "stderr": stderr, "error": error, "exit_code": exit_code}

    def _write(self, text: str) -> bool:
        try:
            assert self.process is not None and self.process.stdin is not None
            self.process.stdin.write(text)
            self.process.stdin.flush()
            return True
        except (OSError, BrokenPipeError, ValueError):
            self.close()
            self.start()
            return False

    def _read_python_result(self, uid: str, timeout: int) -> dict:
        deadline = time.monotonic() + timeout
        payload = None
        stdout_prefix: list[str] = []
        while time.monotonic() < deadline:
            remaining = max(0.05, deadline - time.monotonic())
            try:
                line = self._stdout.get(timeout=min(0.1, remaining))
            except queue.Empty:
                if not self.alive:
                    break
                continue
            if line is None:
                break
            if f'"id": "{uid}"' in line or f'"id":"{uid}"' in line:
                try:
                    payload = json.loads(line.strip())
                except json.JSONDecodeError:
                    payload = {"id": uid, "stdout": "".join(stdout_prefix), "stderr": "", "error": "Parse error"}
                continue
            if f"__ARES_DONE_{uid}__" in line:
                if payload is not None:
                    payload["stderr"] = (payload.get("stderr") or "") + self._drain(self._stderr)
                    return payload
                break
            stdout_prefix.append(line)

        self._interrupt()
        # Give KeyboardInterrupt handler a chance to emit the sentinel.
        end = time.monotonic() + 2
        while time.monotonic() < end and self.alive:
            try:
                line = self._stdout.get(timeout=0.1)
            except queue.Empty:
                continue
            if line and (f'"id": "{uid}"' in line or f'"id":"{uid}"' in line):
                try:
                    payload = json.loads(line.strip())
                    existing_error = payload.get("error")
                    payload["error"] = (
                        f"Timeout after {timeout}s"
                        + (f"\n{existing_error}" if existing_error else "")
                    )
                    return payload
                except json.JSONDecodeError:
                    pass
        return {"id": uid, "stdout": "".join(stdout_prefix), "stderr": self._drain(self._stderr), "error": f"Timeout after {timeout}s"}

    def _collect_until(self, q: queue.Queue[str | None], sentinel: str, timeout: int) -> tuple[str, str, bool, int | None]:
        deadline = time.monotonic() + timeout
        lines: list[str] = []
        while time.monotonic() < deadline:
            try:
                line = q.get(timeout=min(0.1, max(0.01, deadline - time.monotonic())))
            except queue.Empty:
                if not self.alive:
                    break
                continue
            if line is None:
                break
            if sentinel in line:
                exit_code = None
                marker = line.strip().split(sentinel, 1)[-1].lstrip(":")
                try:
                    exit_code = int(marker.split()[0])
                except (ValueError, IndexError):
                    pass
                return "".join(lines), "", False, exit_code
            lines.append(line)
        return "".join(lines), "", True, None

    @staticmethod
    def _drain(q: queue.Queue[str | None]) -> str:
        lines: list[str] = []
        while True:
            try:
                line = q.get_nowait()
            except queue.Empty:
                break
            if line is not None:
                lines.append(line)
        return "".join(lines)

    def _interrupt(self) -> None:
        if not self.alive or self.process is None:
            return
        try:
            if sys.platform == "win32":
                self.process.terminate()
            else:
                self.process.send_signal(signal.SIGINT)
        except (OSError, ProcessLookupError):
            pass


class PersistentREPL:
    """Manages persistent Python and shell sessions."""

    def __init__(self) -> None:
        self.python_session: REPLSession | None = None
        self.shell_session: REPLSession | None = None
        self._python_lock = threading.Lock()
        self._shell_lock = threading.Lock()

    def execute_python(self, code: str, timeout: int = 30, cwd: str | None = None) -> str:
        """Execute Python code in the persistent session and format its output."""
        with self._python_lock:
            if cwd and self.python_session and self.python_session.cwd != cwd:
                self.python_session.close()
                self.python_session = None
            if not self.python_session or not self.python_session.alive:
                self.python_session = REPLSession("python", cwd=cwd)
                self.python_session.start()
            result = self.python_session.execute(code, timeout=timeout)
        return self._format_result(result)

    def execute_shell(self, command: str, timeout: int = 30, cwd: str | None = None) -> str:
        """Execute a shell command in the persistent session and format its output."""
        with self._shell_lock:
            if cwd and self.shell_session and self.shell_session.cwd != cwd:
                self.shell_session.close()
                self.shell_session = None
            if not self.shell_session or not self.shell_session.alive:
                self.shell_session = REPLSession("shell", cwd=cwd)
                self.shell_session.start()
            result = self.shell_session.execute(command, timeout=timeout)
        return self._format_result(result)

    def close(self) -> None:
        """Terminate all managed sessions."""
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
        if stdout:
            parts.append(stdout)
        if stderr:
            parts.append(f"stderr: {stderr}")
        if error:
            parts.append(f"Error: {error}")
        return "\n".join(parts) if parts else "Executed successfully (no output)"
