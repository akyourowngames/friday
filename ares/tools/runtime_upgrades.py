"""Named execution sessions, checkpoints, artifacts, and owned background jobs."""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import uuid4

from ares.tools.repl import PersistentREPL


TRANSIENT_PATTERNS = (
    "temporarily unavailable", "connection reset", "connection timed out", "timed out",
    "too many requests", "rate limit", "try again", "eai_again",
)


def _snapshot_files(cwd: str | None) -> dict[str, tuple[int, int]]:
    root = Path(cwd or os.getcwd()).expanduser().resolve()
    output: dict[str, tuple[int, int]] = {}
    try:
        for path in root.iterdir():
            if path.is_file():
                stat = path.stat()
                output[str(path)] = (stat.st_size, stat.st_mtime_ns)
    except OSError:
        pass
    return output


def _artifacts(before: dict[str, tuple[int, int]], after: dict[str, tuple[int, int]]) -> list[dict[str, Any]]:
    output = []
    for path, state in after.items():
        if before.get(path) != state:
            output.append({"path": path, "size": state[0], "kind": "created" if path not in before else "modified"})
    return output[:50]


def _parse_execution(text: str) -> dict[str, Any]:
    exit_match = re.search(r"Exit code:\s*(-?\d+)", text)
    passed = re.search(r"(\d+)\s+passed", text)
    failed = re.search(r"(\d+)\s+failed", text)
    warnings = re.search(r"(\d+)\s+warnings?", text)
    return {
        "exit_code": int(exit_match.group(1)) if exit_match else None,
        "tests": {
            "passed": int(passed.group(1)) if passed else 0,
            "failed": int(failed.group(1)) if failed else 0,
            "warnings": int(warnings.group(1)) if warnings else 0,
        },
        "classified_transient": any(pattern in text.casefold() for pattern in TRANSIENT_PATTERNS),
    }


@dataclass
class RuntimeSession:
    repl: PersistentREPL
    python_history: list[dict[str, Any]] = field(default_factory=list)
    shell_history: list[dict[str, Any]] = field(default_factory=list)
    checkpoints: dict[str, dict[str, Any]] = field(default_factory=dict)


@dataclass
class OwnedJob:
    job_id: str
    command: str
    cwd: str
    process: subprocess.Popen[str]
    stdout_path: Path
    stderr_path: Path
    stdout_handle: Any
    stderr_handle: Any
    created_at: float
    stopped_by_user: bool = False

    def payload(self, *, include_output: bool = True, tail_chars: int = 100_000) -> dict[str, Any]:
        code = self.process.poll()
        status = "running" if code is None else ("stopped" if self.stopped_by_user else ("succeeded" if code == 0 else "failed"))
        payload: dict[str, Any] = {
            "job_id": self.job_id, "command": self.command, "cwd": self.cwd,
            "pid": self.process.pid, "status": status, "exit_code": code,
            "created_at": self.created_at, "duration_seconds": round(time.time() - self.created_at, 3),
        }
        if include_output:
            for label, path in (("stdout", self.stdout_path), ("stderr", self.stderr_path)):
                try:
                    text = path.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    text = ""
                payload[label] = text[-tail_chars:]
        return payload


class RuntimeUpgradeManager:
    """Own named REPL sessions and processes started through upgraded modes."""

    def __init__(self, default_repl: PersistentREPL, data_dir: str | Path | None = None):
        self.default_repl = default_repl
        self.sessions: dict[str, RuntimeSession] = {"default": RuntimeSession(default_repl)}
        self.jobs: dict[str, OwnedJob] = {}
        self.data_dir = Path(data_dir or "~/.ares/runtime").expanduser().resolve()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    def _session(self, session_id: str | None) -> RuntimeSession:
        key = str(session_id or "default").strip() or "default"
        with self._lock:
            if key not in self.sessions:
                self.sessions[key] = RuntimeSession(PersistentREPL())
            return self.sessions[key]

    def close(self) -> None:
        with self._lock:
            for key, session in list(self.sessions.items()):
                if key != "default":
                    session.repl.close()
            for job in self.jobs.values():
                if job.process.poll() is None:
                    self._stop_job(job)
                job.stdout_handle.close()
                job.stderr_handle.close()

    def python(self, args: dict[str, Any]) -> dict[str, Any]:
        session_id = str(args.get("session_id") or "default")
        session = self._session(session_id)
        mode = str(args.get("mode") or "execute").casefold()
        cwd = str(args.get("cwd") or "") or None
        if mode == "history":
            return {"session_id": session_id, "mode": mode, "history": list(session.python_history)}
        if mode == "variables":
            marker = f"__ARES_VARS_{uuid4().hex}__"
            code = (
                "import json as __ares_json\n"
                "__ares_values = {k: {'type': type(v).__name__, 'repr': repr(v)[:500]} "
                "for k, v in globals().items() if not k.startswith('__')}\n"
                f"print('{marker}' + __ares_json.dumps(__ares_values, ensure_ascii=False))"
            )
            raw = session.repl.execute_python(code, timeout=int(args.get("timeout", 30)), cwd=cwd)
            match = re.search(re.escape(marker) + r"(\{.*\})", raw)
            variables = json.loads(match.group(1)) if match else {}
            return {"session_id": session_id, "mode": mode, "variables": variables, "raw": raw if not match else ""}
        if mode == "packages":
            code = "import importlib.metadata as m; print('\\n'.join(sorted(f'{d.metadata[\"Name\"]}=={d.version}' for d in m.distributions() if d.metadata.get(\"Name\"))))"
            raw = session.repl.execute_python(code, timeout=int(args.get("timeout", 30)), cwd=cwd)
            return {"session_id": session_id, "mode": mode, "packages": raw.splitlines(), "raw": raw}
        if mode == "checkpoint":
            checkpoint_id = str(args.get("checkpoint_id") or uuid4().hex[:12])
            session.checkpoints[checkpoint_id] = {"python_history_length": len(session.python_history), "created_at": time.time()}
            return {"session_id": session_id, "mode": mode, "checkpoint_id": checkpoint_id, **session.checkpoints[checkpoint_id]}
        if mode == "rollback":
            checkpoint_id = str(args.get("checkpoint_id") or "")
            checkpoint = session.checkpoints.get(checkpoint_id)
            if checkpoint is None:
                raise ValueError("Unknown Python checkpoint_id")
            replay = session.python_history[:int(checkpoint["python_history_length"])]
            session.repl.reset_python()
            for cell in replay:
                result = session.repl.execute_python(cell["code"], timeout=int(args.get("timeout", 30)), cwd=cell.get("cwd"))
                if "Error:" in result:
                    raise RuntimeError(f"Checkpoint replay failed at cell {cell.get('cell_name')}: {result}")
            session.python_history = replay
            return {"session_id": session_id, "mode": mode, "checkpoint_id": checkpoint_id, "replayed_cells": len(replay)}
        if mode == "reset":
            session.repl.reset_python()
            session.python_history.clear()
            session.checkpoints.clear()
            return {"session_id": session_id, "mode": mode, "generation": session.repl.python_generation}
        if mode != "execute":
            raise ValueError("run_code mode must be execute, history, variables, packages, checkpoint, rollback, or reset")
        code = str(args.get("code") or "")
        if not code:
            raise ValueError("code is required in execute mode")
        before = _snapshot_files(cwd)
        started = time.perf_counter()
        raw = session.repl.execute_python(code, timeout=int(args.get("timeout", 30)), cwd=cwd)
        duration = time.perf_counter() - started
        artifacts = _artifacts(before, _snapshot_files(cwd)) if bool(args.get("capture_artifacts", True)) else []
        entry = {
            "execution_id": uuid4().hex, "cell_name": str(args.get("cell_name") or f"cell-{len(session.python_history) + 1}"),
            "code": code, "cwd": cwd, "duration_seconds": round(duration, 4),
            "ok": "Error:" not in raw, "artifacts": artifacts,
        }
        session.python_history.append(entry)
        return {"session_id": session_id, "mode": mode, "output": raw, "execution": entry, "artifacts": artifacts, "metrics": {"duration_seconds": round(duration, 4), "history_length": len(session.python_history)}}

    def command(self, args: dict[str, Any]) -> dict[str, Any]:
        mode = str(args.get("mode") or "execute").casefold()
        if mode in {"inspect", "attach", "follow"}:
            job = self._job(str(args.get("job_id") or ""))
            return {"mode": mode, "job": job.payload(include_output=True, tail_chars=int(args.get("tail_chars", 100_000)))}
        if mode == "stop":
            job = self._job(str(args.get("job_id") or ""))
            self._stop_job(job)
            return {"mode": mode, "job": job.payload(include_output=True)}
        if mode == "stdin":
            job = self._job(str(args.get("job_id") or ""))
            if job.process.poll() is not None or job.process.stdin is None:
                raise ValueError("Job is not running or has no stdin")
            job.process.stdin.write(str(args.get("stdin") or ""))
            job.process.stdin.flush()
            return {"mode": mode, "job": job.payload(include_output=False), "bytes_written": len(str(args.get("stdin") or "").encode())}
        if mode == "jobs":
            return {"mode": mode, "jobs": [job.payload(include_output=False) for job in self.jobs.values()]}
        if mode == "history":
            session = self._session(str(args.get("session_id") or "default"))
            return {"mode": mode, "history": list(session.shell_history)}
        if mode == "discover":
            cwd = Path(str(args.get("cwd") or os.getcwd())).expanduser().resolve()
            commands: dict[str, str] = {}
            package = cwd / "package.json"
            if package.exists():
                try:
                    commands.update({f"npm:{key}": f"npm run {key}" for key in json.loads(package.read_text(encoding="utf-8")).get("scripts", {})})
                except (OSError, json.JSONDecodeError):
                    pass
            if (cwd / "pyproject.toml").exists():
                commands.update({"pytest": "python -m pytest", "ruff": "ruff check ."})
            return {"mode": mode, "cwd": str(cwd), "commands": commands}
        if mode == "git_summary":
            cwd = str(args.get("cwd") or os.getcwd())
            proc = subprocess.run(["git", "-C", cwd, "status", "--short"], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=15, check=False)
            files = [line[3:] for line in proc.stdout.splitlines() if len(line) > 3]
            return {"mode": mode, "cwd": cwd, "exit_code": proc.returncode, "changed_files": files, "raw": proc.stdout + proc.stderr}
        if mode == "checkpoint":
            session_id = str(args.get("session_id") or "default")
            session = self._session(session_id)
            checkpoint_id = str(args.get("checkpoint_id") or uuid4().hex[:12])
            session.checkpoints[checkpoint_id] = {"shell_history_length": len(session.shell_history), "created_at": time.time(), "cwd": str(args.get("cwd") or os.getcwd())}
            return {"mode": mode, "session_id": session_id, "checkpoint_id": checkpoint_id, **session.checkpoints[checkpoint_id]}
        if mode not in {"execute", "start"}:
            raise ValueError("run_command mode must be execute, start, inspect, attach, follow, stop, stdin, jobs, history, discover, git_summary, or checkpoint")
        command = str(args.get("command") or "")
        if not command:
            raise ValueError("command is required")
        cwd = str(Path(str(args.get("cwd") or os.getcwd())).expanduser().resolve())
        if mode == "start" or bool(args.get("detach", False)):
            return {"mode": "start", "job": self._start_job(command, cwd).payload(include_output=False)}
        session_id = str(args.get("session_id") or "default")
        session = self._session(session_id)
        attempts = max(1, min(int(args.get("retry", 0)) + 1, 4))
        started = time.perf_counter()
        outputs: list[str] = []
        parsed: dict[str, Any] = {}
        for attempt in range(1, attempts + 1):
            raw = session.repl.execute_shell(command, timeout=int(args.get("timeout", 30)), cwd=cwd, profile=args.get("profile"))
            outputs.append(raw)
            parsed = _parse_execution(raw)
            if parsed.get("exit_code") in (0, None) and "Error:" not in raw:
                break
            if not parsed["classified_transient"]:
                break
        duration = time.perf_counter() - started
        entry = {"execution_id": uuid4().hex, "command": command, "cwd": cwd, "attempts": len(outputs), "duration_seconds": round(duration, 4), "parsed": parsed}
        session.shell_history.append(entry)
        return {"mode": "execute", "session_id": session_id, "output": outputs[-1], "attempt_outputs": outputs, "execution": entry, "metrics": {"duration_seconds": round(duration, 4), "attempts": len(outputs)}, "parsed": parsed}

    def _start_job(self, command: str, cwd: str) -> OwnedJob:
        job_id = uuid4().hex[:12]
        stdout_path = self.data_dir / f"{job_id}.stdout.log"
        stderr_path = self.data_dir / f"{job_id}.stderr.log"
        stdout_handle = stdout_path.open("w", encoding="utf-8", errors="replace")
        stderr_handle = stderr_path.open("w", encoding="utf-8", errors="replace")
        creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        if sys.platform == "win32":
            creationflags |= getattr(subprocess, "CREATE_NO_WINDOW", 0)
        process = subprocess.Popen(
            command, cwd=cwd, shell=True, stdin=subprocess.PIPE, stdout=stdout_handle, stderr=stderr_handle,
            text=True, encoding="utf-8", errors="replace", creationflags=creationflags,
            start_new_session=sys.platform != "win32",
        )
        job = OwnedJob(job_id, command, cwd, process, stdout_path, stderr_path, stdout_handle, stderr_handle, time.time())
        self.jobs[job_id] = job
        return job

    def _job(self, job_id: str) -> OwnedJob:
        if not job_id or job_id not in self.jobs:
            raise ValueError("Unknown or unowned job_id")
        return self.jobs[job_id]

    @staticmethod
    def _stop_job(job: OwnedJob) -> None:
        if job.process.poll() is not None:
            return
        job.stopped_by_user = True
        try:
            if sys.platform == "win32":
                subprocess.run(["taskkill", "/PID", str(job.process.pid), "/T", "/F"], capture_output=True, timeout=10, check=False)
            else:
                os.killpg(job.process.pid, signal.SIGTERM)
            job.process.wait(timeout=10)
        except (OSError, subprocess.SubprocessError):
            job.process.kill()
