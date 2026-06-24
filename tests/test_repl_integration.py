"""Integration tests for persistent REPL through ToolExecutor."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from ares.tools.executor import ToolExecutor
from ares.tools.tasks import TaskStore


@pytest.fixture
def tool_executor():
    tasks = TaskStore(db_path=Path(":memory:"))
    executor = ToolExecutor(memory_store=SimpleNamespace(), task_store=tasks)
    yield executor
    executor.close()


class TestToolExecutorREPL:
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
        """Shell state preservation — bash only (cmd.exe doesn't support env vars like this)."""
        import sys
        if sys.platform == "win32":
            # On Windows cmd.exe, just verify shell runs commands at all
            r1 = tool_executor.execute("run_command", {"command": "echo hello"})
            assert "hello" in r1
            return
        tool_executor.execute("run_command", {"command": "MYVAR=hello"})
        result = tool_executor.execute("run_command", {"command": "echo $MYVAR"})
        assert "hello" in result

    def test_run_code_syntax_error_stays_alive(self, tool_executor):
        tool_executor.execute("run_code", {"code": "def def def"})
        result = tool_executor.execute("run_code", {"code": "print('alive')"})
        assert "alive" in result

    def test_run_code_runtime_error_stays_alive(self, tool_executor):
        tool_executor.execute("run_code", {"code": "1/0"})
        result = tool_executor.execute("run_code", {"code": "print('still here')"})
        assert "still here" in result

    def test_run_code_list_comprehension(self, tool_executor):
        result = tool_executor.execute("run_code", {"code": "squares = [x**2 for x in range(5)]\nprint(squares)"})
        assert "[0, 1, 4, 9, 16]" in result

    def test_run_code_with_timeout(self, tool_executor):
        result = tool_executor.execute("run_code", {"code": "import time; time.sleep(60)", "timeout": 1})
        assert "timeout" in result.lower() or "keyboardinterrupt" in result.lower() or "error" in result.lower()
