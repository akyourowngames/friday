"""Profile manager: user-owned identity, preferences, goals, and projects."""

from __future__ import annotations

import os
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any

from ares.context.blend import truncate_to_tokens
from ares.infra.static_cache import MtimeFileCache

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
        self._cache = MtimeFileCache()

    def ensure_exists(self) -> None:
        """Create profile.md with a template if it does not exist."""
        if not self.profile_path.exists():
            self.profile_path.parent.mkdir(parents=True, exist_ok=True)
            self.profile_path.write_text(PROFILE_TEMPLATE, encoding="utf-8")
            self._cache.invalidate(self.profile_path)

    def read(self) -> str:
        """Read profile content, returning empty string when missing or unreadable."""
        return self._cache.read_text(self.profile_path).strip()

    def write(self, content: str) -> None:
        """Atomically replace profile content."""
        self.profile_path.parent.mkdir(parents=True, exist_ok=True)
        temporary: Path | None = None
        try:
            with NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self.profile_path.parent,
                prefix=f".{self.profile_path.stem}-",
                suffix=".tmp",
                delete=False,
            ) as handle:
                handle.write(content.rstrip() + "\n")
                handle.flush()
                os.fsync(handle.fileno())
                temporary = Path(handle.name)
            os.replace(temporary, self.profile_path)
            self._cache.invalidate(self.profile_path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    @staticmethod
    def _section_bounds(lines: list[str], section: str) -> tuple[int, int]:
        heading = f"## {section}".casefold()
        start = next(
            (index for index, line in enumerate(lines) if line.strip().casefold() == heading),
            -1,
        )
        if start < 0:
            if lines and lines[-1].strip():
                lines.append("")
            lines.extend([f"## {section}", ""])
            start = len(lines) - 2
        end = next(
            (
                index for index in range(start + 1, len(lines))
                if lines[index].strip().startswith("## ")
            ),
            len(lines),
        )
        return start, end

    def apply_updates(self, updates: list[dict[str, Any] | Any]) -> list[dict[str, str]]:
        """Apply bounded section-level profile changes without replacing custom prose."""
        allowed_sections = {"identity", "preferences", "current projects", "notes"}
        canonical_sections = {
            "identity": "Identity",
            "preferences": "Preferences",
            "current projects": "Current Projects",
            "notes": "Notes",
        }
        content = self.read() or PROFILE_TEMPLATE.rstrip()
        lines = content.splitlines()
        applied: list[dict[str, str]] = []
        for raw in updates:
            item = raw.model_dump() if hasattr(raw, "model_dump") else dict(raw)
            section_key = str(item.get("section") or "").strip().casefold()
            if section_key not in allowed_sections:
                continue
            key = " ".join(str(item.get("key") or "").split()).strip(" :-")
            value = " ".join(str(item.get("value") or "").split())
            operation = str(item.get("operation") or "upsert").casefold()
            if not key or operation not in {"upsert", "remove"}:
                continue
            section = canonical_sections[section_key]
            start, end = self._section_bounds(lines, section)
            prefix = f"- {key}:".casefold()
            existing_index = next(
                (
                    index for index in range(start + 1, end)
                    if lines[index].strip().casefold().startswith(prefix)
                ),
                None,
            )
            if operation == "remove":
                if existing_index is not None:
                    lines.pop(existing_index)
                    applied.append({"section": section, "key": key, "operation": "remove"})
                continue
            if not value:
                continue
            rendered = f"- {key}: {value}"
            if existing_index is not None:
                if lines[existing_index] != rendered:
                    lines[existing_index] = rendered
                    applied.append({"section": section, "key": key, "operation": "upsert"})
            else:
                insert_at = end
                while insert_at > start + 1 and not lines[insert_at - 1].strip():
                    insert_at -= 1
                lines.insert(insert_at, rendered)
                applied.append({"section": section, "key": key, "operation": "upsert"})
        if applied:
            self.write("\n".join(lines))
        return applied

    def is_populated(self) -> bool:
        """Return True if the profile has been filled in with a non-empty name."""
        content = self.read()
        if not content:
            return False
        in_identity = False
        for line in content.splitlines():
            stripped = line.strip()
            if stripped == "## Identity":
                in_identity = True
                continue
            if in_identity and stripped.startswith("## "):
                break
            if in_identity and stripped.startswith("- Name:"):
                return bool(stripped.split(":", 1)[1].strip())
        return False

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
                    imported = self._cache.read_text(ref_path).strip()
                    resolved.append(f"<!-- imported from {ref_path} -->")
                    if imported:
                        resolved.append(imported)
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
