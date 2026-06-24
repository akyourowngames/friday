# Persistent REPL Execution Engine — Design Spec

> **For agentic workers:** This is a design spec, not an implementation plan. After approval, invoke `superpowers:writing-plans` to create the implementation plan.

**Goal:** Upgrade Ares' execution system from subprocess-per-call to persistent REPL sessions, enabling state preservation, faster execution, and streaming output — similar to Open Interpreter and pyreplab.

**Architecture:** Two persistent subprocesses (Python + shell) managed by a `PersistentREPL` singleton. The existing `run_code` and `run_command` tool interfaces remain unchanged — routing is transparent.

**Tech Stack:** Python stdlib (`subprocess`, `threading`, `json`, `uuid`), no new dependencies.

---

## 1. Current State (Problems)

| Problem | Impact |
|---------|--------|
| **No state preservation** | Each `run_code()` call spawns a fresh subprocess. Variables, imports, functions lost between calls. `x = 1` then `print(x)` → `NameError`. |
| **High overhead** | Subprocess startup + teardown for every tool call. ~50-100ms per call. |
| **No streaming** | Output only available after process exits. Long-running scripts block the agent loop. |
| **Limited error context** | Agent sees the error but can't interactively fix and re-run. |
| **Terminal loop broken** | Agent can't iterate on errors like Codex/Open Interpreter. |

## 2. Design Overview

### 2.1 Architecture

```
Agent (LLM with tools)
    → ToolExecutor
        → PersistentREPL.execute_python(code) or .execute_shell(command)
            → REPLSession (alive subprocess)
                → stdin → [Python/bash process, kept alive]
                → stdout → structured JSON output with unique ID sentinel
            → returns structured result {stdout, stderr, error}
        → returns string result to LLM
```

### 2.2 Key Components

| Component | File | Purpose |
|-----------|------|---------|
| `REPLSession` | `ares/tools/repl.py` | Single alive subprocess with stdin/stdout framing |
| `PersistentREPL` | `ares/tools/repl.py` | Manages Python + shell sessions, lifecycle |
| `ToolExecutor._run_code()` | `ares/tools/executor.py` | Routes to REPL instead of spawning fresh process |
| `ToolExecutor._run_command()` | `ares/tools/executor.py` | Routes to shell REPL |
| `ToolExecutor._terminal_exec()` | `ares/tools/executor.py` | Same as `_run_command` + display callback |

### 2.3 Tool Interface (Unchanged)

The LLM sees no difference. These tools work exactly as before:

```python
# run_code still takes the same arguments
{"code": "import pandas as pd\ndf = pd.read_csv('data.csv')\nprint(df.head())", "timeout": 30}

# run_command still takes the same arguments
{"command": "ls -la", "timeout": 30}

# terminal_exec still works the same way
{"command": "python script.py", "wait": true, "timeout": 30}
```

The difference is internal: no subprocess spawn/kill per call.

---

## 3. REPLSession — Persistent Session Design

### 3.1 Lifecycle

```python
class REPLSession:
    def __init__(self, lang: str = "python", cwd: str = None):
        self.lang = lang
        self.process: subprocess.Popen | None = None
        self.cwd = cwd
        self._lock = threading.Lock()  # Prevent concurrent stdin writes
    
    def start(self):
        """Start the persistent subprocess."""
        if self.lang == "python":
            self.process = subprocess.Popen(
                [sys.executable, "-i", "-q", "-u"],
                stdin=PIPE, stdout=PIPE, stderr=PIPE,
                cwd=self.cwd, text=True, bufsize=1,
                env={**os.environ, "PYTHONIOENCODING": "utf-8"}
            )
        elif self.lang == "shell":
            shell = "bash" if sys.platform != "win32" else "cmd.exe"
            self.process = subprocess.Popen(
                [shell],
                stdin=PIPE, stdout=PIPE, stderr=PIPE,
                cwd=self.cwd, text=True, bufsize=1
            )
    
    def execute(self, code: str, timeout: int = 30) -> dict:
        """Send code to alive process, return structured output."""
        # Implementation uses sentinel markers (see 3.2)
    
    def close(self):
        """Terminate the subprocess gracefully, then force-kill if needed."""
        if self.process:
            self.process.stdin.close()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
            self.process = None
    
    @property
    def alive(self) -> bool:
        return self.process is not None and self.process.poll() is None
```

### 3.2 Framing Protocol (Sentinel Markers)

Each `execute()` call wraps code with a unique sentinel to know when output ends:

```python
def execute(self, code: str, timeout: int = 30) -> dict:
    uid = uuid4().hex[:8]
    
    # Wrap code to capture output
    wrapped = f"""
import sys as __sys, json as __json
__ares_buf = []
__ares_orig_write = __sys.stdout.write
def __ares_capture(s):
    __ares_buf.append(s)
    __ares_orig_write(s)
__sys.stdout.write = __ares_capture

# --- user code start ---
{code}
# --- user code end ---

__sys.stdout.write = __ares_orig_write
__ares_result = __json.dumps({{
    "id": "{uid}",
    "stdout": "".join(__ares_buf),
    "error": None
}})
__sys.stdout.write(__ares_result + "\\n")
"""
    
    with self._lock:
        self.process.stdin.write(wrapped + "\n")
        self.process.stdin.flush()
        
        # Read until sentinel appears
        output_lines = []
        start = time.time()
        while time.time() - start < timeout:
            line = self.process.stdout.readline()
            if not line:
                break
            # Check if this is the sentinel line
            if f'"id": "{uid}"' in line or f'"id":"{uid}"' in line:
                try:
                    return json.loads(line.strip())
                except json.JSONDecodeError:
                    return {"id": uid, "stdout": "".join(output_lines), "error": "Parse error"}
            output_lines.append(line)
        
        return {"id": uid, "stdout": "".join(output_lines), "error": "Timeout"}
```

### 3.3 Crash Recovery

```python
def ensure_alive(self):
    """Auto-restart if process died."""
    if not self.alive:
        self.close()
        self.start()
```

If the Python process dies (segfault, `os._exit()`, OOM), the next `execute()` call auto-restarts it. State from before the crash is lost, but the agent can recover.

### 3.4 Timeout Handling

```python
def execute_with_timeout(self, code: str, timeout: int = 30) -> dict:
    result = {"error": None}
    
    def _execute():
        nonlocal result
        result = self.execute(code, timeout)
    
    thread = threading.Thread(target=_execute)
    thread.start()
    thread.join(timeout + 2)  # Extra 2s buffer
    
    if thread.is_alive():
        # Timeout — send interrupt to the subprocess
        try:
            self.process.send_signal(signal.SIGINT)
        except (OSError, ProcessLookupError):
            pass
        return {"stdout": "", "error": f"Timeout after {timeout}s"}
    
    return result
```

### 3.5 Error Handling

| Case | Handling |
|------|----------|
| `sys.exit()` | `SystemExit` caught, REPL auto-restarts |
| `os._exit()` | Process dies, `alive` returns False, auto-restart |
| Infinite loop | Timeout kills execution, REPL stays alive |
| `input()` call | Not supported — returns error message |
| Syntax error | Stderr captured, REPL stays alive |
| Import error | Stderr captured, REPL stays alive |
| Unicode output | `PYTHONIOENCODING=utf-8` ensures clean encoding |

---

## 4. PersistentREPL — Session Manager

```python
class PersistentREPL:
    """Manages persistent Python and shell sessions."""
    
    def __init__(self):
        self.python_session: REPLSession | None = None
        self.shell_session: REPLSession | None = None
        self._python_lock = threading.Lock()
        self._shell_lock = threading.Lock()
    
    def execute_python(self, code: str, timeout: int = 30) -> str:
        """Execute Python code in the persistent session."""
        with self._python_lock:
            if not self.python_session or not self.python_session.alive:
                self.python_session = REPLSession("python")
                self.python_session.start()
            
            result = self.python_session.execute(code, timeout)
            
            # Format output
            parts = []
            if result.get("stdout"):
                parts.append(result["stdout"])
            if result.get("stderr"):
                parts.append(f"stderr: {result['stderr']}")
            if result.get("error"):
                parts.append(f"Error: {result['error']}")
            
            return "\n".join(parts) if parts else "Executed successfully (no output)"
    
    def execute_shell(self, command: str, timeout: int = 30) -> str:
        """Execute shell command in the persistent session."""
        with self._shell_lock:
            if not self.shell_session or not self.shell_session.alive:
                self.shell_session = REPLSession("shell")
                self.shell_session.start()
            
            result = self.shell_session.execute(command, timeout)
            
            parts = []
            if result.get("stdout"):
                parts.append(result["stdout"])
            if result.get("stderr"):
                parts.append(f"stderr: {result['stderr']}")
            if result.get("error"):
                parts.append(f"Error: {result['error']}")
            
            return "\n".join(parts) if parts else "Executed successfully (no output)"
    
    def close(self):
        """Terminate both sessions."""
        if self.python_session:
            self.python_session.close()
        if self.shell_session:
            self.shell_session.close()
```

---

## 5. ToolExecutor Integration

### 5.1 Wiring

```python
class ToolExecutor:
    def __init__(self, ...):
        # ... existing init ...
        self.repl = PersistentREPL()  # New
    
    def _run_code(self, args: dict) -> str:
        code = args["code"]
        timeout = int(args.get("timeout", 30))
        cwd = args.get("cwd")
        
        # Set CWD if specified
        if cwd and self.repl.python_session:
            # Restart session in new CWD
            self.repl.python_session.close()
            self.repl.python_session = None
        
        return self.repl.execute_python(code, timeout)
    
    def _run_command(self, args: dict) -> str:
        command = args["command"]
        timeout = int(args.get("timeout", 30))
        cwd = args.get("cwd")
        
        if cwd and self.repl.shell_session:
            self.repl.shell_session.close()
            self.repl.shell_session = None
        
        return self.repl.execute_shell(command, timeout)
```

### 5.2 Shared State

The REPL is shared across all tool calls in the same `ToolExecutor` instance. This means:

- Main agent and background tasks share the same Python session
- Variables set by one call persist to the next
- This is intentional — mirrors how a real REPL works

**CWD changes reset state:** When `run_code(code, cwd="/some/dir")` is called with a different working directory, the Python session must be restarted (Python can't change CWD mid-session without side effects). This loses accumulated state. To preserve state, avoid changing CWD between calls.

**If isolation is needed:** Background tasks could get their own REPL instance via `TaskExecutor`. This is a future enhancement, not part of this spec.

---

## 6. Streaming Output

For real-time output during long-running scripts:

```python
def execute_streaming(self, code: str, timeout: int = 30):
    """Generator that yields output lines as they appear."""
    uid = uuid4().hex[:8]
    wrapped = f"""
import sys as __sys, json as __json
# ... same wrapping as execute() ...
"""
    
    with self._lock:
        self.process.stdin.write(wrapped + "\n")
        self.process.stdin.flush()
        
        while True:
            line = self.process.stdout.readline()
            if not line:
                break
            if f'"id": "{uid}"' in line:
                break
            yield line  # Stream to caller
```

The agent's `run_stream()` can display these lines as tool output tokens, giving real-time feedback.

---

## 7. Background Tasks

Background tasks (auto-executable via `TaskExecutor`) use the same `PersistentREPL`:

1. `TaskExecutor._execute_step()` calls `self.tool_executor.execute("run_code", {...})`
2. `ToolExecutor._run_code()` routes to `PersistentREPL.execute_python()`
3. State is shared with the main agent session

**Shared state implication:** If the main agent runs `x = 5`, a background task can access `x`. This is by design — all code runs in the same Python namespace.

---

## 8. Testing Strategy

| Test Type | What to Test |
|-----------|-------------|
| **Unit: REPLSession** | `start()`, `execute()`, `close()`, `alive` property |
| **Unit: crash recovery** | Kill the process, verify `alive=False`, verify auto-restart |
| **Unit: timeout** | Verify execution stops after timeout, REPL stays alive |
| **Unit: sentinel framing** | Verify unique IDs prevent stale output reads |
| **Integration: run_code** | `ToolExecutor.run_code()` → REPL → output verification |
| **Integration: state** | `run_code("x = 1")` → `run_code("print(x)")` → "1" |
| **Integration: run_command** | `run_command("echo hello")` → "hello\n" |
| **Edge: sys.exit** | Verify REPL survives `sys.exit()` call |
| **Edge: syntax error** | Verify REPL stays alive after syntax error |
| **Edge: infinite loop** | Verify timeout kills execution, REPL stays alive |

---

## 9. Migration Path

1. **Phase 1:** Create `ares/tools/repl.py` with `REPLSession` and `PersistentREPL`
2. **Phase 2:** Update `ToolExecutor` to use REPL for `run_code` and `run_command`
3. **Phase 3:** Update `ALLOWED_TOOLS` to include `run_code` for background tasks
4. **Phase 4:** Add streaming support for real-time output
5. **Phase 5:** Add tests and documentation

---

## 10. Risks and Mitigations

| Risk | Impact | Mitigation |
|------|--------|------------|
| State pollution | Variables from one call affect another | Accept as feature; document clearly |
| Memory leak | Long-running REPL accumulates state | Add periodic `gc.collect()` or session restart |
| Deadlock | stdin/stdout buffer fills up | Use `bufsize=1` (line buffered), flush after each write |
| Process crash | REPL dies unexpectedly | Auto-restart on next call |
| Concurrent access | Multiple threads write to same REPL | Use `threading.Lock` per session |

---

## 11. Success Criteria

1. ✅ `run_code("x = 1")` then `run_code("print(x)")` → "1"
2. ✅ `run_command("echo hello")` → "hello\n"
3. ✅ REPL survives `sys.exit()` call
4. ✅ REPL auto-restarts after process crash
5. ✅ Timeout kills execution without killing REPL
6. ✅ Streaming output works for long-running scripts
7. ✅ All existing tests pass
8. ✅ No new external dependencies (stdlib only)

---

## 12. References

- [pyreplab](https://github.com/protostatis/pyreplab) — Persistent Python REPL for LLM CLI tools
- [Open Interpreter](https://github.com/OpenInterpreter/open-interpreter) — Terminal-based AI code interpreter
- [subprocess documentation](https://docs.python.org/3/library/subprocess.html) — Python subprocess management
