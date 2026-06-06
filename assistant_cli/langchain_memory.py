from __future__ import annotations

import json
import uuid
from datetime import datetime
from pathlib import Path


class JsonlChatMessageHistory:
    """JSONL chat history with a lazy LangChain message adapter."""

    def __init__(self, session_dir: str, session_id: str | None = None) -> None:
        self.session_dir = Path(session_dir)
        self.session_dir.mkdir(parents=True, exist_ok=True)
        self.session_id = session_id or datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
        self.path = self.session_dir / f"{self.session_id}.jsonl"

    @property
    def messages(self) -> list[object]:
        from langchain_core.messages import AIMessage, HumanMessage

        out: list[object] = []
        for record in self.records():
            role = record.get("role")
            content = str(record.get("content") or "")
            if role == "user":
                out.append(HumanMessage(content=content))
            elif role == "assistant":
                out.append(AIMessage(content=content))
        return out

    def records(self) -> list[dict]:
        if not self.path.exists():
            return []
        rows = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return rows

    def add_message(self, message: object) -> None:
        role = "assistant" if message.__class__.__name__ == "AIMessage" else "user"
        self._append(role, str(getattr(message, "content", "")))

    def add_user_message(self, message: str) -> None:
        self._append("user", message)

    def add_ai_message(self, message: str) -> None:
        self._append("assistant", message)

    def add_tool_message(self, tool: str, content: str) -> None:
        self._append("tool", content, {"tool": str(tool or "")})

    def clear(self) -> None:
        if self.path.exists():
            self.path.unlink()

    def recent_openai_messages(self, limit: int = 20) -> list[dict[str, str]]:
        rows = [row for row in self.records() if row.get("role") in {"user", "assistant"}]
        return [
            {"role": str(row["role"]), "content": str(row.get("content") or "")}
            for row in rows[-max(0, int(limit)) :]
            if str(row.get("content") or "").strip()
        ]

    def recent_tool_results(self, limit: int = 8) -> list[dict]:
        rows = [row for row in self.records() if row.get("role") == "tool"]
        return rows[-max(0, int(limit)) :]

    def _append(self, role: str, content: str, extra: dict | None = None) -> None:
        text = str(content or "").strip()
        if not text:
            return
        record = {
            "type": "message",
            "session_id": self.session_id,
            "role": role,
            "content": text,
            "at": datetime.now().isoformat(timespec="seconds"),
        }
        if extra:
            record.update(extra)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
