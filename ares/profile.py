"""Profile manager: user-owned identity, preferences, goals, and projects."""

from __future__ import annotations

from pathlib import Path

from ares.context_blend import truncate_to_tokens

PROFILE_TEMPLATE = """# About Me

## Identity
- Name:
- Pronouns:

## Preferences
- Coding style:
- Assistant style:
- Terminal/OS:

## Current Projects

## Goals

## Notes

"""


class ProfileManager:
    """Manages the user's profile file."""

    def __init__(self, data_dir: Path, profile_path: str | Path = ""):
        self.data_dir = Path(data_dir).expanduser()
        self.profile_path = (
            Path(profile_path).expanduser() if profile_path else self.data_dir / "profile.md"
        )

    def ensure_exists(self) -> None:
        """Create profile.md with a template if it does not exist."""
        if not self.profile_path.exists():
            self.profile_path.parent.mkdir(parents=True, exist_ok=True)
            self.profile_path.write_text(PROFILE_TEMPLATE, encoding="utf-8")

    def read(self) -> str:
        """Read profile content, returning empty string when missing or unreadable."""
        if not self.profile_path.exists():
            return ""
        try:
            return self.profile_path.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeDecodeError):
            return ""

    def _resolve_ref_path(self, raw_path: str) -> Path:
        ref_path = Path(raw_path).expanduser()
        if not ref_path.is_absolute():
            ref_path = self.profile_path.parent / ref_path
        return ref_path

    def resolve_imports(self, content: str) -> str:
        """Resolve @path/to/file references in profile content."""
        resolved: list[str] = []
        for line in content.splitlines():
            stripped = line.strip()
            if stripped.startswith("@") and not stripped.startswith("@@"):
                ref_path = self._resolve_ref_path(stripped[1:].strip())
                if ref_path.is_file():
                    try:
                        imported = ref_path.read_text(encoding="utf-8").strip()
                        resolved.append(f"<!-- imported from {ref_path} -->")
                        if imported:
                            resolved.append(imported)
                    except (OSError, UnicodeDecodeError):
                        resolved.append(f"<!-- could not read {ref_path} -->")
                else:
                    resolved.append(f"<!-- file not found: {ref_path} -->")
            else:
                resolved.append(line)
        return "\n".join(resolved)

    def get_context(self, token_budget: int = 400) -> str:
        """Return the profile as a context block."""
        content = self.read()
        if not content:
            return ""
        content = self.resolve_imports(content)
        return truncate_to_tokens(f"## User Profile\n\n{content}", token_budget)
