"""Per-session JSONL conversation storage."""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

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

    def get_previous_summary(self, session_id: str, block: bool = False) -> str | None:
        """Get the summary from the most recent session before this one.

        Lists all JSONL files, sorts by creation time, finds the session
        immediately before the given session_id, reads its last summary entry.
        Returns None if no previous session, no summary found, or block=True.
        """
        if block:
            return None
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

    @staticmethod
    def _parse_since(value: str | None) -> datetime | None:
        """Parse an ISO or relative time filter for historical session recall."""
        if value is None or not str(value).strip():
            return None
        text = str(value).strip()
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            try:
                import dateparser

                parsed = dateparser.parse(
                    text,
                    settings={
                        "PREFER_DATES_FROM": "past",
                        "RELATIVE_BASE": datetime.now().astimezone(),
                        "RETURN_AS_TIMEZONE_AWARE": True,
                    },
                )
            except Exception:
                parsed = None
        if parsed is None:
            raise ValueError(f"Could not parse 'since' value: {value}")
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=datetime.now().astimezone().tzinfo)
        return parsed.astimezone(timezone.utc)

    @staticmethod
    def _entry_time(value: object) -> datetime | None:
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=datetime.now().astimezone().tzinfo)
        return parsed.astimezone(timezone.utc)

    def _session_entries(self, path: Path) -> list[dict[str, Any]]:
        """Read one append-only JSONL safely, preserving its stable line IDs."""
        session_id = path.stem
        entries: list[dict[str, Any]] = []
        try:
            with path.open("r", encoding="utf-8", errors="replace") as handle:
                for line_number, line in enumerate(handle, 1):
                    if not line.strip():
                        continue
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        # Historical files should never make all recall fail.
                        continue
                    if not isinstance(entry, dict):
                        continue
                    content = entry.get("content")
                    if not isinstance(content, str) or not content.strip():
                        continue
                    entries.append({
                        "source": "session",
                        "source_id": f"session:{entry.get('session_id') or session_id}:line:{line_number}",
                        "session_id": str(entry.get("session_id") or session_id),
                        "line_number": line_number,
                        "entry_type": str(entry.get("type") or "message"),
                        "role": str(entry.get("role") or entry.get("type") or "message"),
                        "timestamp": str(entry.get("timestamp") or ""),
                        "content": content,
                    })
        except OSError:
            return []
        return entries

    @staticmethod
    def _query_tokens(query: str) -> list[str]:
        return [token.casefold() for token in re.findall(r"[\w]+", query, flags=re.UNICODE) if len(token) > 1]

    def search_recall(
        self,
        query: str = "",
        *,
        since: str | None = None,
        limit: int = 12,
        roles: list[str] | None = None,
        context_lines: int = 1,
    ) -> list[dict[str, Any]]:
        """Search every persisted session JSONL with stable session/line provenance.

        This intentionally scans the source files rather than relying on a stale
        side index: sessions written by any Ares version become searchable
        immediately, and an old session cannot be missed merely because it was
        never backfilled. Matching considers a small neighboring-turn window so
        references split across adjacent messages remain recoverable.
        """
        bounded_limit = max(1, min(int(limit), 100))
        context_radius = max(0, min(int(context_lines), 3))
        query_text = str(query or "").strip()[:500]
        phrase = query_text.casefold()
        tokens = self._query_tokens(query_text)
        since_time = self._parse_since(since)
        wanted_roles = {str(role).casefold() for role in (roles or []) if str(role).strip()}
        matches: list[dict[str, Any]] = []

        for path in self.sessions_dir.glob("*.jsonl"):
            entries = self._session_entries(path)
            for index, entry in enumerate(entries):
                if wanted_roles and entry["role"].casefold() not in wanted_roles:
                    continue
                entry_time = self._entry_time(entry.get("timestamp"))
                if since_time is not None and (entry_time is None or entry_time < since_time):
                    continue
                start = max(0, index - context_radius)
                end = min(len(entries), index + context_radius + 1)
                window_entries = entries[start:end]
                window_text = "\n".join(item["content"] for item in window_entries).casefold()
                if not tokens:
                    score = 1
                    matched_terms: list[str] = []
                else:
                    present = [token for token in tokens if token in window_text]
                    if not present:
                        continue
                    # Exact phrases and all-token windows rank first.  Keeping
                    # partial-token matches prevents false negatives for a
                    # remembered name in one turn and the requested detail in
                    # its neighbor.
                    score = len(present) * 10
                    if phrase and phrase in window_text:
                        score += 1_000
                    if len(present) == len(tokens):
                        score += 100
                    matched_terms = list(dict.fromkeys(present))
                before = [item["content"] for item in entries[start:index]]
                after = [item["content"] for item in entries[index + 1:end]]
                matches.append({
                    **entry,
                    "matched_terms": matched_terms,
                    "score": score,
                    "context_before": before,
                    "context_after": after,
                })

        matches.sort(
            key=lambda item: (
                int(item.get("score", 0)),
                str(item.get("timestamp") or ""),
                str(item.get("session_id") or ""),
                int(item.get("line_number", 0)),
            ),
            reverse=True,
        )
        return matches[:bounded_limit]
