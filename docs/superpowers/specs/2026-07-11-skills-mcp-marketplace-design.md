# Skills & MCP Marketplace Integration — Design Spec

**Date:** 2026-07-11
**Status:** Draft
**Author:** Claude + User

## Summary

Add a comprehensive skill and MCP marketplace system to Ares that allows searching, installing, creating, and managing skills from multiple community registries, and discovering/configuring MCP servers from multiple platforms — all through a unified `/skills` and `/mcp` command interface.

## Motivation

Ares currently has:
- Built-in skills in `ares/skills/`
- Local user skills in `~/.ares/skills/`
- MCP server configuration in `~/.ares/config.json`

But lacks:
- Community skill discovery and installation
- MCP server discovery from registries
- Skill creation from natural language
- Automatic MCP dependency resolution when installing skills

This feature makes Ares a self-extensible assistant that can grow its capabilities from community contributions.

---

## Architecture Overview

```
┌─────────────────────────────────────────────────────────────────┐
│                      ARES EXTENSIBILITY SYSTEM                   │
│                                                                  │
│  ┌─────────────────────┐          ┌─────────────────────┐       │
│  │    /skills CMD      │          │      /mcp CMD       │       │
│  │  search|install|    │          │  search|add|list|   │       │
│  │  create|list|remove │          │  remove|test|refresh │       │
│  └──────────┬──────────┘          └──────────┬──────────┘       │
│             │                                │                   │
│     ┌───────┴───────┐                ┌───────┴───────┐          │
│     ▼               ▼                ▼               ▼          │
│  ┌──────┐     ┌──────────┐     ┌──────────┐    ┌─────────┐     │
│  │Skill │     │ Skill    │     │ MCP      │    │ MCP     │     │
│  │Store │     │ Generator│     │ Registry │    │ Config  │     │
│  └──────┘     └──────────┘     └──────────┘    └─────────┘     │
│     │                              │                │           │
│     ▼                              ▼                ▼           │
│  ┌──────────────────────────────────────────────────────────┐  │
│  │                    External Registries                    │  │
│  │  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌─────────┐  │  │
│  │  │ ClawhHub │  │ OpenClaw │  │ MCP      │  │Smithery │  │  │
│  │  │ (skills) │  │ (skills) │  │ Registry │  │  .ai    │  │  │
│  │  └──────────┘  └──────────┘  └──────────┘  └─────────┘  │  │
│  └──────────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────┘
```

---

## Supported Registries

### Skill Registries

| Registry | API Base | Auth Required | Format |
|----------|----------|---------------|--------|
| **ClawhHub** | `https://clawhub.ai/api/v1` | Optional (for publish) | SKILL.md |
| **OpenClaw** | `https://api.openclaw.ai/v1` | Optional | SKILL.md |

### MCP Registries

| Registry | API Base | Auth Required | Transport |
|----------|----------|---------------|-----------|
| **MCP Registry** | `https://registry.modelcontextprotocol.io` | No | HTTP/stdio |
| **Smithery.ai** | `https://smithery.ai/api` | Optional | HTTP/stdio |
| **Custom** | Configurable | Varies | Varies |

---

## Data Model

### Skill Registry Config

```python
class SkillRegistry(BaseModel):
    """Configuration for a skill registry."""
    name: str                    # "clawhub", "openclaw", etc.
    api_base: str                # "https://clawhub.ai/api/v1"
    enabled: bool = True
    auth_token: str = ""         # Optional Bearer token
    priority: int = 0            # Higher = preferred
    search_limit: int = 10       # Max results per search
```

### MCP Registry Config

```python
class MCPRegistry(BaseModel):
    """Configuration for an MCP server registry."""
    name: str                    # "mcp-registry", "smithery", etc.
    api_base: str                # "https://registry.modelcontextprotocol.io"
    enabled: bool = True
    auth_token: str = ""
    priority: int = 0
```

### Skill Dependency

```python
class SkillDependency(BaseModel):
    """A dependency that a skill requires."""
    type: str                    # "mcp_server", "tool", "skill"
    name: str                    # "playwright", "filesystem", etc.
    required: bool = True
    auto_install: bool = False   # Can auto-add if missing
```

---

## Config Additions

### `~/.ares/config.json` additions

```json
{
  "skill_registries": [
    {
      "name": "clawhub",
      "api_base": "https://clawhub.ai/api/v1",
      "enabled": true,
      "priority": 10
    },
    {
      "name": "openclaw",
      "api_base": "https://api.openclaw.ai/v1",
      "enabled": true,
      "priority": 5
    }
  ],
  "mcp_registries": [
    {
      "name": "mcp-registry",
      "api_base": "https://registry.modelcontextprotocol.io",
      "enabled": true,
      "priority": 10
    },
    {
      "name": "smithery",
      "api_base": "https://smithery.ai/api",
      "enabled": true,
      "priority": 5
    }
  ]
}
```

---

## New Modules

### `ares/skill_registry.py` — Skill Registry Client

```python
class SkillRegistryClient:
    """Client for searching and downloading skills from registries."""

    def __init__(self, registries: list[SkillRegistry]):
        self.registries = sorted(registries, key=lambda r: -r.priority)

    async def search(self, query: str, registry: str = None) -> list[SkillResult]:
        """Search skills across registries."""
        ...

    async def get_skill(self, slug: str, registry: str = None) -> SkillDetail:
        """Get skill details from a registry."""
        ...

    async def download(self, slug: str, version: str = None, registry: str = None) -> bytes:
        """Download skill ZIP from registry."""
        ...

    async def get_versions(self, slug: str, registry: str = None) -> list[Version]:
        """Get version history for a skill."""
        ...
```

### `ares/mcp_registry.py` — MCP Registry Client

```python
class MCPRegistryClient:
    """Client for searching and discovering MCP servers."""

    def __init__(self, registries: list[MCPRegistry]):
        self.registries = sorted(registries, key=lambda r: -r.priority)

    async def search(self, query: str, registry: str = None) -> list[MCPResult]:
        """Search MCP servers across registries."""
        ...

    async def get_server(self, name: str, registry: str = None) -> MCPServerDetail:
        """Get MCP server details."""
        ...

    async def get_install_command(self, name: str, registry: str = None) -> InstallCommand:
        """Get install command/config for an MCP server."""
        ...
```

### `ares/skill_generator.py` — Skill Creator

```python
class SkillGenerator:
    """Generate new skills from natural language prompts."""

    def __init__(self, llm_client):
        self.llm = llm_client

    async def generate(self, name: str, description: str, category: str = "general") -> Skill:
        """Generate a new skill from description."""
        ...

    async def generate_from_task(self, task: str) -> Skill:
        """Generate a skill from a task description."""
        ...
```

---

## CLI Commands

### `/skills` Command Group

| Command | Description | Example |
|---------|-------------|---------|
| `/skills search <query>` | Search skill registries | `/skills search "weather forecast"` |
| `/skills install <slug>` | Download and install a skill | `/skills install @openclaw/weather` |
| `/skills create <name>` | Generate a new skill | `/skills create "daily standup"` |
| `/skills list` | Show all installed skills | `/skills list` |
| `/skills info <name>` | Show skill details | `/skills info browser-use` |
| `/skills update [name]` | Update skill(s) | `/skills update weather` |
| `/skills remove <name>` | Delete a skill | `/skills remove weather` |
| `/skills publish <name>` | Publish to registry | `/skills publish my-skill` |

### `/mcp` Command Group

| Command | Description | Example |
|---------|-------------|---------|
| `/mcp search <query>` | Search MCP registries | `/mcp search "database"` |
| `/mcp add <name>` | Add MCP server to config | `/mcp add @modelcontextprotocol/memory` |
| `/mcp list` | Show configured MCPs | `/mcp list` |
| `/mcp info <name>` | Show MCP server details | `/mcp info playwright` |
| `/mcp remove <name>` | Remove MCP server | `/mcp remove playwright` |
| `/mcp test <name>` | Test MCP connection | `/mcp test playwright` |
| `/mcp refresh` | Reload all MCP servers | `/mcp refresh` |

---

## Smart Coordination Flow

### Installing a Skill with MCP Dependencies

```
User: /skills install browser-use

Ares:
1. Search registries for "browser-use"
2. Find: @openclaw/browser-use (ClawhHub)
3. Download skill ZIP
4. Extract SKILL.md
5. Parse metadata → detect "requires: playwright"
6. Check if playwright MCP is configured
7. If NOT configured:
   ┌─────────────────────────────────────────┐
   │ This skill requires: Playwright MCP     │
   │                                          │
   │ Playwright MCP provides browser control  │
   │ tools for web automation.               │
   │                                          │
   │ Add it now? [Y/n]                       │
   └─────────────────────────────────────────┘
8. If user confirms:
   - Add playwright to mcp_servers config
   - Save config
   - Install skill to ~/.ares/skills/browser-use/
9. "Installed browser-use skill + Playwright MCP"
```

### Creating a Skill from Task

```
User: /skills create "daily standup"

Ares:
1. Generate skill using LLM:
   - Name: daily-standup
   - Category: productivity
   - Description: Build a daily standup summary
   - Content: Step-by-step standup workflow
2. Save to ~/.ares/skills/daily-standup/SKILL.md
3. "Created skill: daily-standup"
4. "Use it: /skills load daily-standup"
```

### Publishing a Skill

```
User: /skills publish my-workflow

Ares:
1. Check if user is logged in: /skills whoami
2. If not logged in:
   "Please login first: /skills login"
3. Validate skill:
   - SKILL.md exists
   - Name follows conventions
   - No security issues
4. Package skill as ZIP
5. Upload to registry
6. "Published: @username/my-workflow v1.0.0"
7. "View: https://clawhub.ai/skills/my-workflow"
```

---

## Error Handling

| Error | Message | Action |
|-------|---------|--------|
| Registry unreachable | "Registry X is unreachable. Try again later." | Skip registry |
| Skill not found | "Skill 'X' not found in any registry." | Suggest alternatives |
| Download failed | "Failed to download skill. Check your connection." | Retry or skip |
| MCP dependency missing | "This skill requires MCP server X." | Prompt to install |
| Invalid skill | "Skill X has invalid SKILL.md format." | Skip skill |
| Auth required | "Publishing requires login. Run: /skills login" | Prompt login |

---

## Security Considerations

1. **Skill validation** — Parse SKILL.md before installing, reject malformed skills
2. **MCP server verification** — Only add known MCP servers from trusted registries
3. **No auto-execution** — Skills are instructions only, not executable code
4. **User confirmation** — Always confirm before adding MCP servers
5. **Registry trust** — Only connect to configured, trusted registries
6. **Token security** — Store auth tokens in config, never in skills

---

## File Structure

### New Files

| File | Purpose |
|------|---------|
| `ares/skill_registry.py` | Skill registry client |
| `ares/mcp_registry.py` | MCP registry client |
| `ares/skill_generator.py` | Skill creation from LLM |
| `tests/test_skill_registry.py` | Skill registry tests |
| `tests/test_mcp_registry.py` | MCP registry tests |
| `tests/test_skill_generator.py` | Skill generator tests |

### Modified Files

| File | Changes |
|------|---------|
| `ares/models.py` | Add SkillRegistry, MCPRegistry models |
| `ares/config.py` | Add registry config loading |
| `ares/cli.py` | Add /skills and /mcp commands |
| `ares/tools/definitions.py` | Add skill marketplace tools |
| `ares/tools/executor.py` | Add skill marketplace handlers |

---

## Testing Strategy

1. **Unit tests** for registry clients (mock HTTP responses)
2. **Unit tests** for skill generator (mock LLM)
3. **Integration tests** for CLI commands
4. **Manual tests** for end-to-end flows
5. **Security tests** for skill validation

---

## Out of Scope

- Browser-based skill editor
- Skill versioning/rollback (use git)
- Skill dependencies between skills (only MCP dependencies)
- Paid/premium skills
- Skill ratings/reviews (registry-provided)

---

## Dependencies

- `httpx` (already included) — HTTP client for API calls
- `zipfile` (stdlib) — Extract skill ZIPs
- `yaml` (already included) — Parse SKILL.md frontmatter

No new external dependencies required.
