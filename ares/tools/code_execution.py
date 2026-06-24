"""Python code execution in isolated subprocess."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile


def run_code(code: str, timeout: int = 30, cwd: str | None = None) -> str:
    """Execute Python code in an isolated subprocess.

    Args:
        code: Python code to execute.
        timeout: Max seconds before kill (1-300, default 30).
        cwd: Working directory for the subprocess.

    Returns:
        Formatted string with exit code and output.
    """
    timeout = max(1, min(timeout, 300))

    temp_fd, temp_path = tempfile.mkstemp(suffix=".py", text=True)
    try:
        with os.fdopen(temp_fd, "w", encoding="utf-8") as f:
            f.write(code)

        result = subprocess.run(
            [sys.executable, "-u", temp_path],
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=cwd,
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        )

        output_parts = []
        if result.stdout and result.stdout.rstrip():
            output_parts.append(f"--- stdout ---\n{result.stdout.rstrip()}")
        if result.stderr and result.stderr.rstrip():
            output_parts.append(f"--- stderr ---\n{result.stderr.rstrip()}")

        if not output_parts:
            return f"Exit code: {result.returncode}\n(No output)"

        return f"Exit code: {result.returncode}\n" + "\n".join(output_parts)

    except subprocess.TimeoutExpired:
        return f"Error: Code execution timed out after {timeout}s"
    except Exception as e:
        return f"Error: {e}"
    finally:
        try:
            os.unlink(temp_path)
        except OSError:
            pass
