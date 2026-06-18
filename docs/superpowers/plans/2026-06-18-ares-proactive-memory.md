# Ares v3: Proactive Memory & Context System — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Ares feel like it knows you — soul file, user profile, project context discovery, and smart context blending with token budgeting.

**Architecture:** Three new modules (`soul.py`, `profile.py`, `context.py`) handle file I/O for personality, user profile, and project context. A utility module (`context_blend.py`) handles token estimation, truncation, and the priority-ordered blending logic. Updated `build_context_prompt()` in `prompts.py` orchestrates everything. Agent and CLI updated to wire it all together.

**Tech Stack:** Python 3.11+, pathlib, Rich (for CLI rendering), pytest

**Implementation status:** Complete. Added proactive context modules, config fields, Agent wiring, `/soul`, `/profile`, `/context` commands, README/capability docs, and focused plus full-suite verification.

---

## File Structure

| File | Responsibility |
|------|---------------|
| `ares/soul.py` | **NEW** — SoulManager: read/write/ensure soul.md, get_context() |
| `ares/profile.py` | **NEW** — ProfileManager: read/write/ensure profile.md, resolve @imports, get_context() |
| `ares/context.py` | **NEW** — ProjectContext: scan CWD for project files, get_context() |
| `ares/context_blend.py` | **NEW** — estimate_tokens(), truncate_to_tokens(), build_context_prompt() |
| `ares/prompts.py` | **MODIFY** — Remove old build_context_prompt(), update SYSTEM_PROMPT |
| `ares/agent.py` | **MODIFY** — Init SoulManager + ProfileManager + ProjectContext, update get_context() |
| `ares/models.py` | **MODIFY** — Add soul_path, profile_path, project_context_enabled, etc. to AppConfig |
| `ares/config.py` | **MODIFY** — Add new config defaults |
| `ares/cli.py` | **MODIFY** — Add /soul, /profile, /context commands + update COMPLETER |
| `tests/test_soul.py` | **NEW** — Tests for SoulManager |
| `tests/test_profile.py` | **NEW** — Tests for ProfileManager |
| `tests/test_context.py` | **NEW** — Tests for ProjectContext |
| `tests/test_context_blend.py` | **NEW** — Tests for blending logic |

---

## Task 1: Token Utilities + Blending Logic

**Files:**
- Create: `ares/context_blend.py`
- Create: `tests/test_context_blend.py`

- [ ] **Step 1: Write failing tests for token utilities**

```python
# tests/test_context_blend.py
"""Tests for token estimation, truncation, and context blending."""

from ares.context_blend import (
    estimate_tokens,
    truncate_to_tokens,
    format_memories,
    format_tasks,
    format_summaries,
    build_context_prompt,
)


class TestEstimateTokens:
    def test_empty_string(self):
        assert estimate_tokens("") == 0

    def test_single_word(self):
        assert estimate_tokens("hello") == 1

    def test_multiple_words(self):
        result = estimate_tokens("hello world foo bar")
        assert result == 5  # 4 words * 1.3 = 5.2 → int(5.2) = 5


class TestTruncateToTokens:
    def test_short_text_unchanged(self):
        text = "hello world"
        result = truncate_to_tokens(text, max_tokens=100)
        assert result == text

    def test_long_text_truncated(self):
        words = [f"word{i}" for i in range(200)]
        text = " ".join(words)
        result = truncate_to_tokens(text, max_tokens=50)
        assert "truncated" in result.lower() or len(result) < len(text)

    def test_truncation_note_present(self):
        words = [f"word{i}" for i in range(200)]
        text = " ".join(words)
        result = truncate_to_tokens(text, max_tokens=50)
        assert "truncated" in result.lower()


class TestFormatMemories:
    def test_empty_memories(self):
        result = format_memories([])
        assert result == ""

    def test_formats_with_category(self):
        memories = [{"fact_id": 1, "fact_text": "likes tea", "category": "preference", "importance": 0.8}]
        result = format_memories(memories)
        assert "likes tea" in result
        assert "preference" in result


class TestFormatTasks:
    def test_empty_tasks(self):
        result = format_tasks([])
        assert result == ""

    def test_formats_with_due_date(self):
        tasks = [{"title": "Call mom", "due": "2026-06-20T14:00:00"}]
        result = format_tasks(tasks)
        assert "Call mom" in result
        assert "2026-06-20" in result


class TestFormatSummaries:
    def test_empty_summaries(self):
        result = format_summaries([])
        assert result == ""

    def test_formats_list(self):
        summaries = ["Discussed project setup", "Fixed login bug"]
        result = format_summaries(summaries)
        assert "project setup" in result
        assert "login bug" in result


class TestBuildContextPrompt:
    def test_empty_context(self):
        result = build_context_prompt()
        assert result == ""

    def test_soul_only(self):
        result = build_context_prompt(soul_context="## Personality\nBe concise.")
        assert "Personality" in result
        assert "Be concise" in result

    def test_profile_only(self):
        result = build_context_prompt(profile_context="## User\nName: Alice")
        assert "Alice" in result

    def test_memories_fills_remaining_budget(self):
        memories = [{"fact_id": 1, "fact_text": "likes coffee", "category": "preference", "importance": 0.5}]
        result = build_context_prompt(memories=memories, token_budget=2000)
        assert "coffee" in result

    def test_tasks_always_included(self):
        tasks = [{"title": "Buy milk", "due": None}]
        result = build_context_prompt(tasks=tasks)
        assert "Buy milk" in result

    def test_priority_ordering(self):
        result = build_context_prompt(
            soul_context="SOUL HERE",
            profile_context="PROFILE HERE",
            memories=[{"fact_id": 1, "fact_text": "fact1", "category": "note", "importance": 0.5}],
        )
        soul_pos = result.index("SOUL")
        profile_pos = result.index("PROFILE")
        assert soul_pos < profile_pos

    def test_token_budget_respected(self):
        big_profile = "x " * 500
        result = build_context_prompt(profile_context=big_profile, token_budget=100)
        assert len(result.split()) < 200  # Should be truncated
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd ares && python -m pytest tests/test_context_blend.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'ares.context_blend'`

- [ ] **Step 3: Write minimal implementation**

```python
# ares/context_blend.py
"""Token estimation, truncation, and context blending utilities."""

from __future__ import annotations


def estimate_tokens(text: str) -> int:
    """Rough token estimate: ~1.3 tokens per word."""
    return int(len(text.split()) * 1.3)


def truncate_to_tokens(text: str, max_tokens: int) -> str:
    """Truncate text to fit within token budget."""
    words = text.split()
    max_words = int(max_tokens / 1.3)
    if len(words) <= max_words:
        return text
    return " ".join(words[:max_words]) + "\n\n<!-- truncated to fit context budget -->"


def format_memories(memories: list[dict], token_budget: int = 800) -> str:
    """Format memories for context injection."""
    if not memories:
        return ""
    lines = ["## What I know about you:"]
    for m in memories:
        cat = m.get("category", "note")
        importance = m.get("importance", 0.5)
        lines.append(f"- [{cat}, importance={importance}] #{m['fact_id']}: {m['fact_text']}")
    return truncate_to_tokens("\n".join(lines), token_budget)


def format_tasks(tasks: list[dict]) -> str:
    """Format tasks for context injection."""
    if not tasks:
        return ""
    lines = ["## Your pending tasks:"]
    for t in tasks[:5]:
        due = f" (due: {t['due']})" if t.get("due") else ""
        lines.append(f"- {t['title']}{due}")
    return "\n".join(lines)


def format_summaries(summaries: list[str]) -> str:
    """Format conversation summaries for context injection."""
    if not summaries:
        return ""
    lines = ["## Recent session summaries:"]
    for s in summaries:
        lines.append(f"- {s}")
    return "\n".join(lines)


def build_context_prompt(
    soul_context: str = "",
    profile_context: str = "",
    project_context: str = "",
    memories: list[dict] | None = None,
    tasks: list[dict] | None = None,
    conversation_summaries: list[str] | None = None,
    token_budget: int = 2000,
) -> str:
    """Build blended context string from all sources, respecting token budget."""
    sections: list[str] = []
    used_tokens = 0

    # 1. Soul (highest priority)
    if soul_context:
        sections.append(soul_context)
        used_tokens += estimate_tokens(soul_context)

    # 2. Profile
    if profile_context:
        sections.append(profile_context)
        used_tokens += estimate_tokens(profile_context)

    # 3. Project context
    if project_context:
        sections.append(project_context)
        used_tokens += estimate_tokens(project_context)

    # 4. Conversation summaries
    if conversation_summaries:
        summary_text = format_summaries(conversation_summaries)
        sections.append(summary_text)
        used_tokens += estimate_tokens(summary_text)

    # 5. Memories (fill remaining budget)
    remaining = token_budget - used_tokens
    if memories and remaining > 100:
        memory_section = format_memories(memories, token_budget=remaining)
        sections.append(memory_section)
        used_tokens += estimate_tokens(memory_section)

    # 6. Tasks (always small, always included at end)
    if tasks:
        task_section = format_tasks(tasks)
        sections.append(task_section)

    return "\n\n".join(sections)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd ares && python -m pytest tests/test_context_blend.py -v`
Expected: All 14 tests PASS

- [ ] **Step 5: Commit**

```bash
git add ares/context_blend.py tests/test_context_blend.py
git commit -m "feat: add context blending utilities (token estimation, truncation, priority blending)"
```

---

## Task 2: Soul Manager

**Files:**
- Create: `ares/soul.py`
- Create: `tests/test_soul.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_soul.py
"""Tests for SoulManager."""

from ares.soul import SoulManager, SOUL_TEMPLATE


class TestSoulManager:
    def test_ensure_exists_creates_file(self, tmp_path):
        mgr = SoulManager(data_dir=tmp_path)
        mgr.ensure_exists()
        assert (tmp_path / "soul.md").exists()

    def test_ensure_exists_does_not_overwrite(self, tmp_path):
        mgr = SoulManager(data_dir=tmp_path)
        (tmp_path / "soul.md").write_text("custom soul", encoding="utf-8")
        mgr.ensure_exists()
        assert (tmp_path / "soul.md").read_text(encoding="utf-8") == "custom soul"

    def test_read_returns_content(self, tmp_path):
        mgr = SoulManager(data_dir=tmp_path)
        (tmp_path / "soul.md").write_text("Be concise.", encoding="utf-8")
        assert mgr.read() == "Be concise."

    def test_read_returns_empty_if_missing(self, tmp_path):
        mgr = SoulManager(data_dir=tmp_path)
        assert mgr.read() == ""

    def test_get_context_wraps_content(self, tmp_path):
        mgr = SoulManager(data_dir=tmp_path)
        (tmp_path / "soul.md").write_text("Personality rules.", encoding="utf-8")
        ctx = mgr.get_context()
        assert "Personality" in ctx
        assert "Personality rules." in ctx

    def test_get_context_empty_if_missing(self, tmp_path):
        mgr = SoulManager(data_dir=tmp_path)
        assert mgr.get_context() == ""

    def test_get_context_respects_token_budget(self, tmp_path):
        mgr = SoulManager(data_dir=tmp_path)
        big_content = "word " * 500
        (tmp_path / "soul.md").write_text(big_content, encoding="utf-8")
        ctx = mgr.get_context(token_budget=50)
        assert len(ctx.split()) < 100

    def test_template_is_valid_markdown(self):
        assert "# Ares" in SOUL_TEMPLATE
        assert "## Personality" in SOUL_TEMPLATE
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd ares && python -m pytest tests/test_soul.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'ares.soul'`

- [ ] **Step 3: Write minimal implementation**

```python
# ares/soul.py
"""Soul manager: personality definition for Ares."""

from __future__ import annotations

from pathlib import Path

from ares.context_blend import truncate_to_tokens

SOUL_TEMPLATE = """# Ares — My AI Assistant

## Personality
- Concise, no fluff. Like Jarvis, not Alexa.
- Warm but efficient. Helpful, not chatty.
- When unsure, ask. Don't guess.

## Communication Style
- Use bullet points over paragraphs
- Lead with the answer, then explain if needed
- Match the user's energy — short questions get short answers

## Values
- Privacy first — everything stays local
- User control — ask before doing destructive things
- Honesty — say "I don't know" instead of making things up
"""


class SoulManager:
    """Manages the soul/personality file."""

    def __init__(self, data_dir: Path):
        self.soul_path = data_dir / "soul.md"

    def ensure_exists(self) -> None:
        """Create soul.md with template if it doesn't exist."""
        if not self.soul_path.exists():
            self.soul_path.write_text(SOUL_TEMPLATE, encoding="utf-8")

    def read(self) -> str:
        """Read soul content. Returns empty string if missing."""
        if not self.soul_path.exists():
            return ""
        return self.soul_path.read_text(encoding="utf-8").strip()

    def get_context(self, token_budget: int = 200) -> str:
        """Read soul and return as context block, truncated to budget."""
        content = self.read()
        if not content:
            return ""
        return truncate_to_tokens(
            f"## Ares Personality\n\n{content}", token_budget
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd ares && python -m pytest tests/test_soul.py -v`
Expected: All 8 tests PASS

- [ ] **Step 5: Commit**

```bash
git add ares/soul.py tests/test_soul.py
git commit -m "feat: add SoulManager for personality file"
```

---

## Task 3: Profile Manager

**Files:**
- Create: `ares/profile.py`
- Create: `tests/test_profile.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_profile.py
"""Tests for ProfileManager."""

from pathlib import Path

from ares.profile import ProfileManager, PROFILE_TEMPLATE


class TestProfileManager:
    def test_ensure_exists_creates_file(self, tmp_path):
        mgr = ProfileManager(data_dir=tmp_path)
        mgr.ensure_exists()
        assert (tmp_path / "profile.md").exists()

    def test_ensure_exists_does_not_overwrite(self, tmp_path):
        mgr = ProfileManager(data_dir=tmp_path)
        (tmp_path / "profile.md").write_text("my profile", encoding="utf-8")
        mgr.ensure_exists()
        assert (tmp_path / "profile.md").read_text(encoding="utf-8") == "my profile"

    def test_read_returns_content(self, tmp_path):
        mgr = ProfileManager(data_dir=tmp_path)
        (tmp_path / "profile.md").write_text("# About Me\nName: Alice", encoding="utf-8")
        result = mgr.read()
        assert "Alice" in result

    def test_read_returns_empty_if_missing(self, tmp_path):
        mgr = ProfileManager(data_dir=tmp_path)
        assert mgr.read() == ""

    def test_resolve_imports_inlines_file(self, tmp_path):
        mgr = ProfileManager(data_dir=tmp_path)
        ref_file = tmp_path / "about.md"
        ref_file.write_text("I like Python.", encoding="utf-8")
        content = f"# About Me\n@{ref_file}"
        result = mgr.resolve_imports(content)
        assert "I like Python." in result
        assert f"@{ref_file}" not in result

    def test_resolve_imports_handles_missing_file(self, tmp_path):
        mgr = ProfileManager(data_dir=tmp_path)
        content = "# About Me\n@/nonexistent/path.md"
        result = mgr.resolve_imports(content)
        assert "not found" in result.lower() or "could not read" in result.lower()

    def test_get_context_wraps_content(self, tmp_path):
        mgr = ProfileManager(data_dir=tmp_path)
        (tmp_path / "profile.md").write_text("Name: Bob", encoding="utf-8")
        ctx = mgr.get_context()
        assert "User Profile" in ctx
        assert "Bob" in ctx

    def test_get_context_empty_if_missing(self, tmp_path):
        mgr = ProfileManager(data_dir=tmp_path)
        assert mgr.get_context() == ""

    def test_get_context_resolves_imports(self, tmp_path):
        mgr = ProfileManager(data_dir=tmp_path)
        ref = tmp_path / "notes.md"
        ref.write_text("My notes here.", encoding="utf-8")
        (tmp_path / "profile.md").write_text(f"# Me\n@{ref}", encoding="utf-8")
        ctx = mgr.get_context()
        assert "My notes here." in ctx

    def test_get_context_respects_token_budget(self, tmp_path):
        mgr = ProfileManager(data_dir=tmp_path)
        big = "word " * 500
        (tmp_path / "profile.md").write_text(big, encoding="utf-8")
        ctx = mgr.get_context(token_budget=50)
        assert len(ctx.split()) < 100

    def test_template_is_valid_markdown(self):
        assert "# About Me" in PROFILE_TEMPLATE
        assert "## Preferences" in PROFILE_TEMPLATE
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd ares && python -m pytest tests/test_profile.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'ares.profile'`

- [ ] **Step 3: Write minimal implementation**

```python
# ares/profile.py
"""Profile manager: user identity and preferences."""

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

    def __init__(self, data_dir: Path):
        self.profile_path = data_dir / "profile.md"
        self.data_dir = data_dir

    def ensure_exists(self) -> None:
        """Create profile.md with template if it doesn't exist."""
        if not self.profile_path.exists():
            self.profile_path.write_text(PROFILE_TEMPLATE, encoding="utf-8")

    def read(self) -> str:
        """Read profile content. Returns empty string if missing."""
        if not self.profile_path.exists():
            return ""
        return self.profile_path.read_text(encoding="utf-8").strip()

    def resolve_imports(self, content: str) -> str:
        """Resolve @path/to/file references in profile content."""
        lines = content.split("\n")
        resolved = []
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("@") and not stripped.startswith("@@"):
                ref_path = Path(stripped[1:]).expanduser()
                if ref_path.is_file():
                    try:
                        imported = ref_path.read_text(encoding="utf-8").strip()
                        resolved.append(f"<!-- imported from {ref_path} -->")
                        resolved.append(imported)
                    except (OSError, UnicodeDecodeError):
                        resolved.append(f"<!-- could not read {ref_path} -->")
                else:
                    resolved.append(f"<!-- file not found: {ref_path} -->")
            else:
                resolved.append(line)
        return "\n".join(resolved)

    def get_context(self, token_budget: int = 400) -> str:
        """Read profile and return as context block, truncated to budget."""
        content = self.read()
        if not content:
            return ""
        content = self.resolve_imports(content)
        return truncate_to_tokens(
            f"## User Profile\n\n{content}", token_budget
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd ares && python -m pytest tests/test_profile.py -v`
Expected: All 12 tests PASS

- [ ] **Step 5: Commit**

```bash
git add ares/profile.py tests/test_profile.py
git commit -m "feat: add ProfileManager with @imports support"
```

---

## Task 4: Project Context Discovery

**Files:**
- Create: `ares/context.py`
- Create: `tests/test_context.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_context.py
"""Tests for ProjectContext."""

from pathlib import Path

from ares.context import ProjectContext, SCAN_TARGETS


class TestProjectContext:
    def test_discover_finds_claude_md(self, tmp_path):
        (tmp_path / "CLAUDE.md").write_text("# Project\nUse pytest.", encoding="utf-8")
        ctx = ProjectContext(cwd=tmp_path)
        found = ctx.discover()
        names = [f[0] for f in found]
        assert "CLAUDE.md" in names

    def test_discover_finds_readme(self, tmp_path):
        (tmp_path / "README.md").write_text("# My Project\nA cool tool.", encoding="utf-8")
        ctx = ProjectContext(cwd=tmp_path)
        found = ctx.discover()
        names = [f[0] for f in found]
        assert "README.md" in names

    def test_discover_finds_pyproject(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text('[project]\nname = "ares"', encoding="utf-8")
        ctx = ProjectContext(cwd=tmp_path)
        found = ctx.discover()
        names = [f[0] for f in found]
        assert "pyproject.toml" in names

    def test_discover_finds_package_json(self, tmp_path):
        (tmp_path / "package.json").write_text('{"name": "myapp"}', encoding="utf-8")
        ctx = ProjectContext(cwd=tmp_path)
        found = ctx.discover()
        names = [f[0] for f in found]
        assert "package.json" in names

    def test_discover_returns_empty_if_no_files(self, tmp_path):
        ctx = ProjectContext(cwd=tmp_path)
        assert ctx.discover() == []

    def test_discover_respects_max_files(self, tmp_path):
        (tmp_path / "CLAUDE.md").write_text("claude", encoding="utf-8")
        (tmp_path / "AGENTS.md").write_text("agents", encoding="utf-8")
        (tmp_path / "README.md").write_text("readme", encoding="utf-8")
        ctx = ProjectContext(cwd=tmp_path)
        found = ctx.discover(max_files=2)
        assert len(found) == 2

    def test_discover_truncates_large_files(self, tmp_path):
        big = "\n".join([f"line {i}" for i in range(300)])
        (tmp_path / "CLAUDE.md").write_text(big, encoding="utf-8")
        ctx = ProjectContext(cwd=tmp_path)
        found = ctx.discover()
        content = found[0][1]
        assert "more lines" in content

    def test_discover_skips_binary_files(self, tmp_path):
        (tmp_path / "README.md").write_bytes(b"\x00\x01\x02\x03")
        ctx = ProjectContext(cwd=tmp_path)
        found = ctx.discover()
        assert len(found) == 0

    def test_get_context_wraps_content(self, tmp_path):
        (tmp_path / "README.md").write_text("# My Project", encoding="utf-8")
        ctx = ProjectContext(cwd=tmp_path)
        result = ctx.get_context()
        assert "Project Context" in result
        assert "My Project" in result

    def test_get_context_empty_if_no_files(self, tmp_path):
        ctx = ProjectContext(cwd=tmp_path)
        assert ctx.get_context() == ""

    def test_get_context_respects_token_budget(self, tmp_path):
        big = "word " * 500
        (tmp_path / "README.md").write_text(big, encoding="utf-8")
        ctx = ProjectContext(cwd=tmp_path)
        result = ctx.get_context(token_budget=50)
        assert len(result.split()) < 100

    def test_scan_targets_are_valid(self):
        assert len(SCAN_TARGETS) > 0
        for name, max_lines in SCAN_TARGETS:
            assert isinstance(name, str)
            assert isinstance(max_lines, int)
            assert max_lines > 0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd ares && python -m pytest tests/test_context.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'ares.context'`

- [ ] **Step 3: Write minimal implementation**

```python
# ares/context.py
"""Project context discovery: scan CWD for project metadata files."""

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
    """Auto-discovers project metadata from the working directory."""

    def __init__(self, cwd: Path | None = None):
        self.cwd = cwd or Path.cwd()

    def discover(self, max_files: int = 2) -> list[tuple[str, str]]:
        """Scan CWD for project files. Returns [(filename, content)] pairs."""
        found = []
        for filename, max_lines in SCAN_TARGETS:
            if len(found) >= max_files:
                break
            path = self.cwd / filename
            if not path.exists():
                continue
            try:
                raw = path.read_bytes()
                if b"\x00" in raw[:1024]:
                    continue  # skip binary files
                text = raw.decode("utf-8")
                lines = text.splitlines()
                if len(lines) > max_lines:
                    lines = lines[:max_lines]
                    lines.append(f"... ({len(text.splitlines()) - max_lines} more lines)")
                content = "\n".join(lines)
                found.append((filename, content))
            except (OSError, UnicodeDecodeError):
                continue
        return found

    def get_context(self, token_budget: int = 400) -> str:
        """Return project context block, truncated to budget."""
        files = self.discover()
        if not files:
            return ""
        parts = ["## Current Project Context"]
        for filename, content in files:
            parts.append(f"\n### {filename}\n\n{content}")
        return truncate_to_tokens("\n".join(parts), token_budget)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd ares && python -m pytest tests/test_context.py -v`
Expected: All 13 tests PASS

- [ ] **Step 5: Commit**

```bash
git add ares/context.py tests/test_context.py
git commit -m "feat: add ProjectContext for CWD metadata discovery"
```

---

## Task 5: Config + Model Updates

**Files:**
- Modify: `ares/models.py`
- Modify: `ares/config.py`

- [ ] **Step 1: Add new fields to AppConfig**

In `ares/models.py`, add these fields to the `AppConfig` class:

```python
class AppConfig(BaseModel):
    model: str = "deepseek-v4-flash-free"
    api_key: str = ""
    api_base_url: str = "https://opencode.ai/zen/v1"
    max_context_messages: int = 20
    max_memory_retrieval: int = 5
    data_dir: str = "~/.ares/data"
    embedding_model: str = "sentence-transformers/all-MiniLM-L6-v2"
    embedding_backend: str = "onnx"
    embedding_provider: str = "CPUExecutionProvider"
    embedding_file_name: str = ""
    reminder_poll_seconds: int = 30
    enable_desktop_notifications: bool = True
    session_summary_messages: int = 2
    web_search_provider: str = "auto"
    tavily_api_key: str = ""
    tavily_search_depth: str = "basic"
    # v3: proactive context
    soul_path: str = ""           # defaults to data_dir / "soul.md"
    profile_path: str = ""        # defaults to data_dir / "profile.md"
    project_context_enabled: bool = True
    context_token_budget: int = 2000
    project_context_max_files: int = 2
```

- [ ] **Step 2: Run existing tests to verify nothing breaks**

Run: `cd ares && python -m pytest tests/ -v`
Expected: All existing tests PASS (new fields have defaults, so no breakage)

- [ ] **Step 3: Commit**

```bash
git add ares/models.py
git commit -m "feat: add soul/profile/project_context config fields to AppConfig"
```

---

## Task 6: Wire Into Agent

**Files:**
- Modify: `ares/agent.py`
- Modify: `ares/prompts.py` (update SYSTEM_PROMPT)

- [ ] **Step 1: Update prompts.py — remove old build_context_prompt, update SYSTEM_PROMPT**

In `ares/prompts.py`, remove the old `build_context_prompt` function entirely (it's now in `context_blend.py`). Update the SYSTEM_PROMPT to reference soul, profile, and project context:

```python
# ares/prompts.py

SYSTEM_PROMPT = """You are Ares, a personal AI assistant living in the user's terminal.
You are like Jarvis from Iron Man — you know the user, remember their preferences,
and help them with daily tasks through natural language.

## Your Capabilities

You have access to these tools:
- **store_memory**: Save facts, preferences, and information the user wants you to remember.
- **search_memory**: Retrieve previously stored information about the user.
- **update_memory**: Correct or enrich an existing memory.
- **delete_memory**: Forget a stored memory by ID.
- **create_task**: Create reminders, to-dos, and tasks.
- **list_tasks**: Show the user their pending tasks.
- **search_tasks**: Find matching tasks.
- **complete_task**: Mark a task done.
- **cancel_task**: Cancel a task.
- **get_due_soon**: Show tasks due soon.
- **export_data**: Export local memories, tasks, and conversations to JSON.
- **web_search**: Search the web for current information.
- **read_file**: Read the contents of a local file.
- **search_files**: Search local files by name or content.
- **list_directory**: List local directory contents.

## Your Personality

Your personality is defined in the soul file provided in context.
Follow those guidelines for tone, communication style, and values.

## About the User

The user's profile is provided in context. Use it to personalize responses:
- Use their name when addressing them
- Reference their projects and goals when relevant
- Respect their stated preferences

## Project Context

When project context is provided, you're working within that codebase.
Follow its conventions, use its tools, and reference its structure.

## Web Search

Use `web_search` when:
- The user asks about current events, news, weather, or recent developments
- The user asks a factual question you're unsure about
- The user asks "what is [something]" and you might not have current info

Do NOT search for:
- Things you already know from memory
- Personal questions about the user
- Tasks/reminders (use tools for those)

## File System Access

You can read files and search the user's file system.

- Use `read_file` when the user references a specific file or wants to see file contents
- Use `search_files` when the user wants to find files by name or content
- Use `list_directory` when the user wants to explore a directory

Rules:
- Show file paths relative to the user's home directory or current workspace when possible
- When reading large files, read only the relevant section
- When searching, start broad and narrow down
- Never modify files — you can only read

## Your Rules

1. **Be concise.** You're a terminal CLI tool — keep responses brief and useful.
2. **Remember everything.** When the user tells you something about themselves, store it.
3. **Use tools when appropriate.** Don't just say "I'll remember that" — actually call store_memory.
4. **Be proactive.** If the user mentions a deadline, offer to create a task.
5. **Don't fabricate.** Never make up facts about the user. Only use what they've told you.
6. **Be warm but efficient.** Like a good assistant — helpful, not chatty.
7. **Respect user control.** If the user asks you to forget or correct a memory, use the memory tools.

## Context

You will receive layered context at the start of each conversation:
- Your personality (soul)
- User profile and preferences
- Project context (if in a project directory)
- Relevant memories
- Pending tasks

Use this context to provide personalized, contextual responses.

## Privacy

All user data is stored locally on their machine. Never suggest sending personal
data to external services. If a user asks about data privacy, explain that everything
is local."""


WELCOME_MESSAGE = """Welcome back! I'm **Ares**, your personal AI assistant.
Type your message or `/help` for available commands.

Model: {model} | Memory: {memory_count} facts stored"""

FIRST_RUN_MESSAGE = """Welcome to **Ares**! I'm your personal AI assistant — think of me as your terminal Jarvis.

**Quick start:**
- Just talk to me naturally: "remember that I prefer dark mode"
- Ask me anything: "what do you know about me?"
- Create tasks: "remind me to call mom tomorrow at 3pm"
- Type `/help` for all commands

**Customize me:**
- `/soul edit` — change my personality and communication style
- `/profile edit` — tell me about yourself so I can help better

**Privacy note:** I use free AI models that may log data for model improvement.
Your personal data (memories, tasks) stays 100% local on your machine.
If you want stronger privacy, you can switch to a paid model with `/model`.

Let's get started! What's on your mind?"""
```

- [ ] **Step 2: Update agent.py — init new managers, update get_context**

```python
# ares/agent.py — updated sections

import json
from typing import AsyncIterator

from ares.memory import MemoryStore
from ares.tasks import TaskStore
from ares.conversations import ConversationStore
from ares.tools import ToolExecutor, get_tool_definitions
from ares.llm import LLMClient
from ares.prompts import SYSTEM_PROMPT
from ares.context_blend import build_context_prompt
from ares.soul import SoulManager
from ares.profile import ProfileManager
from ares.context import ProjectContext


class Agent:
    """The core agent that orchestrates LLM calls and tool execution."""

    def __init__(
        self,
        memory_store: MemoryStore,
        task_store: TaskStore,
        conversation_store: ConversationStore | None = None,
        api_key: str = "",
        base_url: str = "",
        model: str = "",
    ):
        self.memory_store = memory_store
        self.task_store = task_store
        self.conversation_store = conversation_store
        self.tool_executor = ToolExecutor(
            memory_store=memory_store,
            task_store=task_store,
            conversation_store=conversation_store,
        )
        self.tools = get_tool_definitions()

        kwargs = {}
        if api_key:
            kwargs["api_key"] = api_key
        if base_url:
            kwargs["base_url"] = base_url
        if model:
            kwargs["model"] = model
        self.llm = LLMClient(**kwargs)
        self.tool_executor.config = self.llm.config

        # v3: proactive context
        from ares.config import load_config
        config = load_config()
        from pathlib import Path
        data_dir = Path(config.data_dir).expanduser()
        data_dir.mkdir(parents=True, exist_ok=True)

        self.soul_manager = SoulManager(data_dir=data_dir)
        self.profile_manager = ProfileManager(data_dir=data_dir)
        self.project_context = ProjectContext()

        # Ensure files exist on first run
        self.soul_manager.ensure_exists()
        self.profile_manager.ensure_exists()

    def build_messages(self, user_input: str, conversation_history: list[dict],
                       context: str = "") -> list[dict]:
        """Build the message list for the LLM."""
        system_content = SYSTEM_PROMPT
        if context:
            system_content += f"\n\n## Current Context\n{context}"

        messages = [{"role": "system", "content": system_content}]
        messages.extend(conversation_history)
        messages.append({"role": "user", "content": user_input})
        return messages

    def get_context(self, user_input: str) -> str:
        """Build full context: soul + profile + project + memories + tasks."""
        soul_ctx = self.soul_manager.get_context()
        profile_ctx = self.profile_manager.get_context()
        project_ctx = self.project_context.get_context()
        memories = self.memory_store.search(user_input, limit=5)
        tasks = self.task_store.list_pending()
        summaries = []
        if self.conversation_store is not None:
            summaries = self.conversation_store.get_recent_summaries(limit=5)

        return build_context_prompt(
            soul_context=soul_ctx,
            profile_context=profile_ctx,
            project_context=project_ctx,
            memories=memories,
            tasks=tasks,
            conversation_summaries=summaries,
        )

    # ... rest of Agent unchanged (set_model, process_tool_calls, _tool_messages, run, run_stream, close)
```

- [ ] **Step 3: Run existing tests to verify nothing breaks**

Run: `cd ares && python -m pytest tests/test_agent.py tests/test_tools.py -v`
Expected: All existing tests PASS

- [ ] **Step 4: Commit**

```bash
git add ares/agent.py ares/prompts.py
git commit -m "feat: wire soul/profile/project context into Agent and system prompt"
```

---

## Task 7: CLI Commands (/soul, /profile, /context)

**Files:**
- Modify: `ares/cli.py`

- [ ] **Step 1: Add imports and update COMPLETER**

In `ares/cli.py`, add imports at the top:

```python
from ares.soul import SoulManager
from ares.profile import ProfileManager
from ares.context import ProjectContext
```

Update the COMPLETER to include new commands:

```python
COMPLETER = WordCompleter([
    "/help", "/tasks", "/memory", "/model", "/clear",
    "/forget", "/export", "/import", "/reset", "/exit",
    "/soul", "/profile", "/context",
], ignore_case=True)
```

- [ ] **Step 2: Add soul/profile/context managers to AresCLI.__init__**

In `AresCLI.__init__`, after the existing stores are created, add:

```python
# v3: proactive context managers
from pathlib import Path as _Path
_data_dir = _Path(self.config.data_dir).expanduser()
self.soul_manager = SoulManager(data_dir=_data_dir)
self.profile_manager = ProfileManager(data_dir=_data_dir)
self.project_context = ProjectContext()
```

- [ ] **Step 3: Add /soul, /profile, /context to _handle_command**

Add these elif blocks in `_handle_command`, before the `else:` clause:

```python
        elif command == "/soul":
            if not arg or arg == "show":
                content = self.soul_manager.read()
                if content:
                    self.console.print(Panel(
                        Markdown(content),
                        title="🎭 Soul — Ares Personality",
                        border_style="bright_magenta",
                        padding=(0, 1),
                    ))
                else:
                    self.console.print("[dim]No soul file found. One will be created on first run.[/dim]")
            elif arg == "edit":
                self._edit_file(self.soul_manager.soul_path, "soul")
            else:
                self.console.print("[red]Usage: /soul [show|edit][/red]")

        elif command == "/profile":
            if not arg or arg == "show":
                content = self.profile_manager.read()
                if content:
                    self.console.print(Panel(
                        Markdown(content),
                        title="👤 Profile — User Identity",
                        border_style="bright_green",
                        padding=(0, 1),
                    ))
                else:
                    self.console.print("[dim]No profile file found. One will be created on first run.[/dim]")
            elif arg == "edit":
                self._edit_file(self.profile_manager.profile_path, "profile")
            else:
                self.console.print("[red]Usage: /profile [show|edit][/red]")

        elif command == "/context":
            from ares.context_blend import build_context_prompt
            soul_ctx = self.soul_manager.get_context()
            profile_ctx = self.profile_manager.get_context()
            project_ctx = self.project_context.get_context()
            memories = self.memory_store.get_recent(limit=5)
            tasks = self.task_store.list_pending()
            context_str = build_context_prompt(
                soul_context=soul_ctx,
                profile_context=profile_ctx,
                project_context=project_ctx,
                memories=memories,
                tasks=tasks,
            )
            if context_str:
                self.console.print(Panel(
                    Markdown(context_str),
                    title="📡 Active Context",
                    border_style="bright_cyan",
                    padding=(0, 1),
                ))
            else:
                self.console.print("[dim]No context active.[/dim]")
```

- [ ] **Step 4: Add _edit_file helper method**

Add this method to `AresCLI` class (before `_handle_command`):

```python
    def _edit_file(self, file_path: Path, name: str) -> None:
        """Open a file in the user's editor, or print the path."""
        import os
        import subprocess
        import sys as _sys

        # Ensure file exists
        if not file_path.exists():
            self.console.print(f"[yellow]Creating {name} file...[/yellow]")
            if name == "soul":
                from ares.soul import SOUL_TEMPLATE
                file_path.write_text(SOUL_TEMPLATE, encoding="utf-8")
            else:
                from ares.profile import PROFILE_TEMPLATE
                file_path.write_text(PROFILE_TEMPLATE, encoding="utf-8")

        editor = os.environ.get("EDITOR") or os.environ.get("VISUAL")
        if editor:
            self.console.print(f"[dim]Opening {file_path} in {editor}...[/dim]")
            try:
                subprocess.run([editor, str(file_path)], check=False)
            except FileNotFoundError:
                self.console.print(f"[red]Editor '{editor}' not found. File at: {file_path}[/red]")
        elif _sys.platform == "win32":
            self.console.print(f"[dim]Opening {file_path}...[/dim]")
            try:
                os.startfile(str(file_path))
            except OSError:
                self.console.print(f"[yellow]Could not open editor. Edit manually: {file_path}[/yellow]")
        else:
            self.console.print(f"[yellow]No $EDITOR set. Edit manually: {file_path}[/yellow]")
```

- [ ] **Step 5: Update /help table**

In the `/help` command handler, add rows for the new commands:

```python
            table.add_row("/soul [show|edit]", "View or edit Ares' personality")
            table.add_row("/profile [show|edit]", "View or edit your profile")
            table.add_row("/context", "Show active context for this session")
```

- [ ] **Step 6: Run existing tests to verify nothing breaks**

Run: `cd ares && python -m pytest tests/test_cli.py -v`
Expected: All existing tests PASS

- [ ] **Step 7: Commit**

```bash
git add ares/cli.py
git commit -m "feat: add /soul, /profile, /context CLI commands"
```

---

## Task 8: Integration Test + Final Verification

**Files:**
- Create: `tests/test_integration_context.py`

- [ ] **Step 1: Write integration test**

```python
# tests/test_integration_context.py
"""Integration test: full context pipeline end-to-end."""

from pathlib import Path

from ares.soul import SoulManager
from ares.profile import ProfileManager
from ares.context import ProjectContext
from ares.context_blend import build_context_prompt


def test_full_context_pipeline(tmp_path):
    """End-to-end: soul + profile + project + memories + tasks → context string."""
    # Setup soul
    soul_mgr = SoulManager(data_dir=tmp_path)
    soul_mgr.ensure_exists()
    soul_mgr.soul_path.write_text(
        "## Personality\n- Be concise.\n- Be helpful.", encoding="utf-8"
    )

    # Setup profile
    profile_mgr = ProfileManager(data_dir=tmp_path)
    profile_mgr.ensure_exists()
    profile_mgr.profile_path.write_text(
        "## Identity\n- Name: Alice\n\n## Preferences\n- Coding: Python", encoding="utf-8"
    )

    # Setup project context
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    (project_dir / "README.md").write_text("# My Project\nA Python tool.", encoding="utf-8")
    proj_ctx = ProjectContext(cwd=project_dir)

    # Mock memories and tasks
    memories = [
        {"fact_id": 1, "fact_text": "Alice likes coffee", "category": "preference", "importance": 0.8},
        {"fact_id": 2, "fact_text": "Works on Ares project", "category": "project", "importance": 0.6},
    ]
    tasks = [
        {"title": "Write implementation plan", "due": "2026-06-19"},
    ]

    # Build context
    context = build_context_prompt(
        soul_context=soul_mgr.get_context(),
        profile_context=profile_mgr.get_context(),
        project_context=proj_ctx.get_context(),
        memories=memories,
        tasks=tasks,
    )

    # Verify all layers present
    assert "Alice" in context
    assert "coffee" in context
    assert "My Project" in context
    assert "Write implementation plan" in context
    assert "Personality" in context

    # Verify priority ordering
    soul_pos = context.index("Personality")
    profile_pos = context.index("Alice")
    project_pos = context.index("My Project")
    assert soul_pos < profile_pos < project_pos


def test_context_without_project(tmp_path):
    """Context works fine when no project files exist."""
    soul_mgr = SoulManager(data_dir=tmp_path)
    soul_mgr.ensure_exists()
    profile_mgr = ProfileManager(data_dir=tmp_path)
    profile_mgr.ensure_exists()

    context = build_context_prompt(
        soul_context=soul_mgr.get_context(),
        profile_context=profile_mgr.get_context(),
    )

    assert "Personality" in context
    assert "User Profile" in context
```

- [ ] **Step 2: Run integration test**

Run: `cd ares && python -m pytest tests/test_integration_context.py -v`
Expected: PASS

- [ ] **Step 3: Run full test suite**

Run: `cd ares && python -m pytest tests/ -v`
Expected: All tests PASS (existing + new)

- [ ] **Step 4: Commit**

```bash
git add tests/test_integration_context.py
git commit -m "test: add integration test for full context pipeline"
```

---

## Summary

| Task | What it builds | Files touched |
|------|---------------|---------------|
| 1 | Token utilities + blending logic | `context_blend.py`, `test_context_blend.py` |
| 2 | Soul manager | `soul.py`, `test_soul.py` |
| 3 | Profile manager with @imports | `profile.py`, `test_profile.py` |
| 4 | Project context discovery | `context.py`, `test_context.py` |
| 5 | Config additions | `models.py` |
| 6 | Agent integration + prompt update | `agent.py`, `prompts.py` |
| 7 | CLI commands | `cli.py` |
| 8 | Integration test + final verification | `test_integration_context.py` |

**Total:** 5 new files, 4 modified files, 5 new test files, 1 integration test.
