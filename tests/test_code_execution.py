"""Tests for ares.code_execution module."""

import sys
import pytest
from ares.code_execution import run_code


class TestRunCode:
    """Tests for the run_code function."""

    def test_simple_print(self):
        result = run_code("print('hello world')")
        assert "Exit code: 0" in result
        assert "hello world" in result

    def test_stderr_output(self):
        code = "import sys; sys.stderr.write('error msg\\n')"
        result = run_code(code)
        assert "Exit code: 0" in result
        assert "error msg" in result

    def test_nonzero_exit_code(self):
        result = run_code("import sys; sys.exit(1)", timeout=10)
        assert "Exit code: 1" in result

    def test_import_stdlib(self):
        result = run_code("import json; print(json.dumps({'a': 1}))")
        assert "Exit code: 0" in result
        assert '{"a": 1}' in result

    def test_timeout_kills_code(self):
        result = run_code("import time; time.sleep(60)", timeout=2)
        assert "timed out" in result.lower() or "timeout" in result.lower()

    def test_empty_output(self):
        result = run_code("x = 1")
        assert "Exit code: 0" in result
        assert "No output" in result

    def test_multiline_code(self):
        code = """
for i in range(3):
    print(f"line {i}")
"""
        result = run_code(code)
        assert "line 0" in result
        assert "line 1" in result
        assert "line 2" in result

    def test_cwd_parameter(self, tmp_path):
        result = run_code("import os; print(os.getcwd())", cwd=str(tmp_path))
        assert str(tmp_path) in result

    def test_timeout_clamped_to_max(self):
        result = run_code("print('ok')", timeout=999)
        assert "Exit code: 0" in result

    def test_timeout_clamped_to_min(self):
        result = run_code("print('ok')", timeout=0)
        assert "Exit code: 0" in result

    def test_syntax_error_returns_stderr(self):
        result = run_code("def def def")
        assert "Exit code:" in result
