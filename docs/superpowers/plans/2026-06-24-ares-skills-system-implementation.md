# Ares Skills System — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a skills system to Ares — letting the assistant load domain-specific workflow instructions from markdown files (SKILL.md + optional scripts/) without editing Python code.

**Architecture:** Three-tier progressive disclosure: metadata (~50 tokens/skill) in system prompt at session start via `SkillManager.catalog()`, full SKILL.md body loaded on demand via `read_skill` tool, scripts/resources loaded only when instructions reference them via existing tools. Follows the Agent Skills specification.

**Tech Stack:** Python stdlib + PyYAML (already installed). No new dependencies.

**V1 Risk Mitigation:** Only scan `~/.ares/skills/` and `~/.agents/skills/` — project-level (`<cwd>/.agents/skills/`) is skipped for security.

---

## File Map

| File | Action | Purpose |
|------|--------|---------|
| `ares/skill_manager.py` | **Create** | Skill dataclass + SkillManager (discovery, parsing, registry, activation tracking) |
| `ares/tools/definitions.py` | **Modify** | Add `read_skill` tool definition (after line 465) |
| `ares/tools/executor.py` | **Modify** | Add `_read_skill` handler method + wire `skill_manager` attribute |
| `ares/agent.py` | **Modify** | Init SkillManager, wire into tool_executor, inject catalog in `build_messages()` |
| `tests/test_skill_manager.py` | **Create** | Tests for SkillManager + read_skill handler |
| `tests/test_agent.py` | **Modify** | Add tests for catalog injection in `build_messages()` |
| `tests/test_agent.py` | **Modify** | Add tests for catalog injection in `build_messages()` |
| `ares/prompts.py` | **No code change** | Document that the skills section is dynamically injected via `build_messages()` |

---

### Task 1: Write tests for SkillManager and read_skill handler

**Files:**
- Create: `tests/test_skill_manager.py`

- [ ] **Step 1: Create test file for SkillManager and handler tests**

```python
"""Tests for SkillManager and read_skill tool handler."""

from pathlib import Path

import pytest

from ares.skill_manager import Skill, SkillManager


def _write_skill(base_dir: str, name: str, description: str, body: str = "## Instructions\nDo the thing.") -> Path:
    """Helper: create a SKILL.md under base_dir/name/."""
    skill_dir = Path(base_dir) / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    content = f"---\nname: {name}\ndescription: {description}\n---\n{body}"
    (skill_dir / "SKILL.md").write_text(content, encoding="utf-8")
    return skill_dir


def _write_skill_with_frontmatter(base_dir: str, name: str, frontmatter: str, body: str = "## Body") -> Path:
    """Helper: create a SKILL.md with custom frontmatter lines."""
    skill_dir = Path(base_dir) / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    content = f"---\n{frontmatter}\n---\n{body}"
    (skill_dir / "SKILL.md").write_text(content, encoding="utf-8")
    return skill_dir


class TestSkillManagerDiscovery:

    def test_scan_finds_skills(self, tmp_path):
        """Scans directory with SKILL.md files, builds registry."""
        _write_skill(str(tmp_path), "pdf-processing", "Extract text from PDFs")
        _write_skill(str(tmp_path), "code-review", "Review code for issues")

        sm = SkillManager()
        sm._scan_dir(tmp_path)
        names = sm.list_names()

        assert "pdf-processing" in names
        assert "code-review" in names

    def test_scan_ignores_no_skill_md(self, tmp_path):
        """Directories without SKILL.md are skipped."""
        (tmp_path / "empty-dir").mkdir(parents=True)
        (tmp_path / "random-file.txt").write_text("hello", encoding="utf-8")

        sm = SkillManager()
        sm._scan_dir(tmp_path)

        assert sm.list_names() == []

    def test_scan_skips_hidden_and_system_dirs(self, tmp_path):
        """Hidden dirs, .git, node_modules, __pycache__ are skipped."""
        for d in (".git", "node_modules", "__pycache__", ".hidden"):
            (tmp_path / d).mkdir(parents=True)
            _write_skill(str(tmp_path / d), "test-skill", "should be skipped")

        sm = SkillManager()
        sm._scan_dir(tmp_path)

        assert sm.list_names() == []

    def test_name_priority_first_found_wins(self, tmp_path):
        """First directory scanned takes priority on name collision."""
        first = tmp_path / "first"
        second = tmp_path / "second"
        first.mkdir()
        second.mkdir()
        _write_skill(str(first), "dup-skill", "First version")
        _write_skill(str(second), "dup-skill", "Second version")

        sm = SkillManager()
        sm._scan_dir(first)
        sm._scan_dir(second)

        skill = sm.get("dup-skill")
        assert skill is not None
        assert skill.description == "First version"

    def test_max_skills_cap(self, tmp_path):
        """Scan caps at 100 skills."""
        for i in range(105):
            _write_skill(str(tmp_path), f"skill-{i:03d}", f"Skill number {i}")
        sm = SkillManager()
        sm._scan_dir(tmp_path)
        assert len(sm.list_names()) <= 100

    def test_scan_without_skills_returns_empty(self):
        """Scanning a nonexistent directory doesn't crash."""
        sm = SkillManager()
        sm._scan_dir(Path("/nonexistent/path"))
        assert sm.list_names() == []


class TestSkillManagerParsing:

    def test_parse_frontmatter(self, tmp_path):
        """Extracts name, description from YAML frontmatter."""
        _write_skill(str(tmp_path), "my-skill", "Does something useful",
                     body="# Steps\n1. Do it\n2. Verify it")
        sm = SkillManager()
        sm._scan_dir(tmp_path)

        skill = sm.get("my-skill")
        assert skill is not None
        assert skill.name == "my-skill"
        assert skill.description == "Does something useful"

    def test_parse_body(self, tmp_path):
        """Returns full SKILL.md content via full_body()."""
        _write_skill(str(tmp_path), "test-skill", "A test",
                     body="# Steps\n1. Run script\n2. Check output")
        sm = SkillManager()
        sm._scan_dir(tmp_path)

        skill = sm.get("test-skill")
        assert skill is not None
        body = skill.full_body()
        assert "name: test-skill" in body
        assert "description: A test" in body
        assert "1. Run script" in body
        assert "2. Check output" in body

    def test_lenient_yaml(self, tmp_path):
        """Handles unquoted colons in descriptions."""
        frontmatter = (
            'name: pdf-tools\n'
            'description: Extract text from PDFs. Merge: combine multiple PDFs into one.\n'
        )
        _write_skill_with_frontmatter(str(tmp_path), "pdf-tools", frontmatter)

        sm = SkillManager()
        sm._scan_dir(tmp_path)

        skill = sm.get("pdf-tools")
        assert skill is not None
        assert "Extract text from PDFs" in skill.description
        assert "Merge: combine" in skill.description

    def test_skips_skill_without_description(self, tmp_path):
        """Skills without description are skipped."""
        (tmp_path / "no-desc").mkdir(parents=True)
        content = "---\nname: no-desc\n---\n## Body"
        (tmp_path / "no-desc" / "SKILL.md").write_text(content, encoding="utf-8")

        sm = SkillManager()
        sm._scan_dir(tmp_path)
        assert sm.get("no-desc") is None

    def test_skips_skill_without_name(self, tmp_path):
        """Skills without name are skipped."""
        (tmp_path / "no-name").mkdir(parents=True)
        content = "---\ndescription: something\n---\n## Body"
        (tmp_path / "no-name" / "SKILL.md").write_text(content, encoding="utf-8")

        sm = SkillManager()
        sm._scan_dir(tmp_path)
        assert sm.list_names() == []

    def test_skips_file_without_frontmatter(self, tmp_path):
        """File without opening --- is skipped."""
        (tmp_path / "bad-skill").mkdir(parents=True)
        (tmp_path / "bad-skill" / "SKILL.md").write_text(
            "Just markdown without frontmatter", encoding="utf-8"
        )
        sm = SkillManager()
        sm._scan_dir(tmp_path)
        assert sm.list_names() == []


class TestSkillManagerCatalog:

    def test_catalog_output(self, tmp_path):
        """Returns formatted XML with correct structure."""
        _write_skill(str(tmp_path), "pdf-tools", "Work with PDF files")
        _write_skill(str(tmp_path), "code-review", "Review code quality")

        sm = SkillManager()
        sm._scan_dir(tmp_path)
        catalog = sm.catalog()

        assert "<available_skills>" in catalog
        assert "</available_skills>" in catalog
        assert "<skill>" in catalog
        assert "<name>code-review</name>" in catalog
        assert "<description>Review code quality</description>" in catalog
        assert "<name>pdf-tools</name>" in catalog
        assert "<description>Work with PDF files</description>" in catalog

    def test_catalog_empty(self):
        """Returns empty string when no skills installed."""
        sm = SkillManager()
        assert sm.catalog() == ""


class TestSkillManagerGet:

    def test_get_skill(self, tmp_path):
        """Returns Skill record with lazy body loading."""
        _write_skill(str(tmp_path), "test-skill", "A test skill",
                     body="# Steps\n1. Do X\n2. Verify Y")
        sm = SkillManager()
        sm._scan_dir(tmp_path)

        skill = sm.get("test-skill")
        assert skill is not None
        assert skill.name == "test-skill"
        assert skill.description == "A test skill"

        # Body is loaded lazily
        body = skill.full_body()
        assert "1. Do X" in body
        assert "2. Verify Y" in body

    def test_get_unknown(self):
        """Returns None for nonexistent skill."""
        sm = SkillManager()
        assert sm.get("nonexistent") is None

    def test_list_resources(self, tmp_path):
        """list_resources shows scripts/, references/, assets/ contents."""
        skill_dir = _write_skill(str(tmp_path), "data-sync", "Sync data")
        (skill_dir / "scripts").mkdir()
        (skill_dir / "scripts" / "sync.py").write_text("print('sync')", encoding="utf-8")
        (skill_dir / "references").mkdir()
        (skill_dir / "references" / "api.md").write_text("# API docs", encoding="utf-8")
        (skill_dir / "assets").mkdir()
        (skill_dir / "assets" / "template.json").write_text("{}", encoding="utf-8")

        sm = SkillManager()
        sm._scan_dir(tmp_path)
        skill = sm.get("data-sync")

        resources = skill.list_resources()
        assert "scripts/" in resources
        assert "sync.py" in resources
        assert "references/" in resources
        assert "api.md" in resources
        assert "assets/" in resources
        assert "template.json" in resources

    def test_list_resources_no_dirs(self, tmp_path):
        """list_resources returns empty string when no subdirectories exist."""
        _write_skill(str(tmp_path), "simple-skill", "Simple")
        sm = SkillManager()
        sm._scan_dir(tmp_path)
        skill = sm.get("simple-skill")
        assert skill.list_resources() == ""


class TestSkillManagerActivation:

    def test_activation_dedup(self, tmp_path):
        """is_activated() returns True after activate()."""
        _write_skill(str(tmp_path), "test-skill", "A test")
        sm = SkillManager()
        sm._scan_dir(tmp_path)

        assert sm.is_activated("test-skill") is False
        sm.activate("test-skill")
        assert sm.is_activated("test-skill") is True

    def test_activate_nonexistent_does_not_error(self):
        """Activate for a name not in registry does not raise."""
        sm = SkillManager()
        sm.activate("nobody")  # should not raise
        assert sm.is_activated("nobody") is False


class TestReadSkillHandler:
    """Tests for the read_skill handler in ToolExecutor."""

    @pytest.fixture
    def executor(self, tmp_path):
        """Create a ToolExecutor with a SkillManager containing one skill."""
        from ares.tools.executor import ToolExecutor
        from ares.memory import MemoryStore
        from ares.tools.tasks import TaskStore
        from ares.conversations import ConversationStore

        _write_skill(str(tmp_path), "test-skill", "A test skill",
                     body="# Steps\n1. Do the thing\n2. Verify")

        skill_mgr = SkillManager()
        skill_mgr._scan_dir(tmp_path)

        mem = MemoryStore(db_path=str(tmp_path / "mem.db"))
        task = TaskStore(db_path=str(tmp_path / "tasks.db"))
        conv = ConversationStore(db_path=tmp_path / "conv.db")

        ex = ToolExecutor(memory_store=mem, task_store=task, conversation_store=conv)
        ex.skill_manager = skill_mgr
        return ex

    def test_read_skill_handler_found(self, executor):
        """Dispatcher returns SKILL.md content wrapped in <skill> tags."""
        result = executor.execute("read_skill", {"name": "test-skill"})
        assert "<skill name=\"test-skill\">" in result
        assert "</skill>" in result
        assert "1. Do the thing" in result
        assert "Skill directory:" in result

    def test_read_skill_handler_not_found(self, executor):
        """Unknown skill returns error with available names."""
        result = executor.execute("read_skill", {"name": "nonexistent"})
        assert "not found" in result.lower()
        assert "test-skill" in result  # suggests available names

    def test_read_skill_handler_activates_skill(self, executor):
        """Calling read_skill marks the skill as activated."""
        executor.execute("read_skill", {"name": "test-skill"})
        assert executor.skill_manager.is_activated("test-skill") is True

    def test_read_skill_handler_no_manager(self, tmp_path):
        """Gracefully handles missing skill_manager."""
        from ares.tools.executor import ToolExecutor
        from ares.memory import MemoryStore
        from ares.tools.tasks import TaskStore

        mem = MemoryStore(db_path=str(tmp_path / "mem.db"))
        task = TaskStore(db_path=str(tmp_path / "tasks.db"))
        ex = ToolExecutor(memory_store=mem, task_store=task)

        result = ex.execute("read_skill", {"name": "anything"})
        assert "not available" in result.lower()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_skill_manager.py -v --tb=short 2>&1 | head -50`
Expected: Most tests FAIL with `ModuleNotFoundError: No module named 'ares.skill_manager'` (the module doesn't exist yet).

- [ ] **Step 3: Commit**

```bash
git add tests/test_skill_manager.py
git commit -m "test: add failing tests for SkillManager and read_skill handler"
```

---

### Task 2: Implement SkillManager module

**Files:**
- Create: `ares/skill_manager.py`

- [ ] **Step 1: Create the Skill dataclass and SkillManager class**

```python
"""Skill discovery, parsing, registry, and activation tracking.

Follows the Agent Skills specification (https://agentskills.io/specification).
"""

from __future__ import annotations

import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


@dataclass
class Skill:
    """A single skill loaded from a SKILL.md file."""

    name: str
    description: str
    base_dir: Path
    _body: str | None = field(default=None, repr=False)

    def full_body(self) -> str:
        """Return SKILL.md content (frontmatter + body). Loaded lazily and cached."""
        if self._body is None:
            self._body = (self.base_dir / "SKILL.md").read_text(encoding="utf-8")
        return self._body

    def list_resources(self) -> str:
        """List files in scripts/, references/, assets/ subdirectories."""
        lines: list[str] = []
        for subdir in ("scripts", "references", "assets"):
            d = self.base_dir / subdir
            if d.exists() and d.is_dir():
                files = sorted(
                    f.name for f in d.iterdir()
                    if f.is_file() and not f.name.startswith(".")
                )
                if files:
                    lines.append(f"  {subdir}/")
                    for fname in files:
                        lines.append(f"    {fname}")
        return "\n".join(lines)


class SkillManager:
    """Discovers skills, parses SKILL.md, and manages activation."""

    def __init__(self) -> None:
        self._skills: dict[str, Skill] = {}
        self._activated: set[str] = set()
        self._max_skills = 100

    def scan(self) -> None:
        """Walk standard skill directories and build the registry.

        V1 scan paths (user-level only for security):
          1. ~/.ares/skills/
          2. ~/.agents/skills/

        Project-level (<cwd>/.agents/skills/) is skipped in V1.
        """
        base_dirs = [
            Path.home() / ".ares" / "skills",
            Path.home() / ".agents" / "skills",
        ]
        for base_dir in base_dirs:
            self._scan_dir(base_dir)

    def _scan_dir(self, base_dir: Path) -> None:
        """Scan a single directory for skill subdirectories."""
        if not base_dir.exists():
            return

        for entry in sorted(base_dir.iterdir()):
            if len(self._skills) >= self._max_skills:
                warnings.warn(
                    f"Reached maximum of {self._max_skills} skills. "
                    "Skipping remaining."
                )
                return

            if not entry.is_dir():
                continue
            if entry.name.startswith(".") or entry.name in (
                ".git", "node_modules", "__pycache__",
            ):
                continue

            skill_md = entry / "SKILL.md"
            if not skill_md.exists():
                continue

            try:
                content = skill_md.read_text(encoding="utf-8")
            except Exception:
                continue

            parsed = self._parse_frontmatter(content)
            if parsed is None:
                continue

            name, description = parsed
            if not name or not description:
                continue

            # Warn on name mismatch but still register
            if name != entry.name:
                warnings.warn(
                    f"Skill '{name}' in directory '{entry.name}' has mismatched name. "
                    f"Expected '{entry.name}'."
                )

            # First found wins (priority by scan order)
            if name not in self._skills:
                self._skills[name] = Skill(
                    name=name,
                    description=description,
                    base_dir=entry,
                )

    @staticmethod
    def _parse_frontmatter(content: str) -> tuple[str, str] | None:
        """Parse YAML frontmatter. Returns (name, description) or None."""
        lines = content.split("\n")
        if not lines or lines[0].strip() != "---":
            return None

        end_idx = None
        for i in range(1, len(lines)):
            if lines[i].strip() == "---":
                end_idx = i
                break
        if end_idx is None:
            return None

        metadata: dict[str, str] = {}
        for line in lines[1:end_idx]:
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if ":" in stripped:
                key, _, value = stripped.partition(":")
                key = key.strip()
                value = value.strip().strip("'\"")
                if key:
                    metadata[key] = value

        return metadata.get("name", ""), metadata.get("description", "")

    def catalog(self) -> str:
        """Return formatted XML for system prompt injection.

        Returns empty string if no skills are installed.
        """
        if not self._skills:
            return ""

        parts = ["<available_skills>"]
        for name in sorted(self._skills):
            skill = self._skills[name]
            parts.append(
                f"  <skill>"
                f"<name>{name}</name>"
                f"<description>{skill.description}</description>"
                f"</skill>"
            )
        parts.append("</available_skills>")
        return "\n".join(parts)

    def get(self, name: str) -> Skill | None:
        """Return full Skill record, or None if not found."""
        return self._skills.get(name)

    def list_names(self) -> list[str]:
        """Return all available skill names."""
        return list(self._skills.keys())

    def activate(self, name: str) -> None:
        """Mark a skill as activated in this session."""
        if name in self._skills:
            self._activated.add(name)

    def is_activated(self, name: str) -> bool:
        """Check if skill was already loaded (for dedup)."""
        return name in self._activated
```

- [ ] **Step 2: Run SkillManager tests**

Run: `python -m pytest tests/test_skill_manager.py::TestSkillManagerDiscovery tests/test_skill_manager.py::TestSkillManagerParsing tests/test_skill_manager.py::TestSkillManagerCatalog tests/test_skill_manager.py::TestSkillManagerGet tests/test_skill_manager.py::TestSkillManagerActivation -v --tb=short 2>&1 | head -60`

Expected: All SkillManager-specific tests PASS.

- [ ] **Step 3: Commit**

```bash
git add ares/skill_manager.py
git commit -m "feat: add SkillManager for skill discovery, parsing, and activation tracking"
```

---

### Task 3: Add `read_skill` tool definition and handler

**Files:**
- Modify: `ares/tools/definitions.py` (after line 465)
- Modify: `ares/tools/executor.py` (__init__ + handler map + new method)

- [ ] **Step 1: Add tool definition**

In `ares/tools/definitions.py`, after the last `_tool(...)` call (line 465, `get_task_artifacts`), add before the closing `]`:

```python
        _tool(
            "read_skill",
            "Load the full instructions for a skill. Call this before performing a task "
            "that matches a skill's description from <available_skills> in the system "
            "prompt. Returns the complete SKILL.md content plus a list of available "
            "scripts and resources.",
            {
                "name": {
                    "type": "string",
                    "description": "The skill name from <available_skills>",
                },
            },
            ["name"],
        ),
```

The last existing entry is `get_task_artifacts` (lines 458-465). Add a comma after line 465 and insert this before the closing `]`. The file's `get_tool_definitions()` returns a list — this new entry goes at the end of that list.

- [ ] **Step 2: Add handler + wire skill_manager in executor.py**

In `ares/tools/executor.py`:

**A)** In the `handlers` dict (after line 103, `"get_task_artifacts": self._get_task_artifacts,`), add:

```python
            "read_skill": self._read_skill,
```

**B)** After the `_get_task_artifacts` method (after line 607), add the new handler:

```python
    # ── Skill tools ────────────────────────────────────────────

    def _read_skill(self, args: dict) -> str:
        """Read the full instructions for a skill."""
        name = args.get("name", "")
        if not hasattr(self, "skill_manager") or self.skill_manager is None:
            return "Error: Skill manager not available."

        skill = self.skill_manager.get(name)
        if skill is None:
            available = ", ".join(self.skill_manager.list_names())
            if available:
                return f"Error: Skill '{name}' not found. Available skills: {available}"
            return f"Error: Skill '{name}' not found. No skills are currently installed."

        self.skill_manager.activate(name)

        body = skill.full_body()
        resources = skill.list_resources()

        result = f"<skill name=\"{name}\">\n{body}\n</skill>\n\n"
        result += f"Skill directory: {skill.base_dir}\n"
        if resources:
            result += f"Resources:\n{resources}"

        return result
```

- [ ] **Step 3: Run read_skill handler tests**

Run: `python -m pytest tests/test_skill_manager.py::TestReadSkillHandler -v --tb=short 2>&1 | head -30`

Expected: All 4 tests PASS.

- [ ] **Step 4: Commit**

```bash
git add ares/tools/definitions.py ares/tools/executor.py
git commit -m "feat: add read_skill tool with handler in ToolExecutor"
```

---

### Task 4: Wire SkillManager into Agent (init + catalog injection)

**Files:**
- Modify: `ares/agent.py` (__init__ + build_messages)
- Modify: `tests/test_agent.py` (add catalog injection tests)

- [ ] **Step 1: Init SkillManager in Agent.__init__**

In `ares/agent.py`, at the end of `__init__()` (after line 68 `self.profile_manager.ensure_exists()`), add:

```python
        from ares.skill_manager import SkillManager
        self.skill_manager = SkillManager()
        self.skill_manager.scan()
        self.tool_executor.skill_manager = self.skill_manager
```

- [ ] **Step 2: Inject catalog in build_messages()**

In `build_messages()` (lines 70-80), after line 75 (`system_content += f"\n\n## Current Context\n{context}"`), add:

```python
        catalog = self.skill_manager.catalog()
        if catalog:
            system_content += (
                "\n\n## Skills\n\nThe following skills provide specialized "
                "instructions for specific tasks. When a task matches a skill's "
                "description, call `read_skill` with the skill name to load its "
                "full instructions. Resolve relative paths in skill content against "
                "the skill's directory.\n\n"
                f"{catalog}"
            )
```

- [ ] **Step 3: Add catalog injection tests to test_agent.py**

Append to `tests/test_agent.py` (after line 227, the end of the file):

```python

class TestAgentSkillCatalog:
    """Tests for the skill catalog injection in build_messages()."""

    def test_agent_injects_catalog(self, agent, tmp_path):
        """build_messages() includes catalog when skills exist."""
        from ares.skill_manager import Skill

        agent.skill_manager._skills["test-skill"] = Skill(
            name="test-skill",
            description="A test skill",
            base_dir=tmp_path,
        )
        messages = agent.build_messages("Hello", [])
        system_content = messages[0]["content"]

        assert "<available_skills>" in system_content
        assert "test-skill" in system_content
        assert "A test skill" in system_content
        assert "</available_skills>" in system_content

    def test_agent_omits_catalog(self, agent):
        """build_messages() omits catalog when no skills exist."""
        messages = agent.build_messages("Hello", [])
        system_content = messages[0]["content"]

        assert "<available_skills>" not in system_content
```

- [ ] **Step 5: Run full SkillManager and agent test suites**

Run: `python -m pytest tests/test_skill_manager.py tests/test_agent.py::TestAgentSkillCatalog -v --tb=short 2>&1 | head -80`

Expected: All tests PASS.

- [ ] **Step 6: Commit**

```bash
git add ares/agent.py tests/test_agent.py
git commit -m "feat: wire SkillManager into Agent with catalog injection in build_messages()"
```

---

### Task 5: Final verification

- [ ] **Step 1: Run full test suite**

Run: `python -m pytest tests/ -x -q --tb=short 2>&1 | tail -10`

Expected: All tests pass (or only pre-existing unrelated failures).

- [ ] **Step 2: Commit if any cleanup needed**

```bash
git add -A
git commit -m "fix: test cleanup after skills system integration"
```

---

## Success Criteria Checklist

1. ✅ `SkillManager.scan()` finds SKILL.md files in `~/.ares/skills/` and `~/.agents/skills/`
2. ✅ Skills without SKILL.md, without name, or without description are skipped
3. ✅ Hidden dirs, .git, node_modules, __pycache__ are skipped during scan
4. ✅ Max 100 skills cap prevents runaway scanning
5. ✅ First-found-wins priority on name collision
6. ✅ `catalog()` returns formatted XML (or empty string when no skills installed)
7. ✅ `get(name)` returns Skill with lazy-loaded body
8. ✅ `full_body()` returns SKILL.md with frontmatter
9. ✅ `list_resources()` shows scripts/, references/, assets/ contents
10. ✅ `activate()` / `is_activated()` tracks loaded skills per session
11. ✅ `read_skill` tool definition exists in OpenAI function-calling format
12. ✅ `_read_skill` handler returns `<skill>`-wrapped content + resource listing
13. ✅ Unknown skill returns error with available names list
14. ✅ Agent init creates SkillManager and wires into tool_executor
15. ✅ Agent `build_messages()` injects catalog into system prompt (or omits when empty)
16. ✅ No new external dependencies
17. ✅ All existing tests still pass
