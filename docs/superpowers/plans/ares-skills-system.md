# Ares Skills System — Draft Plan

> **Status:** Research & design draft. No code changes yet.
> **Date:** June 18, 2026

---

## What Are Skills?

Skills are on-demand knowledge documents that Ares can discover, load, and follow when needed. Think of them as **reusable playbooks** — when you (or Ares) invoke a skill, its instructions enter the conversation and guide what happens next.

This is the same pattern used by:
- **Hermes Agent** — `~/.hermes/skills/` with progressive disclosure, bundles, and a skills hub
- **Claude Code** — `~/.claude/skills/` and `.claude/skills/` with supporting files, dynamic context injection, and subagent execution
- **OpenClaw / Block Engineering** — Internal skills marketplaces with 100+ skills for tribal knowledge

The **agentskills.io** open standard defines the portable SKILL.md format that works across all of them.

---

## Design Goals

1. **Token-efficient** — Only load full skill content when actually needed (progressive disclosure)
2. **User-extensible** — Users create their own skills by dropping `.md` files in `~/.ares/skills/`
3. **Built-in starter pack** — Ares ships with useful default skills
4. **Agent-creatable** — Ares can save complex workflows as new skills for reuse
5. **Slash command access** — `/skill-name` to invoke any skill directly
6. **Category-organized** — Skills grouped by domain (coding, research, productivity, etc.)

---

## Architecture

### Skill File Format (SKILL.md)

```markdown
---
name: web-research
description: Deep web research with multiple searches and source analysis
category: research
version: 1.0.0
---

# Web Research

## When to Use
Use this skill when the user asks for in-depth research on a topic.

## Procedure
1. Break the research question into sub-questions
2. Search for each sub-question using web_search
3. Synthesize findings with source citations
4. Present a structured summary

## Pitfalls
- Avoid searching for things you already know
- Don't overload with too many searches (3-5 max per question)

## Verification
- All claims should have at least one source URL
- Summary should be under 500 words
```

### Directory Structure

```
~/.ares/skills/                     # User skills (source of truth)
├── coding/
│   ├── code-review/
│   │   └── SKILL.md
│   └── tdd-workflow/
│       └── SKILL.md
├── research/
│   ├── web-research/
│   │   ├── SKILL.md
│   │   └── references/
│   │       └── search-tips.md
│   └── paper-reader/
│       └── SKILL.md
├── productivity/
│   ├── daily-planner/
│   │   └── SKILL.md
│   └── email-drafter/
│       └── SKILL.md
└── ares-builtins/                  # Built-in skills (copied on install)
    ├── memory-consolidator/
    │   └── SKILL.md
    └── weekly-review/
        └── SKILL.md
```

### Module: `ares/skills.py`

```python
class Skill:
    """Parsed representation of a SKILL.md file."""
    name: str                # From frontmatter or directory name
    description: str         # From frontmatter
    category: str            # From frontmatter or parent directory
    version: str             # From frontmatter
    content: str             # Full markdown body
    path: Path               # Absolute path to SKILL.md
    files: list[Path]        # Supporting files (references/, scripts/, etc.)

class SkillManager:
    """Discovers, parses, and manages skills."""
    
    def __init__(self, skill_dirs: list[Path]):
        """skill_dirs = [~/.ares/skills/, built-in dir]"""
    
    def list_all(self) -> list[Skill]:
        """Return all discovered skills (Level 0: names + descriptions only)."""
    
    def search(self, query: str) -> list[Skill]:
        """Search skills by name, description, or category."""
    
    def get_skill(self, name: str) -> Skill | None:
        """Load a specific skill's full content (Level 1)."""
    
    def get_skill_file(self, name: str, file_path: str) -> str | None:
        """Load a supporting file from a skill (Level 2)."""
    
    def create_skill(self, name: str, content: str, category: str = "") -> Skill:
        """Create a new skill (agent or user)."""
    
    def update_skill(self, name: str, content: str) -> Skill:
        """Update an existing skill's content."""
    
    def delete_skill(self, name: str) -> bool:
        """Delete a skill."""
    
    def list_categories(self) -> dict[str, int]:
        """Return category → skill count mapping."""
```

### Progressive Disclosure (Token-Efficient)

| Level | What Loads | Token Cost | When |
|-------|-----------|------------|------|
| **L0** | Skill names + descriptions | ~500 tokens | Every turn (system prompt) |
| **L1** | Full SKILL.md content | Varies | When skill is invoked |
| **L2** | Supporting files | Varies | When skill needs them |

The system prompt includes a compact skill index like:
```
## Available Skills
- code-review: Review code for quality, security, and best practices
- web-research: Deep web research with source analysis
- daily-planner: Plan your day with priorities and time blocks
- memory-consolidator: Organize and clean up stored memories
Type /skill-name to invoke, or ask me to use one.
```

### Integration Points

#### 1. New Tools for the Agent

```python
# Tool definitions
_tool("list_skills", "List all available skills with descriptions.", {
    "category": {"type": "string", "description": "Filter by category."},
    "query": {"type": "string", "description": "Search query."},
})

_tool("load_skill", "Load a skill's full instructions into context.", {
    "name": {"type": "string", "description": "Skill name to load."},
})

_tool("create_skill", "Save a workflow as a reusable skill.", {
    "name": {"type": "string", "description": "Skill name (lowercase, hyphens)."},
    "content": {"type": "string", "description": "Full SKILL.md content."},
    "category": {"type": "string", "description": "Category for organization."},
})
```

#### 2. CLI Commands

| Command | Description |
|---------|-------------|
| `/skills` | List all available skills |
| `/skills search QUERY` | Search skills |
| `/skills load NAME` | Load a skill into context |
| `/skills create NAME` | Create a new skill interactively |
| `/skills delete NAME` | Delete a skill |
| `/skills categories` | Show skill categories |

#### 3. Slash Commands

Every skill becomes a slash command:
- `/code-review` → loads the code-review skill
- `/web-research` → loads the web-research skill
- `/daily-planner` → loads the daily-planner skill

#### 4. System Prompt Update

The system prompt gains a new section:
```
## Skills
You have access to a skills system. Skills are reusable playbooks that guide 
how you handle specific tasks. When relevant, suggest loading a skill.

To list skills: use the list_skills tool
To use a skill: use the load_skill tool
To save a workflow: use the create_skill tool

Users can also invoke skills with /skill-name.
```

#### 5. Agent Context Integration

When Ares processes a user message, the skill manager can optionally:
- Auto-suggest relevant skills based on the message
- Pre-load highly relevant skills (if user preference is enabled)

---

## Built-in Skills (Starter Pack)

| Skill | Category | Description |
|-------|----------|-------------|
| `memory-consolidator` | ares | Clean up and organize stored memories |
| `weekly-review` | ares | Review the week's tasks, memories, and progress |
| `code-review` | coding | Review code for quality and issues |
| `web-research` | research | Deep web research with synthesis |
| `daily-planner` | productivity | Plan your day with priorities |
| `export-backup` | ares | Full data export with timestamp |
| `system-info` | utilities | Gather system info for debugging |

---

## File Changes Required

| File | Change |
|------|--------|
| `ares/skills.py` | **NEW** — Skill + SkillManager classes |
| `ares/skills/` | **NEW** — Built-in skills directory |
| `ares/tools.py` | **MODIFY** — Add list_skills, load_skill, create_skill tools |
| `ares/agent.py` | **MODIFY** — Wire SkillManager, add skill context to build_messages |
| `ares/cli.py` | **MODIFY** — Add /skills commands and slash-command routing |
| `ares/prompts.py` | **MODIFY** — Add skills section to SYSTEM_PROMPT |
| `ares/models.py` | **MODIFY** — Add skill config fields to AppConfig |
| `ares/config.py` | **MODIFY** — Add skill_dirs config |
| `tests/test_skills.py` | **NEW** — Tests for SkillManager |

---

## What NOT to Build (Yet)

Things that are nice but not needed for v1:
- ❌ Skills Hub / online registry — keep it local for now
- ❌ Security scanning — skills are user-authored, not third-party
- ❌ Conditional activation (fallback skills) — add later
- ❌ Skill bundles — add when we have 20+ skills
- ❌ Dynamic context injection (`!`command``) — complex, add later
- ❌ String substitution ($ARGUMENTS) — add later
- ❌ Subagent execution (context: fork) — Ares doesn't have subagents yet

---

## Implementation Order

1. **Skill data model** — `Skill` pydantic model + `SkillManager` class
2. **SKILL.md parser** — YAML frontmatter + markdown body extraction
3. **Skill discovery** — Scan directories, build index
4. **Built-in skills** — Create starter pack
5. **Agent tools** — list_skills, load_skill, create_skill
6. **CLI commands** — /skills family + slash-command routing
7. **System prompt** — Add skills section
8. **Agent integration** — Wire into context building
9. **Tests** — Unit tests for parsing, discovery, CRUD
10. **Docs** — Update README with skills documentation

---

## Open Questions

1. **Should skills auto-load based on relevance?** Or only when explicitly invoked?
2. **Max skill content size?** Hermes doesn't seem to have a hard limit, but we should be token-conscious.
3. **Should the agent be able to update existing skills?** (Hermes does this for self-improvement)
4. **Skill versioning?** Or just overwrite on update?
5. **Should skills have metadata like `disable-model-invocation`?** Or keep it simple?

---

*This is a draft plan. No code has been written. Awaiting review and decisions before implementation.*
