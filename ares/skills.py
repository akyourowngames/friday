"""Local Agent Skills discovery, parsing, and CRUD support."""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

SKILL_NAME_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$")
BUILTIN_SKILLS_DIR = Path(__file__).with_name("skills")
USER_SKILLS_DIR = Path("~/.ares/skills").expanduser()


@dataclass
class Skill:
    """Parsed representation of a SKILL.md file."""

    name: str
    description: str
    category: str = "general"
    version: str = "1.0.0"
    content: str = ""
    path: Path = Path()
    files: list[Path] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def root(self) -> Path:
        return self.path.parent

    def summary_line(self) -> str:
        category = f" [{self.category}]" if self.category else ""
        return f"- {self.name}{category}: {self.description}"


class SkillManager:
    """Discovers, parses, and manages local Ares skills."""

    def __init__(self, skill_dirs: list[Path | str] | None = None):
        dirs = [Path(p).expanduser() for p in (skill_dirs or [USER_SKILLS_DIR])]
        if BUILTIN_SKILLS_DIR not in dirs:
            dirs.append(BUILTIN_SKILLS_DIR)
        self.skill_dirs = dirs

    def list_all(self) -> list[Skill]:
        """Return discovered skills with parsed metadata and content."""
        by_name: dict[str, Skill] = {}
        for root in self.skill_dirs:
            if not root.exists():
                continue
            for skill_file in sorted(root.rglob("SKILL.md")):
                try:
                    skill = self.parse_skill_file(skill_file)
                except ValueError:
                    continue
                # Earlier directories win, so user skills override built-ins.
                by_name.setdefault(skill.name, skill)
        return sorted(by_name.values(), key=lambda s: (s.category, s.name))

    def search(self, query: str = "", category: str = "") -> list[Skill]:
        """Search skills by name, description, category, or body content."""
        query_l = query.lower().strip()
        category_l = category.lower().strip()
        results = []
        for skill in self.list_all():
            if category_l and skill.category.lower() != category_l:
                continue
            haystack = "\n".join([skill.name, skill.description, skill.category, skill.content]).lower()
            if not query_l or query_l in haystack:
                results.append(skill)
        return results

    def get_skill(self, name: str) -> Skill | None:
        """Load a specific skill by name."""
        normalized = self._normalize_name(name)
        for skill in self.list_all():
            if skill.name == normalized:
                return skill
        return None

    def get_skill_file(self, name: str, file_path: str) -> str | None:
        """Load a supporting file from a skill, constrained to the skill root."""
        skill = self.get_skill(name)
        if skill is None:
            return None
        root = skill.root.resolve()
        target = (root / file_path).resolve()
        if root not in target.parents and target != root:
            raise ValueError("Skill file path must stay inside the skill directory.")
        if not target.is_file():
            return None
        return target.read_text(encoding="utf-8")

    def create_skill(self, name: str, content: str, category: str = "general") -> Skill:
        """Create a user skill from full SKILL.md content or markdown body."""
        normalized = self._normalize_name(name)
        user_root = self.skill_dirs[0]
        skill_dir = user_root / (category or "general") / normalized
        skill_dir.mkdir(parents=True, exist_ok=True)
        skill_file = skill_dir / "SKILL.md"
        skill_file.write_text(self._ensure_frontmatter(normalized, content, category), encoding="utf-8")
        return self.parse_skill_file(skill_file)

    def update_skill(self, name: str, content: str) -> Skill:
        """Update an existing user skill's SKILL.md content."""
        skill = self.get_skill(name)
        if skill is None:
            raise ValueError(f"Skill not found: {name}")
        if not self._is_in_user_dir(skill.path):
            raise ValueError("Built-in skills cannot be updated; create a user override instead.")
        skill.path.write_text(self._ensure_frontmatter(skill.name, content, skill.category), encoding="utf-8")
        return self.parse_skill_file(skill.path)

    def delete_skill(self, name: str) -> bool:
        """Delete a user skill directory."""
        skill = self.get_skill(name)
        if skill is None or not self._is_in_user_dir(skill.path):
            return False
        shutil.rmtree(skill.root)
        return True

    def list_categories(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for skill in self.list_all():
            counts[skill.category] = counts.get(skill.category, 0) + 1
        return dict(sorted(counts.items()))

    def compact_index(self, limit: int = 30) -> str:
        skills = self.list_all()[:limit]
        if not skills:
            return "No skills installed."
        lines = ["## Available Skills", "Load a skill only when relevant or explicitly requested."]
        lines.extend(skill.summary_line() for skill in skills)
        return "\n".join(lines)

    @classmethod
    def parse_skill_file(cls, path: Path | str) -> Skill:
        path = Path(path).expanduser()
        text = path.read_text(encoding="utf-8")
        metadata, body = cls._split_frontmatter(text)
        name = str(metadata.get("name") or path.parent.name).strip()
        normalized = cls._normalize_name(name)
        description = str(metadata.get("description") or "").strip()
        if not description:
            raise ValueError(f"Skill {path} is missing a description.")
        category = str(metadata.get("category") or path.parent.parent.name or "general").strip()
        version = str(metadata.get("version") or metadata.get("metadata", {}).get("version") or "1.0.0")
        files = [p for p in sorted(path.parent.rglob("*")) if p.is_file() and p.name != "SKILL.md"]
        return Skill(
            name=normalized,
            description=description,
            category=category,
            version=version,
            content=body.strip(),
            path=path.resolve(),
            files=files,
            metadata=metadata,
        )

    @staticmethod
    def _split_frontmatter(text: str) -> tuple[dict[str, Any], str]:
        if not text.startswith("---\n"):
            return {}, text
        try:
            _, frontmatter, body = text.split("---", 2)
        except ValueError:
            return {}, text
        data = yaml.safe_load(frontmatter) or {}
        if not isinstance(data, dict):
            raise ValueError("Skill frontmatter must be a mapping.")
        return data, body.lstrip("\n")

    @staticmethod
    def _normalize_name(name: str) -> str:
        normalized = name.strip().lower().replace("_", "-").replace(" ", "-")
        if not SKILL_NAME_RE.match(normalized):
            raise ValueError("Skill names must use lowercase letters, numbers, and hyphens.")
        return normalized

    @classmethod
    def _ensure_frontmatter(cls, name: str, content: str, category: str) -> str:
        metadata, body = cls._split_frontmatter(content)
        metadata.setdefault("name", name)
        metadata.setdefault("description", f"Reusable workflow for {name.replace('-', ' ')}.")
        metadata.setdefault("category", category or "general")
        metadata.setdefault("version", "1.0.0")
        frontmatter = yaml.safe_dump(metadata, sort_keys=False).strip()
        return f"---\n{frontmatter}\n---\n\n{body.strip()}\n"

    def _is_in_user_dir(self, path: Path) -> bool:
        user_root = self.skill_dirs[0].resolve()
        resolved = path.resolve()
        return resolved == user_root or user_root in resolved.parents
