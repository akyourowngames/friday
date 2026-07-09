"""Local Agent Skills discovery, parsing, and CRUD support."""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

SKILL_NAME_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$")
WORD_RE = re.compile(r"[a-z0-9][a-z0-9-]{1,}")
STOP_WORDS = {
    "about", "after", "again", "also", "and", "are", "can", "code", "could",
    "for", "from", "have", "into", "make", "more", "need", "please", "that",
    "the", "this", "use", "using", "want", "when", "with", "work", "you",
}
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
    examples: list[dict[str, Any]] = field(default_factory=list)
    test_commands: list[str] = field(default_factory=list)
    lint_messages: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def root(self) -> Path:
        return self.path.parent

    def summary_line(self) -> str:
        category = f" [{self.category}]" if self.category else ""
        return f"- {self.name}{category}: {self.description}"

    @property
    def model_invocable(self) -> bool:
        """Return whether Ares may auto-load this skill for matching tasks."""
        raw = self.metadata.get("disable-model-invocation", self.metadata.get("disable_model_invocation", False))
        return not _as_bool(raw)

    def context_block(self, max_chars: int = 6000) -> str:
        """Render full skill instructions for hidden model context."""
        parts = [
            f"# Skill: {self.name}",
            f"Category: {self.category}",
            f"Description: {self.description}",
            f"Version: {self.version}",
            "",
            self.content,
        ]
        if self.files:
            rel_files = [str(path.relative_to(self.root)) for path in self.files[:20]]
            parts.extend(["", "Supporting files available on disk:", *[f"- {file}" for file in rel_files]])
        if self.test_commands:
            parts.extend(["", "Suggested verification commands:", *[f"- {cmd}" for cmd in self.test_commands]])
        text = "\n".join(part for part in parts if part is not None).strip()
        if len(text) <= max_chars:
            return text
        return text[: max_chars - 80].rstrip() + "\n\n[Skill instructions truncated for context budget.]"


class SkillManager:
    """Discovers, parses, and manages local Ares skills."""

    def __init__(self, skill_dirs: list[Path | str] | None = None):
        dirs = [Path(p).expanduser() for p in (skill_dirs or [USER_SKILLS_DIR])]
        for project_dir in self._project_skill_dirs():
            if project_dir not in dirs:
                dirs.append(project_dir)
        if BUILTIN_SKILLS_DIR not in dirs:
            dirs.append(BUILTIN_SKILLS_DIR)
        self.skill_dirs = dirs

    @staticmethod
    def _project_skill_dirs(start: Path | None = None) -> list[Path]:
        """Return repo/local skill folders from the current directory upward."""
        current = (start or Path.cwd()).resolve()
        roots = [current, *current.parents]
        dirs: list[Path] = []
        for root in roots:
            for name in (".ares/skills", ".agents/skills"):
                candidate = root / name
                if candidate.exists() and candidate not in dirs:
                    dirs.append(candidate)
            if (root / ".git").exists():
                break
        return dirs

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

    def relevant_skills(self, user_input: str, limit: int = 3, min_score: int = 4) -> list[Skill]:
        """Return model-invocable skills that should silently guide this turn."""
        query_tokens = self._tokens(user_input)
        query_l = user_input.lower()
        if not query_tokens and not query_l.strip():
            return []

        scored: list[tuple[int, Skill]] = []
        for skill in self.list_all():
            if not skill.model_invocable:
                continue
            score = self._relevance_score(skill, query_l, query_tokens)
            if score >= min_score:
                scored.append((score, skill))

        scored.sort(key=lambda item: (-item[0], item[1].category, item[1].name))
        return [skill for _, skill in scored[: max(0, limit)]]

    def auto_context(self, user_input: str, limit: int = 3, max_chars: int = 12000) -> str:
        """Build hidden context for relevant skills without user-facing chatter."""
        skills = self.relevant_skills(user_input, limit=limit)
        if not skills:
            return ""
        blocks = [
            "## Auto-Loaded Skills",
            "Use these instructions silently to complete the current user request. "
            "Do not mention skill loading unless the user asks.",
        ]
        remaining = max_chars - sum(len(block) for block in blocks)
        for skill in skills:
            if remaining <= 500:
                break
            block = skill.context_block(max_chars=min(6000, remaining))
            blocks.extend(["", block])
            remaining -= len(block)
        return "\n".join(blocks).strip()

    @staticmethod
    def _tokens(text: str) -> set[str]:
        return {
            token
            for token in WORD_RE.findall(text.lower())
            if len(token) >= 3 and token not in STOP_WORDS
        }

    def _relevance_score(self, skill: Skill, query_l: str, query_tokens: set[str]) -> int:
        name = skill.name.lower()
        description = skill.description.lower()
        category = skill.category.lower()
        examples = " ".join(str(example.get("prompt", "")) for example in skill.examples).lower()

        name_tokens = self._tokens(name.replace("-", " "))
        description_tokens = self._tokens(description)
        category_tokens = self._tokens(category)
        example_tokens = self._tokens(examples)
        content_tokens = self._tokens(skill.content[:4000])

        score = 0
        if name in query_l or name.replace("-", " ") in query_l:
            score += 10
        if description and any(phrase in query_l for phrase in self._trigger_phrases(description)):
            score += 6
        score += 5 * len(query_tokens & name_tokens)
        score += 4 * len(query_tokens & description_tokens)
        score += 2 * len(query_tokens & example_tokens)
        score += len(query_tokens & category_tokens)
        score += min(3, len(query_tokens & content_tokens))
        return score

    @staticmethod
    def _trigger_phrases(description: str) -> list[str]:
        phrases = []
        for marker in ("use when", "when the user", "for "):
            index = description.find(marker)
            if index >= 0:
                phrase = description[index + len(marker):].split(".", 1)[0].strip()
                if 8 <= len(phrase) <= 120:
                    phrases.append(phrase)
        return phrases

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
        lines = [
            "## Available Skills",
            "Ares may auto-load relevant skills silently. Full instructions load only when a skill matches the current task.",
        ]
        lines.extend(skill.summary_line() for skill in skills)
        return "\n".join(lines)

    def lint_all(self) -> dict[str, list[str]]:
        """Return lint messages for all discovered skills."""
        return {
            skill.name: skill.lint_messages
            for skill in self.list_all()
            if skill.lint_messages
        }

    @classmethod
    def lint_skill_file(cls, path: Path | str) -> list[str]:
        """Lint one SKILL.md file without requiring callers to catch parsing errors."""
        try:
            skill = cls.parse_skill_file(path)
        except ValueError as exc:
            return [str(exc)]
        return skill.lint_messages

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
        examples = cls._normalize_examples(metadata.get("examples", []))
        test_commands = cls._normalize_test_commands(metadata.get("test_commands") or metadata.get("tests") or [])
        files = [p for p in sorted(path.parent.rglob("*")) if p.is_file() and p.name != "SKILL.md"]
        lint_messages = cls._lint_metadata(
            name=normalized,
            description=description,
            category=category,
            version=version,
            body=body,
            examples=examples,
            test_commands=test_commands,
        )
        return Skill(
            name=normalized,
            description=description,
            category=category,
            version=version,
            content=body.strip(),
            path=path.resolve(),
            files=files,
            examples=examples,
            test_commands=test_commands,
            lint_messages=lint_messages,
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

    @staticmethod
    def _normalize_examples(raw: Any) -> list[dict[str, Any]]:
        if not isinstance(raw, list):
            return []
        examples: list[dict[str, Any]] = []
        for item in raw:
            if isinstance(item, str):
                examples.append({"prompt": item})
            elif isinstance(item, dict):
                examples.append(dict(item))
        return examples

    @staticmethod
    def _normalize_test_commands(raw: Any) -> list[str]:
        if isinstance(raw, str):
            return [raw]
        if not isinstance(raw, list):
            return []
        return [str(item) for item in raw if str(item).strip()]

    @staticmethod
    def _lint_metadata(
        *,
        name: str,
        description: str,
        category: str,
        version: str,
        body: str,
        examples: list[dict[str, Any]],
        test_commands: list[str],
    ) -> list[str]:
        messages: list[str] = []
        if len(description) < 12:
            messages.append("Description should be at least 12 characters.")
        if not category:
            messages.append("Category is missing.")
        if not re.fullmatch(r"\d+\.\d+\.\d+", version):
            messages.append("Version should use semver, for example 1.0.0.")
        if "# " not in body:
            messages.append("Body should include at least one markdown heading.")
        for index, example in enumerate(examples, 1):
            if not str(example.get("prompt", "")).strip():
                messages.append(f"Example {index} is missing a prompt.")
        if examples and not test_commands:
            messages.append("Examples are present but no test_commands metadata is configured.")
        return messages

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


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() in {"1", "true", "yes", "on"}
