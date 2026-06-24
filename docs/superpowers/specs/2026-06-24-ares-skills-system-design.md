# Ares Skills System — Design Spec

> **For agentic workers:** This is a design spec, not an implementation plan. After approval, invoke `superpowers:writing-plans` to create the implementation plan.

**Goal:** Add a skills system to Ares — letting the assistant load domain-specific workflow instructions from markdown files (SKILL.md + optional scripts/) without editing Python code.

**Architecture:** Follows the [Agent Skills specification](https://agentskills.io/specification), the cross-tool standard adopted by Claude Code, Codex, Open Interpreter, and OpenClaw. Three-tier progressive disclosure: metadata (~50 tokens/skill) in system prompt at session start, full SKILL.md body loaded on demand when a skill matches the task, scripts/resources loaded only when the instructions reference them.

**Tech Stack:** Python stdlib + PyYAML. No new dependencies.

---

## 1. Problem

| Issue | Impact |
|-------|--------|
| **All tool knowledge is hardcoded** | 35 tool definitions in Python, plus domain knowledge baked into system prompt |
| **Adding new capabilities = editing code** | Every new workflow requires modifying `definitions.py`, `prompts.py`, and `executor.py` |
| **No progressive disclosure** | All tool descriptions (~400 lines) sent in every API call regardless of relevance |
| **No user-extensible workflows** | Users can't teach Ares domain-specific procedures without modifying source code |
| **No domain quality gates** | The model has no documented procedures to follow — it wings every unfamiliar domain |

## 2. Solution Overview

A **skills system** following the industry-standard Agent Skills format:

```
~/.ares/skills/<skill-name>/
├── SKILL.md           ← Required: YAML frontmatter + markdown instructions
├── scripts/           ← Optional: executable code (Python, bash)
├── references/        ← Optional: reference docs loaded on demand
└── assets/            ← Optional: templates, data files
```

Three new components + changes to 5 existing files:

| Component | File | Action |
|-----------|------|--------|
| SkillManager | `ares/skill_manager.py` | **Create** — scan, parse, serve skills |
| Skill catalog | `ares/prompts.py` | **Modify** — inject `<available_skills>` block |
| `read_skill` tool | `ares/tools/definitions.py` | **Modify** — add tool definition |
| `read_skill` handler | `ares/tools/executor.py` | **Modify** — add handler method |
| Agent wiring | `ares/agent.py` | **Modify** — init SkillManager, inject catalog, protect from compaction |
| Server wiring | `ares/server.py` | **Modify** — wire SkillManager at startup |

### 2.1 Progressive Disclosure

| Tier | What's loaded | Token cost | When |
|------|---------------|------------|------|
| 1. Catalog | `name` + `description` only | ~50-100 tokens/skill | Session start — always in prompt |
| 2. Instructions | Full SKILL.md body | <5000 tokens | When skill matches task (via `read_skill` tool) |
| 3. Resources | Scripts, references, assets | Varies | When instructions reference them |

The model sees the catalog from the start. When it decides a skill is relevant, it calls `read_skill` to load the full instructions. If those instructions reference scripts or reference files, the model reads them individually via existing tools (`read_file`, `run_code`, `run_command`).

### 2.2 Skill File Format

```yaml
---
name: month-end-close
description: Walk through month-end close procedures — accruals, rollforwards, flux analysis. Use when the user mentions month-end, closing, accruals, or financial reporting.
---
## Steps

1. **Gather data** — Run `scripts/pull_gl.py` with the period
2. **Build accruals** — For each accrual policy, calculate:
   - Basis (contract amount with source)
   - Period portion (basis × days in period ÷ days in basis)
   - Already booked (prior accruals + invoices)
   - This-period accrual = period portion − already booked
3. **Draft JEs** — Dr expense account / Cr accrued liability with memo
4. **Flux analysis** — Run `scripts/flux.py` comparing this month vs last

## Rules
- Draft only, never post
- Flag missing support explicitly
```

---

## 3. SkillManager

### 3.1 Interface

```python
class SkillManager:
    """Discovers skills, parses SKILL.md, and manages activation."""

    def scan(self) -> None:
        """Walk all scan dirs for SKILL.md files, build registry.

        Standard paths (highest priority first):
          1. <cwd>/.agents/skills/
          2. ~/.ares/skills/
          3. ~/.agents/skills/

        Project overrides user overrides bundled.
        Name collisions are resolved by priority order.
        """

    def catalog(self) -> str:
        """Return formatted XML for system prompt injection.

        Returns empty string if no skills are installed.
        Each entry: <skill><name>X</name><description>Y</description></skill>
        """

    def get(self, name: str) -> Skill | None:
        """Return full Skill record. Lazy-loads body on first access."""

    def list_names(self) -> list[str]:
        """Return all available skill names."""

    def activate(self, name: str) -> None:
        """Mark a skill as activated in this session."""

    def is_activated(self, name: str) -> bool:
        """Check if skill was already loaded (for dedup)."""
```

### 3.2 Skill Record

```python
@dataclass
class Skill:
    name: str           # from frontmatter
    description: str    # from frontmatter
    base_dir: Path      # parent of SKILL.md
    _body: str | None   # loaded lazily on first access

    def full_body(self) -> str:
        """Return SKILL.md content (frontmatter + body)."""

    def list_resources(self) -> str:
        """List scripts/, references/, assets/ files."""
```

### 3.3 Scanning Rules

- Only directories containing exactly `SKILL.md` count as skills
- Name from frontmatter `name` field must match parent directory name (warn, not fail)
- Description is required; skills without it are skipped
- Max 100 skills (cap prevents runaway scanning)
- Skip `.git`, `node_modules`, `__pycache__`, and hidden dirs
- Lenient YAML parsing: unquoted colons in values are handled gracefully
- Scan happens once at session start, registry is immutable for the session

### 3.4 Dependencies

- `pathlib` for path operations
- `yaml` (PyYAML) for frontmatter parsing — already available via stdlib install
- No external connections, no network access

---

## 4. System Prompt Changes

### 4.1 Catalog Injection

In `ares/prompts.py`, a new section is added to the system prompt when skills are installed:

```
## Skills

The following skills provide specialized instructions for specific tasks.
When a task matches a skill's description, call `read_skill` with the skill
name to load its full instructions. Resolve relative paths in skill content
against the skill's directory.

<available_skills>
  <skill>
    <name>pdf-processing</name>
    <description>Extract PDF text, fill forms, merge files. Use when handling PDFs.</description>
  </skill>
  <skill>
    <name>code-review</name>
    <description>Review code for security, performance, and style issues.</description>
  </skill>
</available_skills>
```

### 4.2 Empty State

If `SkillManager.catalog()` returns empty string, no skills section is added. The model never sees an empty `<available_skills/>` block.

### 4.3 Behavioral Instructions

The behavioral instruction block is concise — it tells the model:
1. Skills exist for specialized tasks
2. Match tasks against skill descriptions
3. Call `read_skill` to load full instructions
4. Resolve relative paths against the skill's base directory

---

## 5. `read_skill` Tool

### 5.1 Definition

```python
_tool(
    "read_skill",
    "Load the full instructions for a skill. Call this before performing a task "
    "that matches a skill's description from <available_skills>. Returns the "
    "complete SKILL.md content plus a list of available scripts and resources.",
    {
        "name": {
            "type": "string",
            "description": "The skill name from <available_skills>",
        },
    },
    ["name"],
)
```

### 5.2 Handler Behavior

In `ToolExecutor._read_skill()`:

1. Lookup skill by name in `SkillManager`
2. If not found, return error listing available names
3. Mark skill as activated (for dedup tracking)
4. Return full SKILL.md (frontmatter + body) wrapped in `<skill>` XML tags
5. Append list of available resources (scripts/, references/, assets/)
6. The model reads resources on demand using existing tools

### 5.3 Result Format

```
<skill name="pdf-processing">
---
name: pdf-processing
description: ...
---
## Instructions
...
</skill>

Skill directory: /home/user/.agents/skills/pdf-processing/
Resources:
  scripts/extract.py
  scripts/merge.py
  references/pdf-spec-summary.md
```

---

## 6. Agent Changes

### 6.1 Initialization

In `ares/agent.py` `__init__()`:

```python
self.skill_manager = SkillManager()
self.skill_manager.scan()
self.tool_executor.skill_manager = self.skill_manager
```

### 6.2 Message Building

In `build_messages()`, after injecting soul/profile/project context:

```python
catalog = self.skill_manager.catalog()
if catalog:
    system_content += f"\n\n## Skills\n\n...instructions...\n\n{catalog}"
```

### 6.3 Context Protection

Skill content loaded via `read_skill` contains `<skill>` tags. The compaction logic in the agent should:
- Preserve messages containing `<skill` tags during context truncation
- Track activated skills via `SkillManager.is_activated()` to avoid re-injection
- Re-inject the catalog on each turn (it's part of the system prompt)

---

## 7. Server Wiring

In `ares/server.py`:

```python
# Wire skill_manager into tool_executor after construction
agent.tool_executor.skill_manager = agent.skill_manager
```

That's it — one line. The rest is self-contained in SkillManager and agent.py.

---

## 8. Files to Create/Modify

| File | Action | Purpose |
|------|--------|---------|
| `ares/skill_manager.py` | **Create** | Skill discovery, parsing, registry, activation tracking |
| `ares/tools/definitions.py` | **Modify** | Add `read_skill` tool definition |
| `ares/tools/executor.py` | **Modify** | Add `_read_skill` handler method |
| `ares/agent.py` | **Modify** | Init SkillManager, inject catalog, protect compaction |
| `ares/server.py` | **Modify** | Wire skill_manager into tool_executor |
| `ares/prompts.py` | **Modify** | Document the skills section (catalog injected dynamically) |
| `tests/test_skill_manager.py` | **Create** | Tests for discovery, parsing, catalog, edge cases |

---

## 9. Testing Strategy

| Test | What it verifies |
|------|-----------------|
| `test_scan_finds_skills` | Scans directory with SKILL.md files, builds registry |
| `test_scan_ignores_no_skill_md` | Directories without SKILL.md are skipped |
| `test_parse_frontmatter` | Extracts name, description from YAML frontmatter |
| `test_parse_body` | Returns markdown body after frontmatter |
| `test_lenient_yaml` | Handles unquoted colons in descriptions |
| `test_name_priority` | Project-level overrides user-level on collision |
| `test_catalog_output` | Returns formatted XML with correct structure |
| `test_catalog_empty` | Returns empty string when no skills installed |
| `test_get_skill` | Returns Skill record with lazy body loading |
| `test_get_unknown` | Returns None for nonexistent skill |
| `test_read_skill_handler` | ToolExecutor dispatches correctly to SkillManager |
| `test_activation_dedup` | is_activated() returns true after activate() |
| `test_max_skills_cap` | Scan caps at 100 skills |
| `test_agent_injects_catalog` | build_messages() includes catalog when skills exist |
| `test_agent_omits_catalog` | build_messages() omits catalog when no skills |

---

## 10. Risks

| Risk | Mitigation |
|------|------------|
| **Untrusted skill content** | Project-level skills from cloned repos could inject malicious instructions. **V1 mitigation:** Skip project-level (`<cwd>/.agents/skills/`) entirely. Only scan user-level dirs (`~/.ares/skills/`, `~/.agents/skills/`) where the user explicitly placed the files. Trust baked into the install location. |
| **YAML parsing fragility** | Community skills may have invalid YAML. Use lenient parser with fallback for unquoted colons. |
| **Context bloat from many skills** | Progressive disclosure limits overhead to ~50 tokens/skill. 100 skills = 5000 tokens max in catalog. |
| **Skill instructions lost mid-conversation** | Protect `<skill>` tagged content from compaction. Track activated skills for re-injection if needed. |
| **Model wastes turns on irrelevant skills** | `read_skill` tool requires exact skill name match from catalog — can't hallucinate skill names. |
| **Script security** | Scripts in `scripts/` run via existing `run_code`/`run_command` tools with existing sandboxing. No new attack surface. |

---

## 11. LLM Call Comparison

| Scenario | Before | After |
|----------|--------|-------|
| No skills installed | Baseline | Same (catalog omitted) |
| 50 skills installed, no match | Baseline | +2500 tokens in system prompt |
| Skill matches task (file-read activation) | Baseline | +1 `read_skill` tool call, then follows instructions |
| Skill with scripts | Baseline | +`read_skill` call + script execution via existing tools |
