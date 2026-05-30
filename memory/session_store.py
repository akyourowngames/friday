"""Session-based memory: full per-session transcripts as JSONL.

Each conversation session is a single JSONL file under `session_store_dir`, one
turn per line (`{"role", "content", "at"}`), appended live as the conversation
happens. An index file tracks every session with light metadata (id, started_at,
ended_at, turn_count, digested) so a new session can pull recent prior sessions
back as context — this is what lets KING remember across sessions, not just within
a day-based fact store.

Writes are append-only for the transcript (cheap, crash-safe) and atomic for the
index. No regex, no hardcoded content.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime
from pathlib import Path

from config import settings


def _resolve(path_value) -> Path:
    target = Path(path_value).expanduser()
    if not target.is_absolute():
        target = Path(__file__).resolve().parent.parent / target
    return target.resolve()


class SessionStore:
    def __init__(self, directory=None, index_path=None):
        self.directory = _resolve(directory or settings.session_store_dir)
        self.index_path = _resolve(index_path or settings.session_index_file)
        self.session_id: str | None = None
        self._turns = 0

    # --- lifecycle ---

    def start_session(self, session_id: str | None = None) -> str:
        self.directory.mkdir(parents=True, exist_ok=True)
        if not session_id:
            # Timestamp + short uuid so two sessions started in the same second
            # never collide on the same id.
            import uuid

            session_id = datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
        self.session_id = session_id
        self._turns = 0
        index = self._load_index()
        index["sessions"] = [s for s in index.get("sessions", []) if s.get("id") != session_id]
        index["sessions"].append({
            "id": session_id,
            "started_at": datetime.now().isoformat(timespec="seconds"),
            "ended_at": None,
            "turn_count": 0,
            "digested": False,
        })
        self._trim_and_save(index)
        return session_id

    def _session_path(self, session_id: str) -> Path:
        return self.directory / f"{session_id}.jsonl"

    def log_turn(self, user: str, assistant: str) -> None:
        if not self.session_id:
            self.start_session()
        path = self._session_path(self.session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        now = datetime.now().isoformat(timespec="seconds")
        with path.open("a", encoding="utf-8") as handle:
            if str(user or "").strip():
                handle.write(json.dumps({"role": "user", "content": str(user), "at": now}, ensure_ascii=False) + "\n")
            if str(assistant or "").strip():
                handle.write(json.dumps({"role": "assistant", "content": str(assistant), "at": now}, ensure_ascii=False) + "\n")
        self._turns += 1
        self._update_index_meta()

    def end_session(self) -> None:
        if not self.session_id:
            return
        index = self._load_index()
        for entry in index.get("sessions", []):
            if entry.get("id") == self.session_id:
                entry["ended_at"] = datetime.now().isoformat(timespec="seconds")
                entry["turn_count"] = self._turns
        self._trim_and_save(index)

    # --- reading ---

    def read_session(self, session_id: str) -> list[dict]:
        path = self._session_path(session_id)
        if not path.exists():
            return []
        turns = []
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                turns.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return turns

    def recent_sessions(self, limit: int, exclude_current: bool = True) -> list[dict]:
        index = self._load_index()
        sessions = list(index.get("sessions", []))
        if exclude_current and self.session_id:
            sessions = [s for s in sessions if s.get("id") != self.session_id]
        sessions.sort(key=lambda s: str(s.get("started_at", "")), reverse=True)
        return sessions[: max(0, int(limit))]

    def undigested_sessions(self, exclude_current: bool = True) -> list[dict]:
        index = self._load_index()
        out = []
        for entry in index.get("sessions", []):
            if entry.get("digested"):
                continue
            if exclude_current and entry.get("id") == self.session_id:
                continue
            if int(entry.get("turn_count") or 0) <= 0:
                continue
            out.append(entry)
        return out

    def mark_digested(self, session_id: str) -> None:
        index = self._load_index()
        for entry in index.get("sessions", []):
            if entry.get("id") == session_id:
                entry["digested"] = True
        self._trim_and_save(index)

    def context_string(self, count: int | None = None, max_chars: int | None = None) -> str:
        """Build a compact recap of the most recent prior sessions for injection.

        Gives a new session continuity ("here is what we last worked on") without
        replaying entire transcripts. Truncated to a character budget so it never
        bloats the system prompt.
        """
        count = int(settings.session_context_count if count is None else count)
        max_chars = int(settings.session_context_max_chars if max_chars is None else max_chars)
        if count <= 0:
            return ""
        sessions = self.recent_sessions(count, exclude_current=True)
        if not sessions:
            return ""
        lines = ["Recent sessions (most recent first):"]
        for entry in sessions:
            sid = str(entry.get("id"))
            turns = self.read_session(sid)
            user_turns = [t for t in turns if t.get("role") == "user"]
            if not user_turns:
                continue
            started = str(entry.get("started_at", "")).split("T")[0]
            topics = "; ".join(str(t.get("content", "")).strip()[:80] for t in user_turns[:4])
            lines.append(f"- {started}: {topics}")
        body = "\n".join(lines)
        if len(body) > max_chars:
            body = body[:max_chars].rstrip() + " …"
        return body if len(lines) > 1 else ""

    # --- index helpers ---

    def _load_index(self) -> dict:
        if not self.index_path.exists():
            return {"sessions": []}
        try:
            data = json.loads(self.index_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"sessions": []}
        if not isinstance(data, dict) or not isinstance(data.get("sessions"), list):
            return {"sessions": []}
        return data

    def _update_index_meta(self) -> None:
        index = self._load_index()
        for entry in index.get("sessions", []):
            if entry.get("id") == self.session_id:
                entry["turn_count"] = self._turns
        self._trim_and_save(index)

    def _trim_and_save(self, index: dict) -> None:
        keep = int(settings.session_keep_count or 200)
        sessions = index.get("sessions", [])
        if keep > 0 and len(sessions) > keep:
            sessions.sort(key=lambda s: str(s.get("started_at", "")))
            dropped = sessions[:-keep]
            for entry in dropped:
                path = self._session_path(str(entry.get("id")))
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass
            index["sessions"] = sessions[-keep:]
        self._save_index(index)

    def _save_index(self, index: dict) -> None:
        self.index_path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp = tempfile.mkstemp(dir=self.index_path.parent, prefix=f".{self.index_path.name}.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(index, handle, indent=2, ensure_ascii=False)
            os.replace(temp, self.index_path)
        finally:
            if os.path.exists(temp):
                os.unlink(temp)
