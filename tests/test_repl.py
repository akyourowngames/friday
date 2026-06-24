"""Tests for persistent REPL sessions."""

import sys

import pytest

from ares.tools.repl import PersistentREPL, REPLSession


class TestREPLSession:
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
        assert "stderr" in result
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
        assert session.alive
        assert result["error"] is not None
        assert "SyntaxError" in result["error"]
        session.close()

    def test_exception_stays_alive(self):
        session = REPLSession("python")
        session.start()
        result = session.execute("1/0")
        assert session.alive
        assert "ZeroDivisionError" in result["error"]
        session.close()

    def test_timeout_returns_error(self):
        session = REPLSession("python")
        session.start()
        result = session.execute("import time; time.sleep(60)", timeout=1)
        assert result["error"] is not None
        assert "timeout" in result["error"].lower() or "KeyboardInterrupt" in result["error"]
        assert session.alive
        session.close()

    def test_multiple_outputs(self):
        session = REPLSession("python")
        session.start()
        result = session.execute("for i in range(3): print(i)")
        assert result["stdout"].strip().split("\n") == ["0", "1", "2"]
        session.close()

    def test_stderr_captured(self):
        session = REPLSession("python")
        session.start()
        result = session.execute("import sys; sys.stderr.write('err msg\\n')")
        assert "err msg" in result.get("stderr", "")
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
