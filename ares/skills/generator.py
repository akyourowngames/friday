"""Generate and save safe, instruction-only Ares skills from natural language."""

from __future__ import annotations

import inspect
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any

import yaml

from ares.skills.discovery import SKILL_NAME_RE, Skill, SkillManager


SKILL_GENERATION_PROMPT = """Create a high-quality Ares SKILL.md.

Name: {name}
Description: {description}
Category: {category}

Return only one Markdown SKILL.md document. It must contain YAML frontmatter
with name, description, category, and version (1.0.0), followed by a concise
scope section, clear numbered steps, safety/verification guidance, and a few
examples. This is an instruction-only skill: do not embed executable code,
shell commands that modify a machine, credentials, or external downloads.
"""


class SkillGenerationError(ValueError):
    """The model response could not be made into a valid local skill."""


class SkillGenerator:
    """Generate a validated local skill using an Ares-compatible LLM client."""

    def __init__(self, llm_client: Any) -> None:
        self.llm = llm_client

    async def generate(
        self,
        name: str,
        description: str,
        category: str = "general",
        version: str = "1.0.0",
    ) -> Skill:
        normalized_name = self._normalize_name(name)
        description = str(description or "").strip()
        if len(description) < 8:
            raise SkillGenerationError("Describe what the skill should do in at least eight characters.")
        category = self._normalize_category(category)
        prompt = SKILL_GENERATION_PROMPT.format(
            name=normalized_name,
            description=description,
            category=category,
        )
        content = await self._complete(prompt)
        return self._parse_skill(content, normalized_name, description, category, version)

    async def generate_from_task(self, task: str, category: str = "general") -> Skill:
        task = str(task or "").strip()
        if not task:
            raise SkillGenerationError("A task description is required.")
        return await self.generate(self._task_to_name(task), task, category=category)

    def save_skill(self, skill: Skill, directory: Path | str, *, replace: bool = False) -> Path:
        """Atomically persist a generated SKILL.md to the user skills directory."""
        root = Path(directory).expanduser()
        category = self._normalize_category(skill.category)
        destination = root / category / self._normalize_name(skill.name)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists() and not replace:
            raise FileExistsError(f"Skill '{skill.name}' already exists. Use /skills update {skill.name} to replace it.")
        temporary: Path | None = Path(tempfile.mkdtemp(prefix=".ares-generated-", dir=destination.parent))
        backup: Path | None = None
        try:
            frontmatter = {
                "name": self._normalize_name(skill.name),
                "description": str(skill.description).strip(),
                "category": category,
                "version": str(skill.version or "1.0.0"),
            }
            payload = "---\n" + yaml.safe_dump(frontmatter, sort_keys=False).strip() + "\n---\n\n" + skill.content.strip() + "\n"
            (temporary / "SKILL.md").write_text(payload, encoding="utf-8")
            parsed = SkillManager.parse_skill_file(temporary / "SKILL.md")
            self._validate_parsed(parsed)
            if destination.exists():
                backup = destination.with_name(f".{destination.name}.backup")
                if backup.exists():
                    shutil.rmtree(backup)
                os.replace(destination, backup)
            os.replace(temporary, destination)
            temporary = None
            if backup is not None:
                shutil.rmtree(backup, ignore_errors=True)
            return destination
        except Exception:
            if backup is not None and backup.exists() and not destination.exists():
                os.replace(backup, destination)
            raise
        finally:
            if temporary is not None and temporary.exists():
                shutil.rmtree(temporary, ignore_errors=True)

    async def _complete(self, prompt: str) -> str:
        """Adapt to both the lightweight test double and Ares' LLMClient."""
        complete = getattr(self.llm, "complete", None)
        if callable(complete):
            response = complete(prompt)
            if inspect.isawaitable(response):
                response = await response
            return self._content_from_response(response)
        chat = getattr(self.llm, "chat", None)
        if not callable(chat):
            raise SkillGenerationError("The configured model client cannot generate a skill.")
        response = chat(
            [
                {"role": "system", "content": "You produce safe Ares SKILL.md files."},
                {"role": "user", "content": prompt},
            ],
            tools=[],
        )
        if inspect.isawaitable(response):
            response = await response
        return self._content_from_response(response)

    @staticmethod
    def _content_from_response(response: Any) -> str:
        if isinstance(response, str):
            return response
        if isinstance(response, dict):
            return str(response.get("content") or "")
        return str(response or "")

    def _parse_skill(
        self,
        content: str,
        name: str,
        description: str,
        category: str,
        version: str,
    ) -> Skill:
        cleaned = self._strip_fence(str(content or "").strip())
        if not cleaned:
            raise SkillGenerationError("The model returned an empty skill.")
        try:
            metadata, body = SkillManager._split_frontmatter(cleaned)
        except (TypeError, ValueError, yaml.YAMLError) as exc:
            raise SkillGenerationError(f"The generated frontmatter is invalid: {exc}") from exc
        # The requested identity wins. A generation must not silently turn into
        # an unrelated skill because the model changed its frontmatter.
        metadata = dict(metadata)
        metadata["name"] = name
        metadata["description"] = description
        metadata["category"] = category
        metadata["version"] = version
        normalized = "---\n" + yaml.safe_dump(metadata, sort_keys=False).strip() + "\n---\n\n" + body.strip() + "\n"
        temporary_root = Path(tempfile.mkdtemp(prefix=".ares-skill-parse-"))
        try:
            path = temporary_root / "SKILL.md"
            path.write_text(normalized, encoding="utf-8")
            parsed = SkillManager.parse_skill_file(path)
            self._validate_parsed(parsed)
            return Skill(
                name=parsed.name,
                description=parsed.description,
                category=parsed.category,
                version=parsed.version,
                content=parsed.content,
                metadata=parsed.metadata,
            )
        finally:
            shutil.rmtree(temporary_root, ignore_errors=True)

    @staticmethod
    def _validate_parsed(skill: Skill) -> None:
        if not SKILL_NAME_RE.match(skill.name):
            raise SkillGenerationError("Generated skill name is invalid.")
        if not skill.description or "#" not in skill.content or len(skill.content.strip()) < 40:
            raise SkillGenerationError("Generated skill needs a description and substantive Markdown instructions.")
        # A skill should guide tool use, not ship code that an agent could run
        # blindly after being loaded.
        prohibited = ("```python", "```javascript", "```powershell", "```bash", "curl | sh", "invoke-webrequest")
        body = skill.content.casefold()
        if any(token in body for token in prohibited):
            raise SkillGenerationError("Generated skill contains executable content; try a safer description.")

    @staticmethod
    def _strip_fence(value: str) -> str:
        if value.startswith("```") and value.endswith("```"):
            lines = value.splitlines()
            return "\n".join(lines[1:-1]).strip()
        return value

    @staticmethod
    def _normalize_name(value: str) -> str:
        name = re.sub(r"[^a-z0-9-]+", "-", str(value).strip().casefold().replace("_", "-"))
        name = re.sub(r"-{2,}", "-", name).strip("-")
        if not SKILL_NAME_RE.match(name):
            raise SkillGenerationError("Skill names must use lowercase letters, numbers, and hyphens.")
        return name

    @staticmethod
    def _normalize_category(value: str) -> str:
        category = re.sub(r"[^a-z0-9-]+", "-", str(value or "general").strip().casefold())
        category = category.strip("-") or "general"
        return category if re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", category) else "general"

    @staticmethod
    def _task_to_name(task: str) -> str:
        words = re.findall(r"[a-z0-9]+", task.casefold())
        stop_words = {"a", "an", "the", "for", "to", "and", "or", "in", "on", "at", "of", "location"}
        words = [word for word in words if word not in stop_words and len(word) > 2]
        verbs = {"build", "check", "create", "draft", "generate", "make", "write"}
        if len(words) >= 2 and words[0] in verbs:
            words = [words[1], words[0], *words[2:]]
        candidate = "-".join(words[:3]) or "generated-skill"
        return SkillGenerator._normalize_name(candidate)
