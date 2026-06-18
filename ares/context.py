"""Project context discovery from the current working directory."""

from __future__ import annotations

from pathlib import Path

from ares.context_blend import truncate_to_tokens

SCAN_TARGETS = [
    ("CLAUDE.md", 150),
    ("AGENTS.md", 150),
    (".ares/config.json", 50),
    ("pyproject.toml", 50),
    ("package.json", 50),
    ("README.md", 100),
    (".hermes.md", 100),
]


class ProjectContext:
    """Auto-discovers useful project metadata files from one directory."""

    def __init__(self, cwd: Path | None = None, enabled: bool = True, max_files: int = 2):
        self.cwd = cwd or Path.cwd()
        self.enabled = enabled
        self.max_files = max(0, int(max_files))

    def discover(self, max_files: int | None = None) -> list[tuple[str, str]]:
        """Scan the current directory for known project context files."""
        if not self.enabled:
            return []

        limit = self.max_files if max_files is None else max(0, int(max_files))
        found: list[tuple[str, str]] = []
        for filename, max_lines in SCAN_TARGETS:
            if len(found) >= limit:
                break
            path = self.cwd / filename
            if not path.exists() or not path.is_file():
                continue
            try:
                raw = path.read_bytes()
                if b"\x00" in raw[:1024]:
                    continue
                text = raw.decode("utf-8")
            except (OSError, UnicodeDecodeError):
                continue

            all_lines = text.splitlines()
            lines = all_lines[:max_lines]
            if len(all_lines) > max_lines:
                lines.append(f"... ({len(all_lines) - max_lines} more lines)")
            found.append((filename, "\n".join(lines)))
        return found

    def get_context(self, token_budget: int = 400) -> str:
        """Return discovered project context as a bounded context block."""
        files = self.discover()
        if not files:
            return ""

        parts = ["## Current Project Context"]
        for filename, content in files:
            parts.append(f"\n### {filename}\n\n{content}")
        return truncate_to_tokens("\n".join(parts), token_budget)
