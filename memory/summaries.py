import json
import os
import shutil
import tempfile
from datetime import datetime
from pathlib import Path


def _looks_like_json_tool_call(text: str) -> bool:
    stripped = str(text or "").strip()
    if not stripped.startswith("{"):
        return False
    try:
        parsed = json.loads(stripped)
    except (json.JSONDecodeError, TypeError):
        return False
    if not isinstance(parsed, dict):
        return False

    function = parsed.get("function")
    if isinstance(function, dict) and function.get("name"):
        return True
    return "name" in parsed and ("parameters" in parsed or "arguments" in parsed)


class SummaryStore:
    """Persists conversation summaries so context survives restarts and /new.

    Stores a rolling window of the most recent N summaries. Each summary
    records the LLM-generated text, the number of turns it covers, and timing.
    """

    def __init__(self, path: Path | str, max_summaries: int = 10):
        self.path = Path(path)
        self.max_summaries = max(1, int(max_summaries))
        self.summaries: list[dict] = []

    # --- Public API ---

    def append(self, summary_text: str, turn_count: int = 0) -> int:
        """Add a new summary entry. Trims to max_summaries. Returns count."""
        text = str(summary_text or "").strip()
        if not text or _looks_like_json_tool_call(text):
            return len(self.summaries)
        entry = {
            "text": text,
            "turn_count": max(0, int(turn_count)),
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "start_date": datetime.now().strftime("%Y-%m-%d"),
        }
        self.summaries.append(entry)
        while len(self.summaries) > self.max_summaries:
            self.summaries.pop(0)
        self.save()
        return len(self.summaries)

    def get_recent(self, n: int = 3) -> list[dict]:
        """Return N most recent summaries."""
        try:
            safe_n = max(0, min(int(n), len(self.summaries)))
        except (ValueError, TypeError):
            return []
        return self.summaries[-safe_n:] if safe_n > 0 else []

    def context_string(self, n: int = 3) -> str:
        """Format recent summaries for system prompt injection.

        Returns empty string if no summaries exist."""
        recent = self.get_recent(n)
        if not recent:
            return ""
        parts = ["Previous session summaries:"]
        for s in recent:
            text = str(s.get("text", "")).strip()
            if text and not _looks_like_json_tool_call(text):
                parts.append(f"- {text}")
        return "\n".join(parts) if len(parts) > 1 else ""

    def size(self) -> int:
        return len(self.summaries)

    def clear(self):
        self.summaries = []
        self.save()

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "max_summaries": self.max_summaries,
            "summaries": self.summaries,
        }
        fd, temp_path = tempfile.mkstemp(dir=self.path.parent, prefix=f".{self.path.name}.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2, ensure_ascii=False)
            shutil.move(temp_path, self.path)
        finally:
            if Path(temp_path).exists():
                Path(temp_path).unlink()

    def load(self) -> bool:
        """Load summaries from disk. Returns True if loaded."""
        if not self.path.exists():
            return False
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            loaded = data.get("summaries") if isinstance(data, dict) else None
            if loaded is None and isinstance(data, list):
                loaded = data
            if loaded is None or not isinstance(loaded, list):
                return False
            self.summaries = []
            for item in loaded:
                if isinstance(item, dict) and item.get("text"):
                    self.summaries.append(item)
            while len(self.summaries) > self.max_summaries:
                self.summaries.pop(0)
            return True
        except (OSError, json.JSONDecodeError, ValueError):
            return False
