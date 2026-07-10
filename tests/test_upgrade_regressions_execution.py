"""Execution and MCP regressions from the existing-tools audit."""
from __future__ import annotations

import asyncio
import sys
from types import SimpleNamespace

from ares.tools.executor import ToolExecutor
from ares.tools.mcp_client import MCPClientManager
from ares.tools.repl import PersistentREPL


def test_python_repl_captures_child_binary_unicode_and_recovers_after_timeout():
    repl = PersistentREPL()
    try:
        result = repl.execute_python(
            "import subprocess, sys; "
            "subprocess.run([sys.executable, '-c', \"print('child 12345')\"]); "
            "sys.stdout.buffer.write('buffer ✓\\n'.encode('utf-8'))"
        )
        assert "child 12345" in result
        assert "buffer ✓" in result
        assert "Error:" not in result
        timed_out = repl.execute_python("import time; time.sleep(30)", timeout=1)
        assert "Timeout after 1s" in timed_out
        assert "recovered" in repl.execute_python("print('recovered')")
    finally:
        repl.close()


def test_windows_shell_persists_env_cwd_reset_and_terminal_display_status(tmp_path):
    repl = PersistentREPL()
    try:
        if sys.platform == "win32":
            repl.execute_shell("set ARES_PERSIST_CHECK=present")
            assert "present" in repl.execute_shell("echo %ARES_PERSIST_CHECK%")
            repl.execute_shell(f'cd /d "{tmp_path}"')
            assert str(tmp_path).casefold() in repl.execute_shell("cd").casefold()
        else:
            repl.execute_shell("export ARES_PERSIST_CHECK=present")
            assert "present" in repl.execute_shell("echo $ARES_PERSIST_CHECK")
            repl.execute_shell(f"cd '{tmp_path}'")
            assert str(tmp_path) in repl.execute_shell("pwd")
        repl.reset_shell()
        cleared = repl.execute_shell("echo %ARES_PERSIST_CHECK%" if sys.platform == "win32" else "echo $ARES_PERSIST_CHECK")
        assert "present" not in cleared
    finally:
        repl.close()

    executor = ToolExecutor(memory_store=SimpleNamespace())
    delivered: list[str] = []
    executor._terminal_display_callback = delivered.append
    try:
        result = executor.execute("terminal_exec", {"command": "echo terminal-evidence"})
        assert "terminal-evidence" in result
        assert "Display delivery: delivered" in result
        assert delivered == ["echo terminal-evidence"]
        executor._terminal_display_callback = lambda command: (_ for _ in ()).throw(RuntimeError("panel disconnected"))
        nonfatal = executor.execute("terminal_exec", {"command": "echo still-successful"})
        assert "still-successful" in nonfatal
        assert "Display delivery: failed (non-fatal)" in nonfatal
    finally:
        executor.close()


def test_mcp_is_error_and_health_failure_are_explicit_and_not_ready():
    manager = MCPClientManager([{"name": "demo", "server_url": "https://example.test/mcp", "timeout_seconds": 1}])
    manager.sessions["demo"] = SimpleNamespace(
        call_tool=lambda *args, **kwargs: None,
        list_tools=lambda: None,
    )

    class ErrorSession:
        async def call_tool(self, *args, **kwargs):
            return SimpleNamespace(isError=True, content=[SimpleNamespace(text="server rejected request")])

        async def list_tools(self):
            raise RuntimeError("probe failed")

    manager.sessions["demo"] = ErrorSession()
    error = asyncio.run(manager.call_tool("mcp__demo__act", {}))
    assert error.startswith("Error:")
    assert "server rejected request" in error
    report = asyncio.run(manager.health_probe())
    assert report["servers"]["demo"]["ready"] is False
    assert report["servers"]["demo"]["status"] == "degraded"
    assert "probe failed" in report["servers"]["demo"]["error"]
