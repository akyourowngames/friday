# Ares v3: Proactive Memory & Context System — Design Spec

**Date:** 2026-06-18
**Status:** Draft
**Scope:** Profile, project context discovery, smart context blending

---

## 1. Problem Statement

Ares v2 has memory (vector + keyword search), tasks, and conversation summaries. But the context it injects into each session is limited:

- No persistent user profile — the LLM only knows what you've told it in the current conversation
- No project context — when you're working on a codebase, Ares doesn't know what language, framework, or conventions you're using
- Context is flat — no priority ordering, no token budgeting, no structured blending

The result: Ares feels generic. You have to re-explain yourself every session. It doesn't feel like Jarvis — it feels like a chatbot with a database.

---

## 2. Design Principles

1. **User-owned files.** The user writes and edits the profile. Ares never auto-modifies it. You're in control.
2. **Auto-discovery, not auto-mutation.** Ares reads what exists. It doesn't create files or modify your profile without asking.
3. **Hierarchical context.** User-level → project-level → local overrides. Same pattern Claude Code and Hermes use.
4. **Token-aware blending.** Context sections compete for a shared budget. More important sections get more space.
5. **Graceful absence.** If a file doesn't exist, Ares skips it silently. No errors, no noise.

---

## 3. Architecture: Context Pipeline

### Flow

```
Session start
  → Read ~/.ares/profile.md (user profile, always loaded)
  → Read ~/.ares/soul.md (personality, always loaded)
  → Scan CWD for project context files
  → Search memories relevant to user input
  → Fetch pending tasks
  → Blend into priority-ordered context string
  → Inject into system prompt via build_context_prompt()
```

### Priority Order (highest to lowest)

1. **Soul** — Ares personality definition (`~/.ares/soul.md`)
2. **Profile** — User identity, preferences, projects (`~/.ares/profile.md`)
3. **Project context** — Auto-discovered from CWD (CLAUDE.md, pyproject.toml, README, etc.)
4. **Memories** — Vector + keyword search, relevance-ranked
5. **Tasks** — Pending tasks, due-soon items
6. **Conversation summaries** — Recent session summaries

### Token Budget

Total context budget: ~2000 tokens (configurable via `context_token_budget` in config).

Allocation:
| Section | Max tokens | Fallback |
|---------|-----------|----------|
| Soul | 200 | Fixed |
| Profile | 400 | Fixed |
| Project context | 400 | Fixed |
| Memories | 800 | Fills remaining budget |
| Tasks | 200 | Fixed |

If soul or profile is small, unused budget flows to memories (the most variable section).

---

## 4. Component 1: Soul File (NEW)

### Location
`~/.ares/soul.md`

### Purpose
Defines Ares' personality — how it talks, what tone to use, what to prioritize. This is the Hermes `SOUL.md` pattern: the first thing in the system prompt.

### Format

```markdown
# Ares — My AI Assistant

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
```

### Behavior
- Created on first run with a sensible default
- User edits freely — Ares never auto-modifies it
- Read fresh every session, always in system prompt
- If missing, Ares uses built-in defaults (the current SYSTEM_PROMPT personality)

### Implementation

```python
# ares/soul.py

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
        if not self.soul_path.exists():
            self.soul_path.write_text(SOUL_TEMPLATE, encoding="utf-8")

    def read(self) -> str:
        if not self.soul_path.exists():
            return ""
        return self.soul_path.read_text(encoding="utf-8").strip()

    def get_context(self, token_budget: int = 200) -> str:
        content = self.read()
        if not content:
            return ""
        return truncate_to_tokens(f"## Ares Personality\n\n{content}", token_budget)
```

---

## 5. Component 2: Profile File

### Location
`~/.ares/profile.md`

### Purpose
User-maintained file about themselves. Ares reads it every session. The user edits it freely. This is the Hermes `USER.md` + Claude Code `~/.claude/CLAUDE.md` pattern.

### Format

```markdown
# About Me

## Identity
- Name: [name]
- Pronouns: [pronouns]

## Preferences
- Coding: Python, uses Codex for implementation
- Assistant style: concise, no fluff
- Terminal: Windows

## Current Projects
- Ares: personal AI assistant (Python, terminal)
- [other projects]

## Goals
- [What the user is working toward]

## Notes
- [Anything else]
```

### @imports (future)
Profile supports `@path/to/file` syntax to pull in external content (like Claude Code's `@imports` and Hermes' `@references`). Initial implementation: read referenced files and inject their content.

```markdown
# About Me
@~/notes/about-me.md

## Current Projects
@./PROJECT.md
```

### Behavior
- Created on first run with template
- Read fresh every session
- Never auto-modified by Ares
- If missing or empty, skip silently
- Truncated to 200 lines if huge (with note)

### Implementation

```python
# ares/profile.py

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
        if not self.profile_path.exists():
            self.profile_path.write_text(PROFILE_TEMPLATE, encoding="utf-8")

    def read(self) -> str:
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
        content = self.read()
        if not content:
            return ""
        content = self.resolve_imports(content)
        return truncate_to_tokens(
            f"## User Profile\n\n{content}", token_budget
        )
```

---

## 6. Component 3: Project Context Discovery

### Scan Targets (in order of priority)

| File | What it tells us | Max lines |
|------|-----------------|-----------|
| `CLAUDE.md` | Project instructions, conventions | 150 |
| `AGENTS.md` | Agent behavior rules | 150 |
| `.ares/config.json` | Ares-specific project config | 50 |
| `pyproject.toml` | Python project metadata | 50 |
| `package.json` | Node.js project metadata | 50 |
| `README.md` | Project description | 100 |
| `.hermes.md` | Hermes context (if shared project) | 100 |

### Behavior
- Scan current working directory only (not recursive, like Claude Code's root-level scan)
- Read up to 2 files to stay within token budget
- Skip files > 200 lines (too large for context)
- If no project files found, skip silently
- CWD is not required to be a git repo

### Implementation

```python
# ares/context.py

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
                text = path.read_text(encoding="utf-8")
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
        files = self.discover()
        if not files:
            return ""
        parts = ["## Current Project Context"]
        for filename, content in files:
            parts.append(f"\n### {filename}\n\n{content}")
        return truncate_to_tokens("\n".join(parts), token_budget)
```

---

## 7. Component 4: Smart Context Blending

### Updated `build_context_prompt()`

The existing function in `ares/prompts.py` gets memories + tasks. Updated to accept all context layers:

```python
def build_context_prompt(
    soul_context: str = "",
    profile_context: str = "",
    project_context: str = "",
    memories: list[dict] | None = None,
    tasks: list[dict] | None = None,
    conversation_summaries: list[str] | None = None,
    token_budget: int = 2000,
) -> str:
```

### Blending Logic

```python
def build_context_prompt(
    soul_context: str = "",
    profile_context: str = "",
    project_context: str = "",
    memories: list[dict] | None = None,
    tasks: list[dict] | None = None,
    conversation_summaries: list[str] | None = None,
    token_budget: int = 2000,
) -> str:
    sections = []
    used_tokens = 0

    # 1. Soul (highest priority, always included)
    if soul_context:
        sections.append(soul_context)
        used_tokens += estimate_tokens(soul_context)

    # 2. Profile (always included)
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

### Token Estimation

```python
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
```

---

## 8. Component 5: Agent Integration

### Updated `Agent.__init__()`

```python
def __init__(self, ...):
    ...
    from ares.soul import SoulManager
    from ares.profile import ProfileManager
    from ares.context import ProjectContext

    self.soul_manager = SoulManager(data_dir=config.data_dir)
    self.profile_manager = ProfileManager(data_dir=config.data_dir)
    self.project_context = ProjectContext()

    # Ensure files exist on first run
    self.soul_manager.ensure_exists()
    self.profile_manager.ensure_exists()
```

### Updated `Agent.get_context()`

```python
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
```

---

## 9. Component 6: System Prompt Integration

The system prompt in `ares/prompts.py` needs updating to reference the new context layers.

### Updated SYSTEM_PROMPT additions

```markdown
## Your Personality

Your personality is defined in the soul file. Follow those guidelines
for tone, communication style, and values.

## About the User

The user's profile is provided in context. Use it to personalize responses:
- Use their name when addressing them
- Reference their projects and goals when relevant
- Respect their stated preferences

## Project Context

When project context is provided, you're working within that codebase.
Follow its conventions, use its tools, and reference its structure.
```

---

## 10. Component 7: Config Updates

### New config fields in `ares/models.py`

```python
class AppConfig(BaseModel):
    ...
    soul_path: str = ""           # defaults to data_dir / "soul.md"
    profile_path: str = ""        # defaults to data_dir / "profile.md"
    project_context_enabled: bool = True
    context_token_budget: int = 2000
    project_context_max_files: int = 2
```

### Defaults
- `soul_path`: `{data_dir}/soul.md`
- `profile_path`: `{data_dir}/profile.md`
- `project_context_enabled`: `True`
- `context_token_budget`: `2000`
- `project_context_max_files`: `2`

---

## 11. Component 8: CLI Commands

### `/profile` — View or edit profile

```
/profile          → show profile content in terminal
/profile edit     → print path for manual editing
```

### `/soul` — View or edit personality

```
/soul             → show soul content in terminal
/soul edit        → open soul.md in $EDITOR (or default system editor)
/soul edit        → if no EDITOR set, print path for manual editing
```

Implementation: Uses `subprocess` to launch the user's editor. On Windows, falls back to `os.startfile()`. On Unix, uses `$EDITOR` or `$VISUAL` env var. If neither is set, prints the path with a note.

### `/context` — Show what context is being injected

```
/context          → show the full blended context for the current session
```

Implementation: Add to CLI's command handler. Render as Rich panels.

---

## 12. Files to Create/Modify

| File | Action | What Changes |
|------|--------|-------------|
| `ares/soul.py` | **Create** | SoulManager class + template |
| `ares/profile.py` | **Create** | ProfileManager class + @imports |
| `ares/context.py` | **Create** | ProjectContext class |
| `ares/prompts.py` | Modify | Update `build_context_prompt()` signature + SYSTEM_PROMPT |
| `ares/agent.py` | Modify | Initialize SoulManager + ProfileManager + ProjectContext |
| `ares/models.py` | Modify | Add soul/profile/config fields to AppConfig |
| `ares/cli.py` | Modify | Add `/profile`, `/soul`, `/context` commands |
| `ares/config.py` | Modify | Add new config defaults |
| `tests/test_soul.py` | **Create** | Tests for SoulManager |
| `tests/test_profile.py` | **Create** | Tests for ProfileManager |
| `tests/test_context.py` | **Create** | Tests for ProjectContext |
| `tests/test_prompts.py` | **Create** | Tests for updated build_context_prompt |

**Total:** 5 new files, 6 modified files, 3 new test files.

---

## 13. Error Handling

| Scenario | Handling |
|----------|----------|
| soul.md doesn't exist | Create template on first run |
| soul.md is empty | Use built-in defaults |
| profile.md doesn't exist | Create template on first run |
| profile.md is empty | Skip profile context silently |
| profile.md is huge (>200 lines) | Truncate with note |
| @import reference doesn't exist | Show comment, skip silently |
| @import reference is unreadable | Show comment, skip silently |
| No project files in CWD | Skip project context silently |
| CWD is not a git repo | Still scan for files (no git dependency) |
| Config field missing | Use default values |

---

## 14. Testing Strategy

- **Unit tests:** SoulManager, ProfileManager (read/write/ensure_exists/resolve_imports), ProjectContext (discover/truncation), build_context_prompt (blending logic, token budget, priority ordering)
- **Integration:** Agent.get_context() with mocked soul + profile + project files
- **Edge cases:** Empty files, no project files, huge files, missing @import targets, config field defaults

---

## 15. Out of Scope

- Auto-learning from conversations (Ares suggests but user decides)
- Profile syncing across machines
- Multi-profile support
- Auto-editing soul.md or profile.md by Ares
- Obsidian/Roam-style knowledge graphs
- Background file watching / live reindex
- MCP integration for context
- Skills system (Hermes-style on-demand knowledge docs)

---

## 16. Success Criteria

- [ ] Soul file is created on first run with sensible personality defaults
- [ ] Profile file is created on first run with template
- [ ] Both files are read fresh every session and appear in context
- [ ] Project files are auto-discovered from CWD
- [ ] Context blending respects token budget
- [ ] Soul + profile + project context + memories + tasks all appear in system prompt
- [ ] `/profile`, `/soul`, `/context` commands work
- [ ] @imports in profile resolve correctly
- [ ] All existing tests still pass
- [ ] New tests cover SoulManager, ProfileManager, ProjectContext, and blending logic

---

## 17. References

Research sources informing this design:

- **Claude Code**: CLAUDE.md hierarchy, auto memory, `@imports`, path-scoped rules ([docs](https://code.claude.com/docs/en/memory))
- **Hermes Agent**: SOUL.md personality, USER.md profile, MEMORY.md learnings, context file auto-discovery ([docs](https://hermes-agent.nousresearch.com/docs/))
- **Codex CLI**: AGENTS.md project instructions ([GitHub](https://github.com/openai/codex))
- **Aider**: CONVENTIONS.md + `.aider.conf.yml` auto-loading ([docs](https://aider.chat/docs/usage/conventions.html))
- **Odysseus**: Skills, memory, session management ([GitHub](https://github.com/pewdiepie-archdaemon/odysseus))
