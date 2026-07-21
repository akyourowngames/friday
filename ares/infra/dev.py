"""Dev launcher: run ``python -m ares --all`` with hot-reload on file changes.

This is a zero-dependency watchdog for local development.  It spawns the real
Ares runtime as a child process, watches the ``ares/`` source tree (and
``pyproject.toml``) for ``.py`` edits, and restarts the child whenever anything
changes.  Restart is a clean terminate-and-respawn so the asyncio loop shuts
down instead of leaking sockets or browser sessions.

Usage:
    python -m ares.dev                 # equivalent to --all, auto-reload on edit
    python -m ares.dev --all           # explicit; extra flags pass through
    python -m ares.dev --watch ../ares --port 9000
    python -m ares.dev --no-telegram   # drop flags you don't want reloaded

Only ``.py`` files are watched (and ``pyproject.toml``).  Virtualenvs, build
artefacts, and ``__pycache__`` are ignored.  Poll interval is 0.4s so edits are
picked up almost immediately without a filesystem-event dependency.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

POLL_INTERVAL_SECONDS = 0.4
SETTLE_SECONDS = 0.25  # debounce bursts of saves from editors
RESTART_GRACE_SECONDS = 8.0

_IGNORE_DIRS = {
    "__pycache__",
    ".venv",
    "venv",
    ".git",
    "node_modules",
    ".pytest_cache",
    ".mypy_cache",
    "build",
    "dist",
}


def _source_roots(watch: Path) -> list[Path]:
    """Return the directories to scan, skipping ignored trees.

    Includes the watched directory itself (for loose ``.py``/``.toml`` files at
    its top level) plus each non-ignored subdirectory.
    """
    if watch.is_file():
        return [watch.parent]
    roots: list[Path] = [watch]
    for entry in sorted(watch.iterdir()):
        if entry.name in _IGNORE_DIRS:
            continue
        if entry.is_dir():
            roots.append(entry)
    return roots or [watch]


def _snapshot(roots: list[Path]) -> dict[Path, float]:
    """Map every watched source file to its mtime."""
    snap: dict[Path, float] = {}
    for root in roots:
        try:
            entries = list(root.rglob("*"))
        except OSError:
            continue
        for path in entries:
            if path.is_dir():
                continue
            if path.suffix not in {".py", ".toml"}:
                continue
            if any(part in _IGNORE_DIRS for part in path.parts):
                continue
            try:
                snap[path] = path.stat().st_mtime
            except OSError:
                continue
    return snap


def _changed(previous: dict[Path, float], roots: list[Path]) -> str | None:
    """Return a description of the first change since ``previous``, else None."""
    current = _snapshot(roots)
    if len(current) != len(previous):
        # Added or removed a file; treat as a change.
        added = set(current) - set(previous)
        removed = set(previous) - set(current)
        if added:
            return f"added {sorted(added)[0].name}"
        if removed:
            return f"removed {sorted(removed)[0].name}"
    for path, mtime in current.items():
        if previous.get(path) != mtime:
            return f"edited {path.name}"
    return None


def _start_child(cmd: list[str]) -> subprocess.Popen[bytes]:
    return subprocess.Popen(cmd)


def _stop_child(proc: subprocess.Popen[bytes]) -> None:
    if proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=RESTART_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        proc.kill()
        try:
            proc.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            pass


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="python -m ares.dev",
        description="Run Ares with hot-reload on source changes (dev only).",
    )
    parser.add_argument(
        "--watch",
        default=None,
        help="Directory or file to watch (default: the ares package next to this file).",
    )
    parser.add_argument(
        "--poll",
        type=float,
        default=POLL_INTERVAL_SECONDS,
        help="File-poll interval in seconds.",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run once without watching (same as python -m ares --all).",
    )
    # Everything after the dev flags is forwarded to the real runtime.
    args, forward = parser.parse_known_args()

    here = Path(__file__).resolve().parent
    watch_target = Path(args.watch).resolve() if args.watch else here
    roots = _source_roots(watch_target)

    cmd = [sys.executable, "-m", "ares", *forward]
    if "--all" not in forward and "--server" not in forward and "--telegram" not in forward:
        cmd.append("--all")

    print(f"[ares.dev] watching {watch_target} (poll {args.poll}s)")
    print(f"[ares.dev] launching: {' '.join(cmd)}")

    if args.once:
        proc = _start_child(cmd)
        try:
            proc.wait()
        finally:
            _stop_child(proc)
        return

    proc = _start_child(cmd)
    snapshot = _snapshot(roots)
    # Keep the child's stdout/stderr flowing to the parent terminal.
    try:
        last_change: str | None = None
        quiet_until = 0.0
        while True:
            time.sleep(args.poll)
            change = _changed(snapshot, roots)
            if change is None:
                continue
            now = time.monotonic()
            if now < quiet_until:
                # Still debouncing a burst of saves.
                continue
            quiet_until = now + SETTLE_SECONDS
            last_change = change
            print(f"\n[ares.dev] change detected ({change}) — restarting Ares...")
            _stop_child(proc)
            proc = _start_child(cmd)
            snapshot = _snapshot(roots)
            print(f"[ares.dev] restarted.")
    except KeyboardInterrupt:
        print("\n[ares.dev] stopping (Ctrl-C)...")
    finally:
        _stop_child(proc)


if __name__ == "__main__":
    main()
