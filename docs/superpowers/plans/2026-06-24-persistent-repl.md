# Persistent REPL Execution Engine — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace subprocess-per-call execution with persistent, stateful REPL sessions — variables, imports, and functions survive between `run_code` and `run_command` calls.

**Architecture:** A `REPLSession` class wraps a long-lived Python/bash subprocess. A `PersistentREPL` singleton manages one Python session and one shell session, auto-restarting dead processes. `ToolExecutor` routes existing `run_code`/`run_command` calls to the REPL transparently. No tool interface changes.

**Tech Stack:** Python stdlib only (`subprocess`, `threading`, `json`, `uuid`, `signal`, `time`).

---

## File Map

| File | Action | Purpose |
|------|--------|---------|
| `ares/tools/repl.py` | **Create** | `REPLSession` + `PersistentREPL` classes |
| `ares/tools/executor.py:89-457` | **Modify** | Add `self.repl`, rewrite `_run_code`/`_run_command` to route through REPL |
| `ares/task_executor.py:18-26` | **Modify** | Add `"run_code"` to `ALLOWED_TOOLS` |
| `tests/test_repl.py` | **Create** | Unit tests for `REPLSession` and `PersistentREPL` |
| `tests/test_code_execution.py` | **Modify** | Existing tests must still pass (they test subprocess path) |
| `tests/test_shell_execution.py` | **Modify** | Existing tests must still pass (they test subprocess path) |

---

### Task 1: Write failing tests for REPLSession

**Files:**
- Create: `tests/test_repl.py`

- [ ] **Step 1: Create the test file with REPLSession unit tests**

```python
"""Tests for ares.repl module — persistent REPL sessions."""

import sys
import json
import time
import pytest
from ares.tools.repl import REPLSession, PersistentREPL


class TestREPLSession:
    """Unit tests for REPLSession."""

    def test_start_creates_alive_process(self):
        session = REPLSession("python")
        session.start()
        assert session.alive
        session.close()

    def test_alive_returns_false_after_close(self):
        session = REPLSession("python")
        session.start()
        session.close()
        assert not session.alive

    def test_execute_simple_print(self):
        session = REPLSession("python")
        session.start()
        result = session.execute("print('hello')")
        assert result["stdout"].strip() == "hello"
        assert result["error"] is None
        session.close()

    def test_execute_returns_structured_json(self):
        session = REPLSession("python")
        session.start()
        result = session.execute("print('test')")
        assert isinstance(result, dict)
        assert "id" in result
        assert "stdout" in result
        assert "error" in result
        session.close()

    def test_state_preserved_between_calls(self):
        session = REPLSession("python")
        session.start()
        session.execute("x = 42")
        result = session.execute("print(x)")
        assert result["stdout"].strip() == "42"
        session.close()

    def test_import_persists(self):
        session = REPLSession("python")
        session.start()
        session.execute("import json")
        result = session.execute("print(json.dumps([1,2,3]))")
        assert result["stdout"].strip() == "[1, 2, 3]"
        session.close()

    def test_function_persists(self):
        session = REPLSession("python")
        session.start()
        session.execute("def greet(name): return f'hi {name}'")
        result = session.execute("print(greet('world'))")
        assert result["stdout"].strip() == "hi world"
        session.close()

    def test_syntax_error_stays_alive(self):
        session = REPLSession("python")
        session.start()
        result = session.execute("def def def")
        # Syntax error reported in error field, session still alive
        assert session.alive
        assert result["error"] is not None or "SyntaxError" in result.get("stdout", "")
        session.close()

    def test_exception_stays_alive(self):
        session = REPLSession("python")
        session.start()
        session.execute("1/0")
        # ZeroDivisionError should not kill the REPL
        assert session.alive
        session.close()

    def test_timeout_returns_error(self):
        session = REPLSession("python")
        session.start()
        result = session.execute("import time; time.sleep(60)", timeout=2)
        assert result["error"] is not None or "timeout" in result.get("stdout", "").lower()
        assert session.alive  # REPL itself should survive
        session.close()

    def test_multiple_outputs(self):
        session = REPLSession("python")
        session.start()
        result = session.execute("for i in range(3): print(i)")
        lines = result["stdout"].strip().split("\n")
        assert lines == ["0", "1", "2"]
        session.close()

    def test_stderr_captured(self):
        session = REPLSession("python")
        session.start()
        result = session.execute("import sys; sys.stderr.write('err msg\\n')")
        assert "err msg" in result.get("stderr", "") or "err msg" in result.get("stdout", "")
        session.close()

    def test_execute_shell_simple(self):
        session = REPLSession("shell")
        session.start()
        result = session.execute("echo hello")
        assert "hello" in result["stdout"]
        assert result["error"] is None
        session.close()

    def test_shell_state_preserved(self):
        if sys.platform == "win32":
            pytest.skip("Shell state test not reliable on Windows cmd")
        session = REPLSession("shell")
        session.start()
        session.execute("MYVAR=42")
        result = session.execute("echo $MYVAR")
        assert "42" in result["stdout"]
        session.close()


class TestPersistentREPL:
    """Unit tests for PersistentREPL manager."""

    def test_execute_python_returns_string(self):
        repl = PersistentREPL()
        result = repl.execute_python("print('hi')")
        assert isinstance(result, str)
        assert "hi" in result
        repl.close()

    def test_python_state_preserved(self):
        repl = PersistentREPL()
        repl.execute_python("x = 99")
        result = repl.execute_python("print(x)")
        assert "99" in result
        repl.close()

    def test_execute_shell_returns_string(self):
        repl = PersistentREPL()
        result = repl.execute_shell("echo hello")
        assert isinstance(result, str)
        assert "hello" in result
        repl.close()

    def test_shell_state_preserved(self):
        if sys.platform == "win32":
            pytest.skip("Shell state test not reliable on Windows cmd")
        repl = PersistentREPL()
        repl.execute_shell("MYVAR=hello")
        result = repl.execute_shell("echo $MYVAR")
        assert "hello" in result
        repl.close()

    def test_auto_restart_dead_python(self):
        repl = PersistentREPL()
        repl.execute_python("import os; os._exit(1)")
        # Next call should auto-restart and work
        result = repl.execute_python("print('restarted')")
        assert "restarted" in result
        repl.close()

    def test_shared_state_across_calls(self):
        repl = PersistentREPL()
        repl.execute_python("counter = 0")
        for _ in range(5):
            repl.execute_python("counter += 1")
        result = repl.execute_python("print(counter)")
        assert "5" in result
        repl.close()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_repl.py -v --tb=short 2>&1 | head -40`
Expected: All tests FAIL with `ModuleNotFoundError: No module named 'ares.tools.repl'`

- [ ] **Step 3: Commit the failing tests**

```bash
git add tests/test_repl.py
git commit -m "test: add failing tests for persistent REPL sessions"
```

---

### Task 2: Implement REPLSession class

**Files:**
- Create: `ares/tools/repl.py`

- [ ] **Step 1: Create repl.py with REPLSession and PersistentREPL**

```python
"""Persistent REPL sessions for stateful code execution."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
from uuid import uuid4


class REPLSession:
    """A single persistent subprocess (Python or shell) with sentinel-based framing."""

    def __init__(self, lang: str = "python", cwd: str | None = None):
        self.lang = lang
        self.cwd = cwd
        self.process: subprocess.Popen | None = None
        self._lock = threading.Lock()

    def start(self) -> None:
        """Start the persistent subprocess."""
        if self.lang == "python":
            self.process = subprocess.Popen(
                [sys.executable, "-i", "-q", "-u"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=self.cwd,
                text=True,
                bufsize=1,
                env={**os.environ, "PYTHONIOENCODING": "utf-8"},
            )
        else:
            shell = "bash" if sys.platform != "win32" else "cmd.exe"
            self.process = subprocess.Popen(
                [shell],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=self.cwd,
                text=True,
                bufsize=1,
            )

    def close(self) -> None:
        """Terminate the subprocess gracefully, then force-kill."""
        if self.process is None:
            return
        try:
            self.process.stdin.close()
        except (OSError, ValueError):
            pass
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                pass
        self.process = None

    @property
    def alive(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def ensure_alive(self) -> None:
        """Auto-restart if the process died."""
        if not self.alive:
            self.close()
            self.start()

    def execute(self, code: str, timeout: int = 30) -> dict:
        """Send code to the alive process, return structured output.

        Returns:
            {"id": str, "stdout": str, "stderr": str, "error": str|None}
        """
        self.ensure_alive()
        uid = uuid4().hex[:8]

        if self.lang == "python":
            return self._execute_python(code, uid, timeout)
        else:
            return self._execute_shell(code, uid, timeout)

    def _execute_python(self, code: str, uid: str, timeout: int) -> dict:
        """Execute Python code with sentinel-based output capture."""
        wrapped = (
            "import sys as __sys, json as __json, traceback as __tb\n"
            "__ares_buf = []\n"
            "__ares_err = []\n"
            "__ares_orig_write = __sys.stdout.write\n"
            "__ares_orig_err_write = __sys.stderr.write\n"
            "def __ares_cap_out(s):\n"
            "    __ares_buf.append(s)\n"
            "    __ares_orig_write(s)\n"
            "def __ares_cap_err(s):\n"
            "    __ares_err.append(s)\n"
            "    __ares_orig_err_write(s)\n"
            "__sys.stdout.write = __ares_cap_out\n"
            "__sys.stderr.write = __ares_cap_err\n"
            "try:\n"
            f"    # --- user code start ---\n"
            f"    {code}\n"
            f"    # --- user code end ---\n"
            "except SystemExit:\n"
            "    pass\n"
            "except:\n"
            "    __ares_err.append(__tb.format_exc())\n"
            "__sys.stdout.write = __ares_orig_write\n"
            "__sys.stderr.write = __ares_orig_err_write\n"
            f"__ares_payload = __json.dumps({{\n"
            f'    "id": "{uid}",\n'
            f'    "stdout": "".join(__ares_buf),\n'
            f'    "stderr": "".join(__ares_err),\n'
            f'    "error": None\n'
            f"}})\n"
            f"__sys.stdout.write(__ares_payload + \"\\n\")\n"
        )

        with self._lock:
            try:
                self.process.stdin.write(wrapped + "\n")
                self.process.stdin.flush()
            except (OSError, BrokenPipeError):
                self.close()
                self.start()
                return {"id": uid, "stdout": "", "stderr": "", "error": "REPL crashed and restarted"}

            return self._read_until_sentinel(uid, timeout)

    def _execute_shell(self, command: str, uid: str, timeout: int) -> dict:
        """Execute shell command with sentinel-based output capture."""
        sentinel_cmd = (
            f'{command}\n'
            f'echo "__ARES_SENTINEL_{uid}__"\n'
        )

        with self._lock:
            try:
                self.process.stdin.write(sentinel_cmd)
                self.process.stdin.flush()
            except (OSError, BrokenPipeError):
                self.close()
                self.start()
                return {"id": uid, "stdout": "", "stderr": "", "error": "REPL crashed and restarted"}

            output_lines = []
            start = time.time()
            while time.time() - start < timeout:
                try:
                    line = self.process.stdout.readline()
                except (OSError, ValueError):
                    break
                if not line:
                    break
                sentinel = f"__ARES_SENTINEL_{uid}__"
                if sentinel in line:
                    return {"id": uid, "stdout": "".join(output_lines), "stderr": "", "error": None}
                output_lines.append(line)

            return {"id": uid, "stdout": "".join(output_lines), "stderr": "", "error": "Timeout"}

    def _read_until_sentinel(self, uid: str, timeout: int) -> dict:
        """Read stdout lines until the sentinel JSON appears."""
        output_lines = []
        start = time.time()
        while time.time() - start < timeout:
            try:
                line = self.process.stdout.readline()
            except (OSError, ValueError):
                break
            if not line:
                break
            if uid in line and '"stdout"' in line:
                try:
                    return json.loads(line.strip())
                except json.JSONDecodeError:
                    return {"id": uid, "stdout": "".join(output_lines), "stderr": "", "error": "Parse error"}
            output_lines.append(line)

        # Timeout — try to interrupt the running code
        try:
            self.process.send_signal(signal.SIGINT)
        except (OSError, ProcessLookupError):
            pass

        return {"id": uid, "stdout": "".join(output_lines), "stderr": "", "error": f"Timeout after {timeout}s"}


class PersistentREPL:
    """Manages persistent Python and shell sessions."""

    def __init__(self) -> None:
        self.python_session: REPLSession | None = None
        self.shell_session: REPLSession | None = None
        self._python_lock = threading.Lock()
        self._shell_lock = threading.Lock()

    def execute_python(self, code: str, timeout: int = 30, cwd: str | None = None) -> str:
        """Execute Python code in the persistent session.

        Returns formatted output string for the LLM.
        """
        with self._python_lock:
            # If CWD changed, restart session (Python can't change CWD safely mid-session)
            if cwd and self.python_session and self.python_session.cwd != cwd:
                self.python_session.close()
                self.python_session = None

            if not self.python_session or not self.python_session.alive:
                self.python_session = REPLSession("python", cwd=cwd)
                self.python_session.start()

            result = self.python_session.execute(code, timeout)

        return self._format_result(result)

    def execute_shell(self, command: str, timeout: int = 30, cwd: str | None = None) -> str:
        """Execute shell command in the persistent session.

        Returns formatted output string for the LLM.
        """
        with self._shell_lock:
            if cwd and self.shell_session and self.shell_session.cwd != cwd:
                self.shell_session.close()
                self.shell_session = None

            if not self.shell_session or not self.shell_session.alive:
                self.shell_session = REPLSession("shell", cwd=cwd)
                self.shell_session.start()

            result = self.shell_session.execute(command, timeout)

        return self._format_result(result)

    def close(self) -> None:
        """Terminate both sessions."""
        if self.python_session:
            self.python_session.close()
            self.python_session = None
        if self.shell_session:
            self.shell_session.close()
            self.shell_session = None

    @staticmethod
    def _format_result(result: dict) -> str:
        """Format REPL result into a string for the LLM."""
        parts = []
        stdout = (result.get("stdout") or "").rstrip()
        stderr = (result.get("stderr") or "").rstrip()
        error = result.get("error")

        if stdout:
            parts.append(stdout)
        if stderr:
            parts.append(f"stderr: {stderr}")
        if error:
            parts.append(f"Error: {error}")

        return "\n".join(parts) if parts else "Executed successfully (no output)"
```

- [ ] **Step 2: Run REPL tests to verify they pass**

Run: `python -m pytest tests/test_repl.py -v --tb=short 2>&1 | head -60`
Expected: All tests PASS

- [ ] **Step 3: Commit the REPL implementation**

```bash
git add ares/tools/repl.py
git commit -m "feat: add persistent REPLSession and PersistentREPL classes"
```

---

### Task 3: Wire REPL into ToolExecutor

**Files:**
- Modify: `ares/tools/executor.py:1-51,451-464`

- [ ] **Step 1: Add PersistentREPL import and init to ToolExecutor**

In `ares/tools/executor.py`, add the import after line 32:

```python
from ares.tools.repl import PersistentREPL
```

In the `ToolExecutor.__init__` method (line 38-51), add after `self.task_executor_ref = None`:

```python
        self.repl = PersistentREPL()
```

- [ ] **Step 2: Rewrite `_run_code` to use REPL**

Replace the existing `_run_code` method (lines 453-457) with:

```python
    def _run_code(self, args: dict) -> str:
        code = args["code"]
        timeout = int(args.get("timeout", 30))
        cwd = args.get("cwd")
        return self.repl.execute_python(code, timeout=timeout, cwd=cwd)
```

- [ ] **Step 3: Rewrite `_run_command` to use REPL**

Replace the existing `_run_command` method (lines 459-463) with:

```python
    def _run_command(self, args: dict) -> str:
        command = args["command"]
        timeout = int(args.get("timeout", 30))
        cwd = args.get("cwd")
        return self.repl.execute_shell(command, timeout=timeout, cwd=cwd)
```

- [ ] **Step 4: Update `_terminal_exec` to use REPL instead of direct `run_command`**

Replace the existing `_terminal_exec` method (lines 509-528) with:

```python
    def _terminal_exec(self, args: dict) -> str:
        """Execute a shell command via persistent REPL for reliable output capture.

        Optionally displays the command in the visual terminal panel if connected.
        """
        command = args["command"]
        timeout = int(args.get("timeout", 30))
        cwd = args.get("cwd")

        # Execute via REPL for reliable output capture
        result = self.repl.execute_shell(command, timeout=timeout, cwd=cwd)

        # Also send to visual terminal if connected (best-effort display)
        if hasattr(self, '_terminal_display_callback') and self._terminal_display_callback:
            try:
                self._terminal_display_callback(command)
            except Exception:
                pass  # display is optional

        return result
```

- [ ] **Step 5: Add `repl.close()` support for clean shutdown**

Add this method to `ToolExecutor` after the `__init__`:

```python
    def close(self) -> None:
        """Clean up persistent sessions."""
        self.repl.close()
```

- [ ] **Step 6: Run all existing tests to verify nothing broke**

Run: `python -m pytest tests/test_code_execution.py tests/test_shell_execution.py tests/test_repl.py -v --tb=short 2>&1 | head -80`
Expected: All tests PASS (existing tests call `run_code`/`run_command` directly, not through `ToolExecutor`, so they still test the subprocess path)

- [ ] **Step 7: Commit the ToolExecutor wiring**

```bash
git add ares/tools/executor.py
git commit -m "feat: wire ToolExecutor to use persistent REPL for run_code/run_command"
```

---

### Task 4: Add run_code to task executor ALLOWED_TOOLS

**Files:**
- Modify: `ares/task_executor.py:18-26`

- [ ] **Step 1: Add `run_code` to ALLOWED_TOOLS**

In `ares/task_executor.py`, add `"run_code"` to the `ALLOWED_TOOLS` set (line 18-26):

```python
ALLOWED_TOOLS = {
    "web_search", "fetch_url",
    "read_file", "search_files", "list_directory",
    "glob_pattern", "get_file_info", "head_file", "tail_file",
    "count_lines", "file_tree",
    "search_memory", "store_memory",
    "write_file", "edit_file", "create_directory",
    "run_command",
    "run_code",
}
```

- [ ] **Step 2: Run task executor tests**

Run: `python -m pytest tests/test_task_executor.py -v --tb=short 2>&1 | head -40`
Expected: Tests PASS

- [ ] **Step 3: Commit**

```bash
git add ares/task_executor.py
git commit -m "feat: add run_code to task executor ALLOWED_TOOLS"
```

---

### Task 5: Add integration tests for state preservation end-to-end

**Files:**
- Create: `tests/test_repl_integration.py`

- [ ] **Step 1: Create integration tests**

```python
"""Integration tests for persistent REPL — end-to-end ToolExecutor flow."""

import pytest
from ares.tools.executor import ToolExecutor
from ares.memory import MemoryStore
from ares.tools.tasks import TaskStore
from pathlib import Path


@pytest.fixture
def tool_executor():
    mem = MemoryStore()
    tasks = TaskStore(db_path=Path(":memory:"))
    executor = ToolExecutor(memory_store=mem, task_store=tasks)
    yield executor
    executor.close()


class TestToolExecutorREPL:
    """Integration tests: ToolExecutor → PersistentREPL."""

    def test_run_code_state_preserved(self, tool_executor):
        tool_executor.execute("run_code", {"code": "x = 42"})
        result = tool_executor.execute("run_code", {"code": "print(x)"})
        assert "42" in result

    def test_run_code_import_persists(self, tool_executor):
        tool_executor.execute("run_code", {"code": "import math"})
        result = tool_executor.execute("run_code", {"code": "print(math.pi)"})
        assert "3.14" in result

    def test_run_code_function_persists(self, tool_executor):
        tool_executor.execute("run_code", {"code": "def add(a, b): return a + b"})
        result = tool_executor.execute("run_code", {"code": "print(add(3, 4))"})
        assert "7" in result

    def test_run_code_class_persists(self, tool_executor):
        tool_executor.execute("run_code", {"code": "class Counter:\n    def __init__(self): self.n = 0\n    def inc(self): self.n += 1"})
        tool_executor.execute("run_code", {"code": "c = Counter()"})
        tool_executor.execute("run_code", {"code": "c.inc(); c.inc(); c.inc()"})
        result = tool_executor.execute("run_code", {"code": "print(c.n)"})
        assert "3" in result

    def test_run_command_state_preserved(self, tool_executor):
        result1 = tool_executor.execute("run_command", {"command": "echo hello"})
        assert "hello" in result1
        result2 = tool_executor.execute("run_command", {"command": "echo world"})
        assert "world" in result2

    def test_run_code_syntax_error_stays_alive(self, tool_executor):
        tool_executor.execute("run_code", {"code": "def def def"})
        # REPL should survive — next call should work
        result = tool_executor.execute("run_code", {"code": "print('alive')"})
        assert "alive" in result

    def test_run_code_runtime_error_stays_alive(self, tool_executor):
        tool_executor.execute("run_code", {"code": "1/0"})
        result = tool_executor.execute("run_code", {"code": "print('still here')"})
        assert "still here" in result

    def test_run_code_list_comprehension(self, tool_executor):
        result = tool_executor.execute("run_code", {
            "code": "squares = [x**2 for x in range(5)]\nprint(squares)"
        })
        assert "[0, 1, 4, 9, 16]" in result

    def test_run_code_with_timeout(self, tool_executor):
        result = tool_executor.execute("run_code", {
            "code": "import time; time.sleep(60)",
            "timeout": 2,
        })
        # Should report timeout, not hang
        assert "timeout" in result.lower() or "timed out" in result.lower() or "error" in result.lower()
```

- [ ] **Step 2: Run integration tests**

Run: `python -m pytest tests/test_repl_integration.py -v --tb=short 2>&1 | head -60`
Expected: All tests PASS

- [ ] **Step 3: Commit**

```bash
git add tests/test_repl_integration.py
git commit -m "test: add integration tests for persistent REPL via ToolExecutor"
```

---

### Task 6: Run full test suite and final commit

- [ ] **Step 1: Run the full test suite**

Run: `python -m pytest tests/ -x -q 2>&1 | tail -20`
Expected: All tests pass (or only pre-existing unrelated failures)

- [ ] **Step 2: Final commit if any cleanup needed**

```bash
git add -A
git commit -m "feat: persistent REPL execution engine — state preservation, crash recovery, auto-restart"
```

---

## Success Criteria Checklist

After implementation, verify these work:

1. `run_code("x = 1")` then `run_code("print(x)")` → "1"
2. `run_command("echo hello")` → "hello"
3. REPL survives `sys.exit()` call (SystemExit caught)
4. REPL auto-restarts after `os._exit()` crash
5. Timeout kills execution without killing REPL
6. All existing tests pass
7. No new external dependencies (stdlib only)
