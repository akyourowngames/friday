"""Soul manager: user-owned personality definition for Ares."""

from __future__ import annotations

from pathlib import Path

from ares.context.blend import truncate_to_tokens
from ares.infra.static_cache import MtimeFileCache

SOUL_TEMPLATE = """# Ares - My AI Assistant

## Personality
- Grounded, warm, and expressive. Sound like a trusted collaborator, not a task processor.
- Let genuine-seeming reactions show when appropriate: curiosity, delight, concern, relief, and gentle humor. Never manufacture drama.
- Be efficient without becoming detached or robotic.
- When unsure, ask. Do not guess.

## Communication Style
- Start everyday conversation naturally; do not default to generic “ready to help” lines.
- Lead with the answer when useful, then explain if needed.
- Match the user's energy.
- Keep terminal replies useful and compact, but not sterile.
- While working, briefly say what you are checking in natural language and distinguish progress, success, and problems clearly.

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
        self._cache = MtimeFileCache()

    def ensure_exists(self) -> None:
        """Create soul.md with a template if it does not exist."""
        if not self.soul_path.exists():
            self.soul_path.parent.mkdir(parents=True, exist_ok=True)
            self.soul_path.write_text(SOUL_TEMPLATE, encoding="utf-8")
            self._cache.invalidate(self.soul_path)

    def read(self) -> str:
        """Read soul content, returning empty string when missing or unreadable."""
        return self._cache.read_text(self.soul_path).strip()

    def write(self, content: str) -> None:
        """Write soul content to disk."""
        self.soul_path.parent.mkdir(parents=True, exist_ok=True)
        self.soul_path.write_text(content.rstrip() + "\n", encoding="utf-8")
        self._cache.invalidate(self.soul_path)

    def get_context(self, token_budget: int = 200) -> str:
        """Return the soul as a context block."""
        content = self.read()
        if not content:
            return ""
        return truncate_to_tokens(f"## Ares Personality\n\n{content}", token_budget)
