"""Small runtime helpers kept separate from terminal presentation."""

import asyncio
import sys
from pathlib import Path


def history_path() -> str:
    """Return an expanded prompt-history path and ensure its parent exists."""
    path = Path("~/.ares_history").expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    return str(path)


def supports_unicode_output() -> bool:
    """Return whether stdout is likely to handle emoji and symbols."""
    return "utf" in (sys.stdout.encoding or "").lower()


def clear_current_task_cancellation() -> None:
    """Clear cancellation state after an intentionally handled cancellation."""
    current_task = asyncio.current_task()
    if current_task is None or not hasattr(current_task, "uncancel"):
        return
    while current_task.cancelling():
        current_task.uncancel()
