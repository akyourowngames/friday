"""Tests for ares.shell_execution module."""

import sys
import pytest
from ares.shell_execution import run_command


class TestRunCommand:
    """Tests for the run_command function."""

    def test_simple_command(self):
        result = run_command("echo hello")
        assert "Exit code: 0" in result
        assert "hello" in result

    def test_stderr_output(self):
        if sys.platform == "win32":
            result = run_command("python -c \"import sys; sys.stderr.write('err\\n')\"")
        else:
            result = run_command("python3 -c \"import sys; sys.stderr.write('err\\n')\"")
        assert "Exit code:" in result
        assert "err" in result

    def test_nonzero_exit_code(self):
        if sys.platform == "win32":
            result = run_command("python -c \"import sys; sys.exit(42)\"")
        else:
            result = run_command("python3 -c \"import sys; sys.exit(42)\"")
        assert "Exit code: 42" in result

    def test_pipe_operator(self):
        if sys.platform == "win32":
            result = run_command("echo hello | findstr hello")
        else:
            result = run_command("echo hello | grep hello")
        assert "hello" in result

    def test_timeout_kills_command(self):
        if sys.platform == "win32":
            result = run_command("python -c \"import time; time.sleep(60)\"", timeout=2)
        else:
            result = run_command("sleep 60", timeout=2)
        assert "timed out" in result.lower() or "timeout" in result.lower()

    def test_output_cap(self):
        if sys.platform == "win32":
            result = run_command("python -c \"for i in range(600): print('x' * 100)\"", timeout=30)
        else:
            result = run_command("python3 -c \"for i in range(600): print('x' * 100)\"", timeout=30)
        assert "truncated" in result.lower()

    def test_cwd_parameter(self, tmp_path):
        if sys.platform == "win32":
            result = run_command("cd", cwd=str(tmp_path))
        else:
            result = run_command("pwd", cwd=str(tmp_path))
        assert str(tmp_path) in result

    def test_timeout_clamped(self):
        result = run_command("echo ok", timeout=999)
        assert "Exit code: 0" in result

    def test_empty_output(self):
        if sys.platform == "win32":
            result = run_command("python -c \"pass\"")
        else:
            result = run_command("python3 -c \"pass\"")
        assert "Exit code: 0" in result
        assert "No output" in result
