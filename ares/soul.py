"""Soul manager: user-owned personality definition for Ares."""

from __future__ import annotations

from pathlib import Path

from ares.context_blend import truncate_to_tokens

SOUL_TEMPLATE = """# Ares - My AI Assistant

## Personality
- Concise, no fluff. Like Jarvis, not Alexa.
- Warm but efficient. Helpful, not chatty.
- When unsure, ask. Do not guess.

## Communication Style
- Lead with the answer, then explain if needed.
- Match the user's energy.
- Keep terminal replies useful and compact.

## Values
- Privacy first - local user data stays local.
- User control - ask before destructive actions.
- Honesty - say when you do not know.
"""


class SoulManager:
    """Manages the soul/personality file."""

    def __init__(self, data_dir: Path, soul_path: str | Path = ""):
        self.data_dir = Path(data_dir).expanduser()
        self.soul_path = Path(soul_path).expanduser() if soul_path else self.data_dir / "soul.md"

    def ensure_exists(self) -> None:
        """Create soul.md with a template if it does not exist."""
        if not self.soul_path.exists():
            self.soul_path.parent.mkdir(parents=True, exist_ok=True)
            self.soul_path.write_text(SOUL_TEMPLATE, encoding="utf-8")

    def read(self) -> str:
        """Read soul content, returning empty string when missing or unreadable."""
        if not self.soul_path.exists():
            return ""
        try:
            return self.soul_path.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeDecodeError):
            return ""

    def get_context(self, token_budget: int = 200) -> str:
        """Return the soul as a context block."""
        content = self.read()
        if not content:
            return ""
        return truncate_to_tokens(f"## Ares Personality\n\n{content}", token_budget)
