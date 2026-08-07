"""User-model manager: always-on, auto-maintained stable facts about the user.

Ares already keeps a manual ``profile.md`` that the user owns and edits by hand.
This module adds a *separate* lightweight ``user_model.md`` that is:

* injected into context **every turn** (so basic facts about the user are present
  without paying the cost of a memory search), and
* auto-updated **between conversations** by the background reflection worker,
  which distills stable, basic facts from each exchange.

The file lives separately from ``profile.md`` so the automatic step never erases
the user's manual edits.  Under its ``## Facts`` heading the manager only ever
*appends* unique facts; existing lines are never deleted by an auto-update.
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import Any

from ares.context.blend import truncate_to_tokens
from ares.infra.static_cache import MtimeFileCache

USER_MODEL_TEMPLATE = """# About You (auto-maintained)

Ares updates this file between conversations with stable, basic facts about you
so it can personalize help without searching memory. You may edit this file
freely: new facts are appended under "## Facts" and existing lines are never
erased by the auto-update. Only the "## Facts" list is managed automatically.

## Facts
"""

# Splitting the document at the "## Facts" heading keeps any manual prose above
# it (the header) untouched by the append-only merge below.
_SECTION_RE = re.compile(r"^##\s+Facts\b", re.IGNORECASE)
_SECRET_RE = re.compile(
    r"(?i)(password|passwd|secret|private[ _-]?key|api[ _-]?key|access[ _-]?token|credential)",
)
# A fact list longer than this is trimmed from the oldest entries (top of the
# list) so the brain file stays small enough for Ares to actually read it.
_MAX_FACTS = 60


def _normalized_fact(value: str) -> str:
    text = str(value).strip().lstrip("-").strip().casefold()
    # Drop punctuation so facts that differ only by trailing/inline punctuation
    # (e.g. "Replies." vs "Replies") collapse to the same dedup key.
    text = re.sub(r"[^a-z0-9\s]", " ", text)
    return " ".join(text.split())


def _looks_secret(fact: str) -> bool:
    """Refuse to store anything that looks like a credential.

    The auto-extractor is also instructed not to return secrets; this is a
    defensive second layer so a determined model cannot persist keys.
    """
    text = fact.casefold()
    if _SECRET_RE.search(text):
        return True
    # crude "key=value" or "key: <long token>" secret heuristic
    if re.search(r"[=:]\s*[\w\-]{16,}", text):
        return True
    return False


class UserModelManager:
    """Manages the auto-maintained ``user_model.md`` file."""

    def __init__(self, data_dir: Path, user_model_path: str | Path = "", max_facts: int = _MAX_FACTS) -> None:
        self.data_dir = Path(data_dir).expanduser()
        self.user_model_path = (
            Path(user_model_path).expanduser()
            if user_model_path
            else self.data_dir / "user_model.md"
        )
        self._max_facts = max(1, int(max_facts))
        self._cache = MtimeFileCache()

    def ensure_exists(self) -> None:
        """Create user_model.md with a template if it does not exist."""
        if not self.user_model_path.exists():
            self.user_model_path.parent.mkdir(parents=True, exist_ok=True)
            self.user_model_path.write_text(USER_MODEL_TEMPLATE, encoding="utf-8")
            self._cache.invalidate(self.user_model_path)

    def read(self) -> str:
        """Read user-model content, returning empty string when missing/unreadable."""
        return self._cache.read_text(self.user_model_path).strip()

    def write(self, content: str) -> None:
        """Write user-model content to disk."""
        self.user_model_path.parent.mkdir(parents=True, exist_ok=True)
        self.user_model_path.write_text(content.rstrip() + "\n", encoding="utf-8")
        self._cache.invalidate(self.user_model_path)

    @staticmethod
    def _facts_heading_index(lines: list[str]) -> int:
        for index, line in enumerate(lines):
            if _SECTION_RE.match(line.strip()):
                return index
        return -1

    def get_context(self, token_budget: int = 400) -> str:
        """Return the user model as a context block for every turn."""
        content = self.read()
        if not content:
            return ""
        return truncate_to_tokens(
            f"## User Model (stable facts about the user)\n\n{content}", token_budget
        )

    def merge_facts(self, facts: list[str] | Any) -> list[str]:
        """Append unique, non-secret stable facts. Returns the newly added facts.

        Existing bullets (and any manual header prose) are never erased.  When the
        fact list would exceed :data:`_MAX_FACTS`, only the oldest auto-managed
        facts are trimmed from the top of the ``## Facts`` section.
        """
        content = self.read() or USER_MODEL_TEMPLATE.rstrip()
        lines = content.splitlines()
        heading_index = self._facts_heading_index(lines)
        if heading_index < 0:
            if lines and lines[-1].strip():
                lines.append("")
            lines.append("## Facts")
            heading_index = len(lines) - 1

        existing_body = lines[heading_index + 1 :]
        seen = {_normalized_fact(line) for line in existing_body if line.strip().startswith("-")}
        added: list[str] = []
        for raw in facts or []:
            fact = " ".join(str(raw).split()).strip().lstrip("-").strip()
            if len(fact) < 3:
                continue
            if _looks_secret(fact):
                continue
            key = _normalized_fact(fact)
            if not key or key in seen:
                continue
            seen.add(key)
            added.append(f"- {fact}")
        if not added:
            return []

        body = list(existing_body)
        # Only bullet lines count toward the cap. Non-bullet lines (manual prose
        # or blank separators, which the docstring promises never to erase) are
        # always kept; only the oldest auto-managed bullets are trimmed.
        bullet_count = sum(1 for line in body if line.strip().startswith("-"))
        total_bullets = bullet_count + len(added)
        if total_bullets > self._max_facts:
            excess = total_bullets - self._max_facts
            trimmed = 0
            kept: list[str] = []
            for line in body:
                if line.strip().startswith("-") and trimmed < excess:
                    trimmed += 1
                    continue
                kept.append(line)
            body = kept
        new_lines = lines[: heading_index + 1] + body + added
        self.write("\n".join(new_lines).strip() + "\n")
        return added


class UserModelReflector:
    """Extract stable, basic facts about the user with a small tool-free LLM call."""

    def __init__(self, llm_client: Any, config: Any) -> None:
        self.llm = llm_client
        self.config = config

    @staticmethod
    def _parse_facts(text: str) -> list[str]:
        clean = str(text or "").strip()
        if clean.startswith("```"):
            clean = re.sub(r"^```(?:json)?\s*|\s*```$", "", clean, flags=re.IGNORECASE)
        try:
            payload = json.loads(clean)
        except json.JSONDecodeError:
            match = re.search(r"\{.*\}", clean, re.DOTALL)
            if not match:
                return []
            try:
                payload = json.loads(match.group())
            except json.JSONDecodeError:
                return []
        facts = payload.get("facts") if isinstance(payload, dict) else None
        if not isinstance(facts, list):
            return []
        return [str(item).strip() for item in facts if str(item).strip()]

    async def extract(self, *, user_text: str, assistant_text: str) -> list[str]:
        """Return a list of stable user facts distilled from one exchange."""
        prompt = (
            "You maintain Ares' lightweight 'user model': stable, basic facts about the user that "
            "help personalize future help without searching memory. Return ONLY a JSON object of the "
            'form {"facts": ["...", "..."]}.\n\n'
            "Rules:\n"
            "- Capture durable, basic facts only: identity, role/job, location or timezone, languages, "
            "operating system and tooling, coding or communication preferences, recurring projects, and "
            "stable constraints. One short sentence per fact.\n"
            "- DO NOT include secrets, passwords, API keys, credentials, or sensitive personal identifiers.\n"
            "- DO NOT include ephemeral task details, one-off instructions, or things already obvious from "
            "the current request.\n"
            "- If nothing durable and new can be learned, return empty facts: {\"facts\": []}.\n\n"
            f"USER:\n{user_text[:8_000]}\n\nASSISTANT:\n{assistant_text[:4_000]}\n"
        )
        chat_kwargs: dict[str, Any] = {"max_tokens": 400, "temperature": 0.1}
        # Mirror ConversationReflector: prefer the configured review model with a
        # fallback to the client's own model when the client exposes one.
        review_model = str(getattr(self.config, "model", "") or "").strip()
        client_model = getattr(self.llm, "model", None)
        if review_model and client_model:
            chat_kwargs.update({"model": review_model, "fallback_model": client_model})
        try:
            response = await asyncio.wait_for(
                self.llm.chat(
                    [{"role": "user", "content": prompt}], tools=[], **chat_kwargs
                ),
                timeout=float(getattr(self.config, "timeout_seconds", 45)),
            )
        except Exception:
            return []
        content = response.get("content") or response.get("reasoning_content") or "{}"
        return self._parse_facts(str(content))


__all__ = ["USER_MODEL_TEMPLATE", "UserModelManager", "UserModelReflector"]
