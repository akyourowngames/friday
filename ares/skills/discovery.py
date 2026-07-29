"""Local Agent Skills discovery, parsing, and CRUD support."""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from ares.infra.static_cache import FileSignature, file_signature
from ares.integrations.turn_policy import (
    is_browser_action_request,
    is_desktop_action_request,
)

SKILL_NAME_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$")
WORD_RE = re.compile(r"[a-z0-9][a-z0-9-]{1,}")
STOP_WORDS = {
    "about", "after", "again", "also", "and", "are", "can", "code", "could",
    "for", "from", "have", "into", "make", "more", "need", "please", "that",
    "the", "this", "use", "using", "want", "when", "with", "work", "you",
}
AUTOLOAD_BROAD_TOKENS = {
    "app", "apps", "code", "desktop", "file", "files", "project",
    "status", "tool", "tools", "window", "windows", "workflow",
}
AUTOMATION_ACTION_TOKENS = {
    "click", "close", "inspect", "launch", "navigate", "open", "save", "type",
    "write",
}
BROWSER_ACTION_TOKENS = {
    "click", "fill", "inspect", "login", "navigate", "open", "operate", "press",
    "scroll", "select", "submit", "type", "upload", "visit",
}
BROWSER_TARGET_TOKENS = {
    "browser", "dashboard", "form", "github", "google", "instagram", "linkedin",
    "page", "portal", "site", "twitter", "url", "web", "webpage",
    "website", "youtube",
}
BROWSER_WINDOW_EXCEPTIONS = (
    "actual chrome window", "browser window", "chrome window", "visible desktop",
    "windows window", "desktop window",
)
RECENCY_TOKENS = {
    "current", "latest", "news", "now", "recent", "recommendation",
    "recommendations", "today",
}
# ``discovery.py`` already lives inside ``ares/skills``. Using
# ``with_name("skills")`` accidentally pointed at ``ares/skills/skills`` and
# made every bundled skill invisible, allowing loosely matching user-generated
# skills to win by default.
BUILTIN_SKILLS_DIR = Path(__file__).parent
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

    @property
    def co_load_with(self) -> tuple[str, ...]:
        """Return explicitly declared compatible skills for intentional bundles."""
        raw = self.metadata.get("co-load-with", self.metadata.get("co_load_with", []))
        if isinstance(raw, str):
            raw = [part.strip() for part in raw.split(",")]
        if not isinstance(raw, list):
            return ()
        names: list[str] = []
        for value in raw:
            name = str(value or "").strip().lower().replace("_", "-").replace(" ", "-")
            if name and SKILL_NAME_RE.match(name) and name not in names:
                names.append(name)
        return tuple(names)

    @property
    def co_load_triggers(self) -> tuple[str, ...]:
        """Return precise phrases required before this skill joins a bundle."""
        raw = self.metadata.get("co-load-triggers", self.metadata.get("co_load_triggers", []))
        if isinstance(raw, str):
            raw = [raw]
        if not isinstance(raw, list):
            return ()
        return tuple(
            phrase for value in raw
            if (phrase := " ".join(str(value or "").lower().split()))
        )

    @property
    def requires_primary(self) -> tuple[str, ...]:
        """Return primary skills required for this skill to auto-load."""
        raw = self.metadata.get("requires-primary", self.metadata.get("requires_primary", []))
        if isinstance(raw, str):
            raw = [raw]
        if not isinstance(raw, list):
            return ()
        return tuple(
            name for value in raw
            if (name := str(value or "").strip().lower().replace("_", "-").replace(" ", "-"))
            and SKILL_NAME_RE.match(name)
        )

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
        self._parsed_cache: dict[Path, tuple[FileSignature, Skill | None]] = {}
        self._inventory_signature: tuple[tuple[str, FileSignature], ...] | None = None
        self._cached_skills: tuple[Skill, ...] = ()
        self._inventory_generation = 0
        self._compact_index_cache: dict[tuple[int, int], str] = {}

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
        """Return discovered skills with parsed metadata and content.

        Discovery still observes every ``SKILL.md`` path so external edits and
        newly installed skills become visible immediately.  Unchanged files are
        not reparsed (or recursively scanned for supporting files) on each
        request, which keeps the hot prompt-building path bounded.
        """
        discovered: list[tuple[Path, FileSignature]] = []
        for root in self.skill_dirs:
            if not root.exists():
                continue
            try:
                skill_files = sorted(root.rglob("SKILL.md"))
            except OSError:
                continue
            for skill_file in skill_files:
                path = skill_file.resolve()
                signature = file_signature(path)
                if signature[1] is not None:
                    discovered.append((path, signature))

        inventory = tuple((str(path), signature) for path, signature in discovered)
        if inventory == self._inventory_signature:
            return list(self._cached_skills)

        by_name: dict[str, Skill] = {}
        next_cache: dict[Path, tuple[FileSignature, Skill | None]] = {}
        for skill_file, signature in discovered:
            cached = self._parsed_cache.get(skill_file)
            if cached is not None and cached[0] == signature:
                skill = cached[1]
            else:
                try:
                    skill = self.parse_skill_file(skill_file)
                except ValueError:
                    skill = None
            next_cache[skill_file] = (signature, skill)
            if skill is not None:
                # Earlier directories win, so user skills override built-ins.
                by_name.setdefault(skill.name, skill)
        self._parsed_cache = next_cache
        self._inventory_signature = inventory
        self._cached_skills = tuple(sorted(by_name.values(), key=lambda s: (s.category, s.name)))
        self._inventory_generation += 1
        self._compact_index_cache.clear()
        return list(self._cached_skills)

    def invalidate_cache(self) -> None:
        """Forget parsed skill and index state after explicit local CRUD."""
        self._parsed_cache.clear()
        self._inventory_signature = None
        self._cached_skills = ()
        self._compact_index_cache.clear()

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

    @staticmethod
    def _should_skip_skill_loading(user_input: str) -> bool:
        """Determine if skill loading should be skipped for this input.

        FIX: Prevents unnecessary skill loading for conversation continuations,
        memory recall requests, and simple acknowledgements.
        """
        text_l = user_input.lower().strip()

        # Pure conversation patterns - no skills needed
        _CONVERSATION_ONLY = re.compile(
            r"^\s*(?:hi|hello|hey|yo|thanks?|thank\s+you|thx|ok(?:ay)?|k|sure|yes|yep|no|nope|nah|"
            r"got\s+it|sounds\s+good|all\s+good|cool|great|perfect|fine|bye|goodbye)\s*[!.?,]*\s*$",
            re.I,
        )
        if _CONVERSATION_ONLY.match(text_l):
            return True

        # Memory recall requests - handled by memory system, not skills
        _MEMORY_RECALL = re.compile(
            r"\b(?:do\s+you|can\s+you|could\s+you)\s+(?:remember|recall)\b", re.I,
        )
        if _MEMORY_RECALL.search(text_l):
            return True

        # Continuation keywords without specific task
        _CONTINUATION_ONLY = re.compile(
            r"^\s*(?:continue|resume|go\s+on|keep\s+going|proceed|next)\s*[!.?]*\s*$",
            re.I,
        )
        if _CONTINUATION_ONLY.match(text_l):
            return True

        # Very short inputs (likely acknowledgements)
        if len(text_l.split()) < 3:
            return True

        return False

    def relevant_skills(self, user_input: str, limit: int = 3, min_score: int = 4) -> list[Skill]:
        """Return model-invocable skills that should silently guide this turn."""
        # FIX: Skip skill loading for conversation continuations and memory recall
        if self._should_skip_skill_loading(user_input):
            return []

        query_tokens = self._tokens(user_input)
        query_l = user_input.lower()
        if not query_tokens and not query_l.strip():
            return []

        # A website/web-app request has one execution route: Playwright.  Words
        # such as "latest" and "summarize" describe the page task, not a
        # request to load generic research or code-review playbooks.  Keeping
        # this route exclusive prevents unrelated skill instructions from
        # competing with browser evidence and stale-reference recovery.
        browser_named = "browser-use" in query_l or "browser use" in query_l
        browser_request = browser_named or is_browser_action_request(user_input)
        desktop_named = "computer-use" in query_l or "computer use" in query_l
        desktop_request = desktop_named or is_desktop_action_request(user_input)

        all_skills = self.list_all()
        scored: list[tuple[int, Skill]] = []
        for skill in all_skills:
            if not skill.model_invocable:
                continue
            if skill.requires_primary and not browser_request:
                continue
            score = self._relevance_score(skill, query_l, query_tokens)
            if score >= min_score:
                scored.append((score, skill))

        scored.sort(key=lambda item: (-item[0], item[1].category, item[1].name))
        bounded_limit = max(0, limit)
        if not bounded_limit:
            return []

        if browser_request:
            browser_skill = next(
                (skill for skill in all_skills if skill.model_invocable and skill.name == "browser-use"),
                None,
            )
            if browser_skill is not None:
                # Browser Use is the sole primary route for web pages.  A
                # companion may join only when it declares compatibility and
                # independently matches an explicit sub-task (form filling,
                # drafting a reply, content review, etc.).
                return self._select_compatible_skills(browser_skill, scored, query_l, bounded_limit)
        if desktop_request:
            computer_skill = next(
                (
                    skill
                    for skill in all_skills
                    if skill.model_invocable and skill.name == "computer-use"
                ),
                None,
            )
            if computer_skill is not None:
                # An explicit native-desktop route is exclusive just like an
                # explicit browser route. Delivery-channel words such as
                # "send" or "telegram" must not replace it with an unrelated
                # composite workflow.
                return self._select_compatible_skills(
                    computer_skill, scored, query_l, bounded_limit
                )

        if not scored:
            return []
        # Default to one focused primary skill.  Extra instructions are added
        # only through an explicit co-load declaration or a user-named skill.
        return self._select_compatible_skills(scored[0][1], scored, query_l, bounded_limit)

    @staticmethod
    def _is_explicitly_named(skill: Skill, query_l: str) -> bool:
        return skill.name in query_l or skill.name.replace("-", " ") in query_l

    def _select_compatible_skills(
        self,
        primary: Skill,
        scored: list[tuple[int, Skill]],
        query_l: str,
        limit: int,
    ) -> list[Skill]:
        """Select a primary skill plus only compatible, independently matched peers."""
        selected = [primary]
        primary_allows = set(primary.co_load_with)
        for _score, candidate in scored:
            if candidate.name == primary.name or len(selected) >= limit:
                continue
            compatible = candidate.name in primary_allows or primary.name in set(candidate.co_load_with)
            explicitly_named = self._is_explicitly_named(candidate, query_l)
            trigger_matches = not candidate.co_load_triggers or any(
                trigger in query_l for trigger in candidate.co_load_triggers
            )
            primary_matches = not candidate.requires_primary or primary.name in set(candidate.requires_primary)
            if explicitly_named or (compatible and trigger_matches and primary_matches):
                selected.append(candidate)
        return selected

    def selection_reason(self, skill: Skill, user_input: str) -> str:
        """Explain the direct request signal that selected a skill."""
        query_l = user_input.lower()
        query_tokens = self._tokens(user_input)
        name = skill.name.lower()
        normalized_name = name.replace("-", " ")
        if name in query_l or normalized_name in query_l:
            return "you named this workflow"

        quoted = self._quoted_trigger_phrases(skill.description)
        for phrase in quoted:
            if phrase in query_l:
                return f'matches “{phrase}”'

        name_hits, description_hits, example_hits = self._match_tokens(skill, query_tokens)
        if skill.name == "browser-use" and is_browser_action_request(user_input):
            return "matches a browser action request"
        if skill.name == "computer-use" and is_desktop_action_request(user_input):
            return "matches a desktop action request"
        if skill.name == "web-research" and self._is_web_research_request(query_l, query_tokens):
            return "matches a web research request"
        if skill.category.lower() == "automation" and query_tokens & AUTOMATION_ACTION_TOKENS:
            targets = query_tokens - AUTOMATION_ACTION_TOKENS - AUTOLOAD_BROAD_TOKENS
            if targets:
                return "matches a desktop action request"
        if skill.category.lower() == "research" and query_tokens & RECENCY_TOKENS:
            return "matches a current-information request"
        direct_hits = sorted((name_hits | description_hits | example_hits) - AUTOLOAD_BROAD_TOKENS)
        if direct_hits:
            return "matches " + ", ".join(direct_hits[:3])
        return "matches this task"

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
        name_tokens, description_tokens, example_tokens = self._match_tokens(skill, query_tokens)

        # Browser pages are DOM/accessibility tasks, not desktop-coordinate
        # tasks. Prevent the generic Windows skill from winning an otherwise
        # ambiguous "open Google/Instagram" request; an explicit request for
        # the visible browser window remains a native desktop workflow.
        browser_request = is_browser_action_request(query_l)
        if (
            name == "computer-use"
            and browser_request
            and not any(phrase in query_l for phrase in BROWSER_WINDOW_EXCEPTIONS)
        ):
            return 0
        if (
            name == "browser-use"
            and name not in query_l
            and name.replace("-", " ") not in query_l
            and not browser_request
        ):
            return 0

        web_research_request = name == "web-research" and self._is_web_research_request(query_l, query_tokens)
        if not web_research_request and not self._passes_autoload_gate(
            skill, query_l, query_tokens, name_tokens, description_tokens, example_tokens
        ):
            return 0

        score = 0
        if name in query_l or name.replace("-", " ") in query_l:
            score += 10
        if description and any(phrase in query_l for phrase in self._quoted_trigger_phrases(description)):
            score += 6
        score += 5 * len(name_tokens)
        score += 4 * len(description_tokens)
        score += 2 * len(example_tokens)
        score += len(query_tokens & self._tokens(category))
        if web_research_request:
            score += 8
        # Long skill bodies often contain generic words such as "files" and
        # "status".  They may refine a direct match, but never create one.
        if score:
            score += min(2, len(query_tokens & self._tokens(skill.content[:4000])))
        return score

    def _passes_autoload_gate(
        self,
        skill: Skill,
        query_l: str,
        query_tokens: set[str],
        name_hits: set[str],
        description_hits: set[str],
        example_hits: set[str],
    ) -> bool:
        """Require an intent signal before auto-loading a broad workflow."""
        name = skill.name.lower()
        if name in query_l or name.replace("-", " ") in query_l:
            return True
        if any(phrase in query_l for phrase in self._quoted_trigger_phrases(skill.description)):
            return True

        specific_name_hits = name_hits - AUTOLOAD_BROAD_TOKENS
        specific_description_hits = description_hits - AUTOLOAD_BROAD_TOKENS
        category = skill.category.lower()
        if category == "general":
            # Generated composite workflows often share their destination
            # ("telegram") and generic verbs ("search", "send") with many
            # unrelated tasks. Require at least two name concepts for a
            # multi-token general skill unless the user names it or an example
            # matches. This prevents download-song-to-telegram from loading
            # for an ordinary Telegram message.
            specific_name_token_count = len(
                self._tokens(skill.name.replace("-", " ")) - AUTOLOAD_BROAD_TOKENS
            )
            required_name_hits = 1 if specific_name_token_count <= 1 else 2
            return bool(
                len(specific_name_hits) >= required_name_hits or example_hits
            )
        if specific_name_hits or len(specific_description_hits) >= 2 or example_hits:
            return True

        if category == "automation":
            actions = query_tokens & AUTOMATION_ACTION_TOKENS
            targets = query_tokens - AUTOMATION_ACTION_TOKENS - AUTOLOAD_BROAD_TOKENS
            return bool(actions and targets)
        if category == "research":
            return bool(query_tokens & RECENCY_TOKENS and query_tokens & self._tokens(skill.description))
        return False

    @staticmethod
    def _is_web_research_request(query_l: str, query_tokens: set[str]) -> bool:
        """Require both a research/evaluation action and an evidence target."""
        actions = {
            "research", "investigate", "compare", "evaluate", "verify", "fact-check",
            "factcheck", "cite", "summarize",
        }
        targets = {
            "web", "search", "results", "sources", "evidence", "current", "latest",
            "news", "recommendations",
        }
        return bool(
            query_tokens & actions
            and (query_tokens & targets or "search results" in query_l or "web research" in query_l)
        )

    def _match_tokens(self, skill: Skill, query_tokens: set[str]) -> tuple[set[str], set[str], set[str]]:
        """Return request-term overlap for the short, intentional skill fields."""
        name_tokens = self._tokens(skill.name.replace("-", " "))
        description_tokens = self._tokens(skill.description)
        examples = " ".join(str(example.get("prompt", "")) for example in skill.examples).lower()
        example_tokens = self._tokens(examples)
        return (
            query_tokens & name_tokens,
            query_tokens & description_tokens,
            query_tokens & example_tokens,
        )

    @staticmethod
    def _quoted_trigger_phrases(description: str) -> list[str]:
        return [
            phrase.lower().strip()
            for phrase in re.findall(r'"([^\"]{4,120})"', description)
            if phrase.strip()
        ]

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
        self.invalidate_cache()
        return self.parse_skill_file(skill_file)

    def update_skill(self, name: str, content: str) -> Skill:
        """Update an existing user skill's SKILL.md content."""
        skill = self.get_skill(name)
        if skill is None:
            raise ValueError(f"Skill not found: {name}")
        if not self._is_in_user_dir(skill.path):
            raise ValueError("Built-in skills cannot be updated; create a user override instead.")
        skill.path.write_text(self._ensure_frontmatter(skill.name, content, skill.category), encoding="utf-8")
        self.invalidate_cache()
        return self.parse_skill_file(skill.path)

    def delete_skill(self, name: str) -> bool:
        """Delete a user skill directory."""
        skill = self.get_skill(name)
        if skill is None or not self._is_in_user_dir(skill.path):
            return False
        shutil.rmtree(skill.root)
        self.invalidate_cache()
        return True

    def is_editable(self, skill: Skill) -> bool:
        """Return whether a skill belongs to the user-managed skills directory."""
        return self._is_in_user_dir(skill.path)

    def list_categories(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for skill in self.list_all():
            counts[skill.category] = counts.get(skill.category, 0) + 1
        return dict(sorted(counts.items()))

    def compact_index(self, limit: int = 30) -> str:
        skills = self.list_all()[:limit]
        cache_key = (self._inventory_generation, int(limit))
        cached = self._compact_index_cache.get(cache_key)
        if cached is not None:
            return cached
        if not skills:
            index = "No skills installed."
            self._compact_index_cache[cache_key] = index
            return index
        lines = [
            "## Available Skills",
            "Select by the meaning of the complete task. Call load_skill for full instructions only when a skill genuinely fits.",
        ]
        lines.extend(skill.summary_line() for skill in skills)
        index = "\n".join(lines)
        self._compact_index_cache[cache_key] = index
        return index

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
