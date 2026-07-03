"""Per-session JSONL conversation storage."""
from __future__ import annotations

import json
from pathlib import Path

from ares.tools.dates import now_local_iso


class SessionStore:
    """Manages per-session JSONL conversation files.

    Each session gets its own .jsonl file at {data_dir}/sessions/{session_id}.jsonl.
    Lines are append-only JSON objects with a 'type' field.
    """

    def __init__(self, data_dir: Path) -> None:
        self.sessions_dir = data_dir / "sessions"
        self.sessions_dir.mkdir(parents=True, exist_ok=True)

    def _session_path(self, session_id: str) -> Path:
        return self.sessions_dir / f"{session_id}.jsonl"

    def write_message(
        self,
        session_id: str,
        role: str,
        content: str,
        tool_calls: list[dict] | None = None,
        tool_call_id: str | None = None,
    ) -> None:
        """Append a message to the session's JSONL file."""
        entry = {
            "type": "message",
            "role": role,
            "content": content,
            "timestamp": now_local_iso(),
            "session_id": session_id,
        }
        if tool_calls:
            entry["tool_calls"] = tool_calls
        if tool_call_id:
            entry["tool_call_id"] = tool_call_id
        self._append(session_id, entry)

    def write_summary(self, session_id: str, summary: str) -> None:
        """Append a summary entry to the session's JSONL file."""
        entry = {
            "type": "summary",
            "content": summary,
            "timestamp": now_local_iso(),
            "session_id": session_id,
        }
        self._append(session_id, entry)

    def _append(self, session_id: str, entry: dict) -> None:
        path = self._session_path(session_id)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def read_session(self, session_id: str) -> list[dict]:
        """Read all entries from a session's JSONL file."""
        path = self._session_path(session_id)
        if not path.exists():
            return []
        entries = []
        for line in path.read_text(encoding="utf-8").strip().split("\n"):
            if line.strip():
                entries.append(json.loads(line))
        return entries

    def get_previous_summary(self, session_id: str) -> str | None:
        """Get the summary from the most recent session before this one.

        Lists all JSONL files, sorts by creation time, finds the session
        immediately before the given session_id, reads its last summary entry.
        Returns None if no previous session or no summary found.
        """
        sessions = self.list_sessions()
        # Find the session just before the current one
        current_idx = None
        for i, s in enumerate(sessions):
            if s["session_id"] == session_id:
                current_idx = i
                break

        if current_idx is None or current_idx >= len(sessions) - 1:
            return None

        # list_sessions returns newest first, so previous = current_idx + 1
        prev_session_id = sessions[current_idx + 1]["session_id"]
        entries = self.read_session(prev_session_id)
        # Find the last summary entry
        for entry in reversed(entries):
            if entry.get("type") == "summary":
                return entry["content"]
        return None

    def list_sessions(self) -> list[dict]:
        """List all sessions with metadata, newest first."""
        sessions = []
        for path in self.sessions_dir.glob("*.jsonl"):
            session_id = path.stem
            message_count = 0
            first_timestamp = None
            for line in path.read_text(encoding="utf-8").strip().split("\n"):
                if not line.strip():
                    continue
                entry = json.loads(line)
                if entry.get("type") == "message":
                    message_count += 1
                if first_timestamp is None:
                    first_timestamp = entry.get("timestamp")
            sessions.append({
                "session_id": session_id,
                "started_at": first_timestamp,
                "message_count": message_count,
                "file_path": str(path),
            })
        # Sort by started_at descending, then session_id descending for determinism
        sessions.sort(key=lambda s: (s.get("started_at") or "", s.get("session_id") or ""), reverse=True)
        return sessions
