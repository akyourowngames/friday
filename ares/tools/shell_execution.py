"""Shell command execution with output capture."""

from __future__ import annotations

import os
import signal
import subprocess
import sys


def run_command(command: str, timeout: int = 30, cwd: str | None = None) -> str:
    """Execute a shell command with output capture.

    Args:
        command: Shell command to execute.
        timeout: Max seconds before kill (1-300, default 30).
        cwd: Working directory for the command.

    Returns:
        Formatted string with exit code and output.
    """
    timeout = max(1, min(timeout, 300))
    command = command.replace("~", os.path.expanduser("~"))

    kwargs = {
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "text": True,
        "cwd": cwd,
        "bufsize": 1,
    }

    if sys.platform != "win32":
        kwargs["start_new_session"] = True

    try:
        proc = subprocess.Popen(command, shell=True, **kwargs)

        try:
            stdout, stderr = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            if sys.platform != "win32":
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                    proc.wait(timeout=2)
                except (ProcessLookupError, subprocess.TimeoutExpired):
                    try:
                        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                    except ProcessLookupError:
                        pass
            else:
                proc.kill()

            stdout, stderr = proc.communicate()
            partial = stdout.rstrip() if stdout else ""
            return (
                f"Error: Command timed out after {timeout}s\n{partial}"
                if partial
                else f"Error: Command timed out after {timeout}s"
            )

        output_parts = []
        if stdout and stdout.strip():
            output_parts.append(f"--- stdout ---\n{stdout.rstrip()}")
        if stderr and stderr.strip():
            output_parts.append(f"--- stderr ---\n{stderr.rstrip()}")

        if not output_parts:
            return f"Exit code: {proc.returncode}\n(No output)"

        result = f"Exit code: {proc.returncode}\n" + "\n".join(output_parts)

        max_chars = 50_000
        if len(result) > max_chars:
            result = result[:max_chars] + f"\n... (output truncated at {max_chars} chars)"

        return result

    except Exception as e:
        return f"Error running command: {e}"
