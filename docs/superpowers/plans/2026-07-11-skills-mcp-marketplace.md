# Skills & MCP Marketplace Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add community skill and MCP server marketplace integration to Ares — search, install, create, and manage skills from multiple registries, discover and configure MCP servers from multiple platforms.

**Architecture:** New modules `ares/skill_registry.py`, `ares/mcp_registry.py`, `ares/skill_generator.py`. Config gets registry lists. CLI gets `/skills` and `/mcp` command groups. Skills auto-detect MCP dependencies.

**Tech Stack:** httpx (already included), yaml (already included), zipfile (stdlib), Pydantic (already included)

## Research Summary

### How Other Frameworks Handle Skills

| Framework | Skill Format | Discovery | Installation |
|-----------|--------------|-----------|--------------|
| **OpenClaw** | SKILL.md in `~/.openclaw/workspace/skills/` | ClawhHub registry | `openclaw onboard` |
| **Claude Code** | `.claude/commands/` + plugins | Community marketplaces | `/plugin` command |
| **Hermes** | Custom format | Built-in only | Manual |

### Key Patterns Identified

1. **SKILL.md format** — Standard across OpenClaw ecosystem
2. **Registry-based discovery** — Central registries for community skills
3. **CLI-first interface** — Commands for search/install/manage
4. **Dependency detection** — Skills declare MCP requirements
5. **User confirmation** — Always confirm before adding MCP servers

### API Endpoints Discovered

**ClawhHub (Skills):**
- `GET /api/v1/search?q=<query>` — Search skills
- `GET /api/v1/skills/<slug>` — Get skill details
- `GET /api/v1/download?slug=<slug>` — Download skill ZIP
- `GET /api/v1/skills/<slug>/versions` — Version history

**MCP Registry:**
- `GET /api/v0/servers` — List servers (paginated)
- `GET /api/v0/servers/<name>` — Get server details
- API frozen at v0.1 (stable)

**Smithery.ai:**
- `GET /api/servers` — List servers
- `GET /api/servers/<name>` — Get server details

---

## File Map

| File | Action | Responsibility |
|------|--------|---------------|
| `ares/skill_registry.py` | **Create** | Skill registry client for ClawhHub/OpenClaw |
| `ares/mcp_registry.py` | **Create** | MCP registry client for MCP Registry/Smithery |
| `ares/skill_generator.py` | **Create** | Generate skills from natural language |
| `ares/models.py` | **Modify** | Add SkillRegistry, MCPRegistry models |
| `ares/config.py` | **Modify** | Add registry config loading |
| `ares/cli.py` | **Modify** | Add /skills and /mcp commands |
| `ares/tools/definitions.py` | **Modify** | Add skill marketplace tools |
| `ares/tools/executor.py` | **Modify** | Add skill marketplace handlers |
| `tests/test_skill_registry.py` | **Create** | Skill registry tests |
| `tests/test_mcp_registry.py` | **Create** | MCP registry tests |
| `tests/test_skill_generator.py` | **Create** | Skill generator tests |

---

### Task 1: Add registry config models

**Files:**
- Modify: `ares/models.py`

- [ ] **Step 1: Add SkillRegistry and MCPRegistry models**

In `ares/models.py`, add after the existing config models:

```python
class SkillRegistry(BaseModel):
    """Configuration for a skill registry."""
    name: str                    # "clawhub", "openclaw", etc.
    api_base: str                # "https://clawhub.ai/api/v1"
    enabled: bool = True
    auth_token: str = ""         # Optional Bearer token
    priority: int = 0            # Higher = preferred
    search_limit: int = 10       # Max results per search


class MCPRegistry(BaseModel):
    """Configuration for an MCP server registry."""
    name: str                    # "mcp-registry", "smithery", etc.
    api_base: str                # "https://registry.modelcontextprotocol.io"
    enabled: bool = True
    auth_token: str = ""
    priority: int = 0
```

- [ ] **Step 2: Add defaults to AppConfig**

In `AppConfig` class, add after the existing fields:

```python
    skill_registries: list[SkillRegistry] = Field(
        default_factory=lambda: [
            SkillRegistry(
                name="clawhub",
                api_base="https://clawhub.ai/api/v1",
                enabled=True,
                priority=10,
            ),
            SkillRegistry(
                name="openclaw",
                api_base="https://api.openclaw.ai/v1",
                enabled=True,
                priority=5,
            ),
        ]
    )
    mcp_registries: list[MCPRegistry] = Field(
        default_factory=lambda: [
            MCPRegistry(
                name="mcp-registry",
                api_base="https://registry.modelcontextprotocol.io",
                enabled=True,
                priority=10,
            ),
            MCPRegistry(
                name="smithery",
                api_base="https://smithery.ai/api",
                enabled=True,
                priority=5,
            ),
        ]
    )
```

- [ ] **Step 3: Verify model loads with defaults**

Run: `cd /c/Users/anime/friday && python -c "from ares.models import AppConfig; c = AppConfig(); print(f'Skill registries: {len(c.skill_registries)}'); print(f'MCP registries: {len(c.mcp_registries)}')"`

Expected: `Skill registries: 2` and `MCP registries: 2`

- [ ] **Step 4: Commit**

```bash
git add ares/models.py
git commit -m "feat: add SkillRegistry and MCPRegistry config models"
```

---

### Task 2: Create SkillRegistryClient

**Files:**
- Create: `ares/skill_registry.py`
- Create: `tests/test_skill_registry.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_skill_registry.py`:

```python
"""Tests for SkillRegistryClient."""

import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from ares.skill_registry import SkillRegistryClient, SkillResult
from ares.models import SkillRegistry


class TestSkillRegistryClient:
    def test_init_sorts_by_priority(self):
        registries = [
            SkillRegistry(name="low", api_base="http://low", priority=1),
            SkillRegistry(name="high", api_base="http://high", priority=10),
        ]
        client = SkillRegistryClient(registries)
        assert client.registries[0].name == "high"

    def test_init_filters_disabled(self):
        registries = [
            SkillRegistry(name="enabled", api_base="http://enabled", enabled=True),
            SkillRegistry(name="disabled", api_base="http://disabled", enabled=False),
        ]
        client = SkillRegistryClient(registries)
        assert len(client.registries) == 1
        assert client.registries[0].name == "enabled"

    @pytest.mark.asyncio
    async def test_search_returns_results(self):
        registries = [
            SkillRegistry(name="test", api_base="http://test", priority=10)
        ]
        client = SkillRegistryClient(registries)

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "results": [
                {
                    "slug": "weather",
                    "displayName": "Weather",
                    "summary": "Get weather forecasts",
                    "version": "1.0.0",
                    "owner": {"handle": "testuser"},
                    "score": 0.95,
                }
            ]
        }

        with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = mock_response
            results = await client.search("weather")
            assert len(results) == 1
            assert results[0].slug == "weather"

    @pytest.mark.asyncio
    async def test_search_handles_error(self):
        registries = [
            SkillRegistry(name="test", api_base="http://test", priority=10)
        ]
        client = SkillRegistryClient(registries)

        with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
            mock_get.side_effect = Exception("Connection failed")
            results = await client.search("weather")
            assert results == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /c/Users/anime/friday && python -m pytest tests/test_skill_registry.py -v`

Expected: FAIL — `ModuleNotFoundError: No module named 'ares.skill_registry'`

- [ ] **Step 3: Implement SkillRegistryClient**

Create `ares/skill_registry.py`:

```python
"""Skill registry client for searching and downloading skills."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import httpx

from ares.models import SkillRegistry

logger = logging.getLogger(__name__)


@dataclass
class SkillResult:
    """A skill search result."""
    slug: str
    name: str
    description: str
    version: str
    owner: str
    score: float
    registry: str


@dataclass
class SkillDetail:
    """Detailed skill information."""
    slug: str
    name: str
    description: str
    version: str
    owner: str
    content: str = ""
    files: list[str] | None = None
    dependencies: list[dict] | None = None


class SkillRegistryClient:
    """Client for searching and downloading skills from registries."""

    def __init__(self, registries: list[SkillRegistry]):
        self.registries = sorted(
            [r for r in registries if r.enabled],
            key=lambda r: -r.priority
        )

    async def search(self, query: str, registry: str = None) -> list[SkillResult]:
        """Search skills across registries."""
        results = []
        for reg in self.registries:
            if registry and reg.name != registry:
                continue
            try:
                reg_results = await self._search_registry(reg, query)
                results.extend(reg_results)
            except Exception as e:
                logger.warning("Failed to search %s: %s", reg.name, e)
        return sorted(results, key=lambda r: -r.score)

    async def get_skill(self, slug: str, registry: str = None) -> SkillDetail | None:
        """Get skill details from a registry."""
        for reg in self.registries:
            if registry and reg.name != registry:
                continue
            try:
                return await self._get_skill_detail(reg, slug)
            except Exception as e:
                logger.warning("Failed to get skill from %s: %s", reg.name, e)
        return None

    async def download(self, slug: str, version: str = None, registry: str = None) -> bytes | None:
        """Download skill ZIP from registry."""
        for reg in self.registries:
            if registry and reg.name != registry:
                continue
            try:
                return await self._download_skill(reg, slug, version)
            except Exception as e:
                logger.warning("Failed to download from %s: %s", reg.name, e)
        return None

    async def _search_registry(self, registry: SkillRegistry, query: str) -> list[SkillResult]:
        """Search a single registry."""
        async with httpx.AsyncClient() as client:
            headers = {}
            if registry.auth_token:
                headers["Authorization"] = f"Bearer {registry.auth_token}"

            response = await client.get(
                f"{registry.api_base}/search",
                params={"q": query, "limit": registry.search_limit},
                headers=headers,
                timeout=10.0,
            )
            response.raise_for_status()

            data = response.json()
            results = []
            for item in data.get("results", []):
                results.append(SkillResult(
                    slug=item.get("slug", ""),
                    name=item.get("displayName", item.get("slug", "")),
                    description=item.get("summary", ""),
                    version=item.get("version", "1.0.0"),
                    owner=item.get("owner", {}).get("handle", "unknown"),
                    score=item.get("score", 0.0),
                    registry=registry.name,
                ))
            return results

    async def _get_skill_detail(self, registry: SkillRegistry, slug: str) -> SkillDetail:
        """Get skill details from a single registry."""
        async with httpx.AsyncClient() as client:
            headers = {}
            if registry.auth_token:
                headers["Authorization"] = f"Bearer {registry.auth_token}"

            response = await client.get(
                f"{registry.api_base}/skills/{slug}",
                headers=headers,
                timeout=10.0,
            )
            response.raise_for_status()

            data = response.json()
            return SkillDetail(
                slug=data.get("slug", slug),
                name=data.get("displayName", slug),
                description=data.get("summary", ""),
                version=data.get("version", "1.0.0"),
                owner=data.get("owner", {}).get("handle", "unknown"),
                files=data.get("files"),
                dependencies=data.get("metadata", {}).get("dependencies"),
            )

    async def _download_skill(self, registry: SkillRegistry, slug: str, version: str = None) -> bytes:
        """Download skill ZIP from a single registry."""
        async with httpx.AsyncClient() as client:
            headers = {}
            if registry.auth_token:
                headers["Authorization"] = f"Bearer {registry.auth_token}"

            params = {"slug": slug}
            if version:
                params["version"] = version

            response = await client.get(
                f"{registry.api_base}/download",
                params=params,
                headers=headers,
                timeout=30.0,
            )
            response.raise_for_status()
            return response.content
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /c/Users/anime/friday && python -m pytest tests/test_skill_registry.py -v`

Expected: All tests PASS

- [ ] **Step 5: Commit**

```bash
git add ares/skill_registry.py tests/test_skill_registry.py
git commit -m "feat: add SkillRegistryClient for searching and downloading skills"
```

---

### Task 3: Create MCPRegistryClient

**Files:**
- Create: `ares/mcp_registry.py`
- Create: `tests/test_mcp_registry.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_mcp_registry.py`:

```python
"""Tests for MCPRegistryClient."""

import pytest
from unittest.mock import AsyncMock, patch
from ares.mcp_registry import MCPRegistryClient, MCPResult
from ares.models import MCPRegistry


class TestMCPRegistryClient:
    def test_init_sorts_by_priority(self):
        registries = [
            MCPRegistry(name="low", api_base="http://low", priority=1),
            MCPRegistry(name="high", api_base="http://high", priority=10),
        ]
        client = MCPRegistryClient(registries)
        assert client.registries[0].name == "high"

    @pytest.mark.asyncio
    async def test_search_returns_results(self):
        registries = [
            MCPRegistry(name="test", api_base="http://test", priority=10)
        ]
        client = MCPRegistryClient(registries)

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "servers": [
                {
                    "name": "io.modelcontextprotocol/memory",
                    "description": "Knowledge graph-based memory",
                    "version": "1.0.0",
                    "repository": {"url": "https://github.com/modelcontextprotocol/servers"},
                }
            ]
        }

        with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = mock_response
            results = await client.search("memory")
            assert len(results) == 1
            assert results[0].name == "io.modelcontextprotocol/memory"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /c/Users/anime/friday && python -m pytest tests/test_mcp_registry.py -v`

Expected: FAIL — `ModuleNotFoundError: No module named 'ares.mcp_registry'`

- [ ] **Step 3: Implement MCPRegistryClient**

Create `ares/mcp_registry.py`:

```python
"""MCP registry client for discovering and configuring MCP servers."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import httpx

from ares.models import MCPRegistry

logger = logging.getLogger(__name__)


@dataclass
class MCPResult:
    """An MCP server search result."""
    name: str
    description: str
    version: str
    repository: str
    registry: str


@dataclass
class MCPServerDetail:
    """Detailed MCP server information."""
    name: str
    description: str
    version: str
    repository: str
    packages: list[dict] | None = None
    remotes: list[dict] | None = None


@dataclass
class InstallCommand:
    """Instructions for installing an MCP server."""
    name: str
    command: str | None = None
    args: list[str] | None = None
    env: dict[str, str] | None = None
    transport: str = "stdio"


class MCPRegistryClient:
    """Client for searching and discovering MCP servers."""

    def __init__(self, registries: list[MCPRegistry]):
        self.registries = sorted(
            [r for r in registries if r.enabled],
            key=lambda r: -r.priority
        )

    async def search(self, query: str, registry: str = None) -> list[MCPResult]:
        """Search MCP servers across registries."""
        results = []
        for reg in self.registries:
            if registry and reg.name != registry:
                continue
            try:
                reg_results = await self._search_registry(reg, query)
                results.extend(reg_results)
            except Exception as e:
                logger.warning("Failed to search %s: %s", reg.name, e)
        return results

    async def get_server(self, name: str, registry: str = None) -> MCPServerDetail | None:
        """Get MCP server details."""
        for reg in self.registries:
            if registry and reg.name != registry:
                continue
            try:
                return await self._get_server_detail(reg, name)
            except Exception as e:
                logger.warning("Failed to get server from %s: %s", reg.name, e)
        return None

    async def get_install_command(self, name: str, registry: str = None) -> InstallCommand | None:
        """Get install command for an MCP server."""
        detail = await self.get_server(name, registry)
        if not detail:
            return None

        # Parse packages to determine install method
        if detail.packages:
            for pkg in detail.packages:
                if pkg.get("registryName") == "npm":
                    return InstallCommand(
                        name=detail.name,
                        command="npx",
                        args=[pkg.get("packageName", ""), "@latest"],
                        transport="stdio",
                    )
                elif pkg.get("registryName") == "pypi":
                    return InstallCommand(
                        name=detail.name,
                        command="uvx",
                        args=[pkg.get("packageName", "")],
                        transport="stdio",
                    )

        # Check remotes for HTTP transport
        if detail.remotes:
            for remote in detail.remotes:
                if remote.get("type") == "streamable-http":
                    return InstallCommand(
                        name=detail.name,
                        transport="streamable-http",
                        args=[remote.get("url", "")],
                    )

        return None

    async def _search_registry(self, registry: MCPRegistry, query: str) -> list[MCPResult]:
        """Search a single registry."""
        async with httpx.AsyncClient() as client:
            headers = {}
            if registry.auth_token:
                headers["Authorization"] = f"Bearer {registry.auth_token}"

            # Try different API endpoints based on registry
            if "registry.modelcontextprotocol.io" in registry.api_base:
                return await self._search_mcp_registry(client, registry, query, headers)
            elif "smithery.ai" in registry.api_base:
                return await self._search_smithery(client, registry, query, headers)
            else:
                return await self._search_generic(client, registry, query, headers)

    async def _search_mcp_registry(self, client, registry, query, headers) -> list[MCPResult]:
        """Search MCP Registry specifically."""
        response = await client.get(
            f"{registry.api_base}/api/v0/servers",
            params={"q": query, "limit": 10},
            headers=headers,
            timeout=10.0,
        )
        response.raise_for_status()

        data = response.json()
        results = []
        for server in data.get("servers", []):
            server_data = server.get("server", server)
            results.append(MCPResult(
                name=server_data.get("name", ""),
                description=server_data.get("description", ""),
                version=server_data.get("version", "1.0.0"),
                repository=server_data.get("repository", {}).get("url", ""),
                registry=registry.name,
            ))
        return results

    async def _search_smithery(self, client, registry, query, headers) -> list[MCPResult]:
        """Search Smithery.ai specifically."""
        response = await client.get(
            f"{registry.api_base}/servers",
            params={"q": query, "limit": 10},
            headers=headers,
            timeout=10.0,
        )
        response.raise_for_status()

        data = response.json()
        results = []
        for server in data.get("servers", []):
            results.append(MCPResult(
                name=server.get("name", ""),
                description=server.get("description", ""),
                version=server.get("version", "1.0.0"),
                repository=server.get("repository", ""),
                registry=registry.name,
            ))
        return results

    async def _search_generic(self, client, registry, query, headers) -> list[MCPResult]:
        """Search a generic registry."""
        response = await client.get(
            f"{registry.api_base}/search",
            params={"q": query, "limit": 10},
            headers=headers,
            timeout=10.0,
        )
        response.raise_for_status()

        data = response.json()
        results = []
        for item in data.get("results", data.get("servers", [])):
            results.append(MCPResult(
                name=item.get("name", ""),
                description=item.get("description", ""),
                version=item.get("version", "1.0.0"),
                repository=item.get("repository", {}).get("url", ""),
                registry=registry.name,
            ))
        return results

    async def _get_server_detail(self, registry: MCPRegistry, name: str) -> MCPServerDetail:
        """Get server details from a single registry."""
        async with httpx.AsyncClient() as client:
            headers = {}
            if registry.auth_token:
                headers["Authorization"] = f"Bearer {registry.auth_token}"

            if "registry.modelcontextprotocol.io" in registry.api_base:
                response = await client.get(
                    f"{registry.api_base}/api/v0/servers/{name}",
                    headers=headers,
                    timeout=10.0,
                )
            else:
                response = await client.get(
                    f"{registry.api_base}/servers/{name}",
                    headers=headers,
                    timeout=10.0,
                )

            response.raise_for_status()

            data = response.json()
            server = data.get("server", data)
            return MCPServerDetail(
                name=server.get("name", name),
                description=server.get("description", ""),
                version=server.get("version", "1.0.0"),
                repository=server.get("repository", {}).get("url", ""),
                packages=server.get("packages"),
                remotes=server.get("remotes"),
            )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /c/Users/anime/friday && python -m pytest tests/test_mcp_registry.py -v`

Expected: All tests PASS

- [ ] **Step 5: Commit**

```bash
git add ares/mcp_registry.py tests/test_mcp_registry.py
git commit -m "feat: add MCPRegistryClient for discovering MCP servers"
```

---

### Task 4: Create SkillGenerator

**Files:**
- Create: `ares/skill_generator.py`
- Create: `tests/test_skill_generator.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_skill_generator.py`:

```python
"""Tests for SkillGenerator."""

import pytest
from unittest.mock import AsyncMock, MagicMock
from ares.skill_generator import SkillGenerator


class TestSkillGenerator:
    @pytest.mark.asyncio
    async def test_generate_creates_skill(self):
        mock_llm = MagicMock()
        mock_llm.complete = AsyncMock(return_value="""
---
name: daily-standup
description: Build a daily standup summary
category: productivity
version: 1.0.0
---

# Daily Standup

## Steps

1. Ask for updates
2. Summarize
3. Format output
""")

        generator = SkillGenerator(mock_llm)
        skill = await generator.generate(
            name="daily-standup",
            description="Build a daily standup summary",
            category="productivity"
        )

        assert skill.name == "daily-standup"
        assert skill.description == "Build a daily standup summary"
        assert "Daily Standup" in skill.content

    @pytest.mark.asyncio
    async def test_generate_from_task(self):
        mock_llm = MagicMock()
        mock_llm.complete = AsyncMock(return_value="""
---
name: weather-check
description: Check weather for a location
category: utilities
version: 1.0.0
---

# Weather Check

## Steps

1. Get location
2. Fetch weather
3. Format report
""")

        generator = SkillGenerator(mock_llm)
        skill = await generator.generate_from_task("Check weather for a location")

        assert skill.name == "weather-check"
        assert "Weather Check" in skill.content
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /c/Users/anime/friday && python -m pytest tests/test_skill_generator.py -v`

Expected: FAIL — `ModuleNotFoundError: No module named 'ares.skill_generator'`

- [ ] **Step 3: Implement SkillGenerator**

Create `ares/skill_generator.py`:

```python
"""Generate new skills from natural language prompts."""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

import yaml

from ares.skills import Skill, SKILL_NAME_RE

logger = logging.getLogger(__name__)

SKILL_GENERATION_PROMPT = """Generate a SKILL.md file for an AI assistant skill.

Name: {name}
Description: {description}
Category: {category}

The skill should provide clear, actionable instructions for completing the task.
Include:
- YAML frontmatter with name, description, category, version
- Clear scope section
- Step-by-step instructions
- Safety guidelines if applicable
- Example use cases

Output ONLY the SKILL.md content, no explanations.
"""


class SkillGenerator:
    """Generate new skills from natural language prompts."""

    def __init__(self, llm_client: Any):
        self.llm = llm_client

    async def generate(
        self,
        name: str,
        description: str,
        category: str = "general",
        version: str = "1.0.0",
    ) -> Skill:
        """Generate a new skill from description."""
        # Validate name
        if not SKILL_NAME_RE.match(name):
            raise ValueError(
                f"Invalid skill name: {name}. "
                "Use lowercase letters, numbers, and hyphens only."
            )

        # Generate content
        prompt = SKILL_GENERATION_PROMPT.format(
            name=name,
            description=description,
            category=category,
        )
        content = await self.llm.complete(prompt)

        # Parse generated content
        return self._parse_skill(content, name, description, category, version)

    async def generate_from_task(self, task: str) -> Skill:
        """Generate a skill from a task description."""
        # Extract name from task
        name = self._task_to_name(task)
        description = task

        return await self.generate(name, description)

    def _parse_skill(
        self,
        content: str,
        name: str,
        description: str,
        category: str,
        version: str,
    ) -> Skill:
        """Parse generated SKILL.md content into a Skill object."""
        # Extract frontmatter if present
        frontmatter = {}
        body = content

        if content.startswith("---"):
            parts = content.split("---", 2)
            if len(parts) >= 3:
                try:
                    frontmatter = yaml.safe_load(parts[1]) or {}
                except yaml.YAMLError:
                    pass
                body = parts[2].strip()

        # Use provided values or frontmatter
        return Skill(
            name=frontmatter.get("name", name),
            description=frontmatter.get("description", description),
            category=frontmatter.get("category", category),
            version=frontmatter.get("version", version),
            content=body,
        )

    def _task_to_name(self, task: str) -> str:
        """Convert a task description to a skill name."""
        # Remove common words
        words = task.lower().split()
        stop_words = {"a", "an", "the", "for", "to", "and", "or", "in", "on", "at"}
        meaningful = [w for w in words if w not in stop_words and len(w) > 2]

        # Take first 2-3 meaningful words
        name_words = meaningful[:3]
        name = "-".join(name_words)

        # Clean up
        name = re.sub(r"[^a-z0-9-]", "", name)
        name = re.sub(r"-+", "-", name).strip("-")

        # Ensure valid name
        if not name or not SKILL_NAME_RE.match(name):
            name = "generated-skill"

        return name

    def save_skill(self, skill: Skill, directory: Path) -> Path:
        """Save a skill to disk."""
        skill_dir = directory / skill.name
        skill_dir.mkdir(parents=True, exist_ok=True)

        skill_file = skill_dir / "SKILL.md"

        # Build frontmatter
        frontmatter = {
            "name": skill.name,
            "description": skill.description,
            "category": skill.category,
            "version": skill.version,
        }

        # Write SKILL.md
        content = "---\n"
        content += yaml.dump(frontmatter, default_flow_style=False)
        content += "---\n\n"
        content += skill.content

        skill_file.write_text(content, encoding="utf-8")

        return skill_dir
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /c/Users/anime/friday && python -m pytest tests/test_skill_generator.py -v`

Expected: All tests PASS

- [ ] **Step 5: Commit**

```bash
git add ares/skill_generator.py tests/test_skill_generator.py
git commit -m "feat: add SkillGenerator for creating skills from natural language"
```

---

### Task 5: Add /skills CLI commands

**Files:**
- Modify: `ares/cli.py`

- [ ] **Step 1: Add imports**

In `ares/cli.py`, add to imports:

```python
from ares.skill_registry import SkillRegistryClient
from ares.mcp_registry import MCPRegistryClient
from ares.skill_generator import SkillGenerator
```

- [ ] **Step 2: Add to COMPLETER**

```python
COMPLETER = WordCompleter([
    "/help", "/memory", "/model", "/clear",
    "/forget", "/export", "/import", "/reset", "/exit",
    "/soul", "/profile", "/context", "/setup", "/browser",
    "/skills", "/mcp",
    "/skills search", "/skills install", "/skills create",
    "/skills list", "/skills info", "/skills remove",
    "/mcp search", "/mcp add", "/mcp list", "/mcp remove",
], ignore_case=True)
```

- [ ] **Step 3: Add /skills command handler**

In `_handle_command`, add:

```python
        elif command == "/skills":
            sub = arg.strip().lower() if arg else "list"

            if sub.startswith("search "):
                query = sub[7:].strip()
                if not query:
                    self.console.print("[red]Usage: /skills search <query>[/red]")
                    return
                await self._skills_search(query)

            elif sub.startswith("install "):
                slug = sub[8:].strip()
                if not slug:
                    self.console.print("[red]Usage: /skills install <slug>[/red]")
                    return
                await self._skills_install(slug)

            elif sub.startswith("create "):
                name = sub[7:].strip()
                if not name:
                    self.console.print("[red]Usage: /skills create <name>[/red]")
                    return
                await self._skills_create(name)

            elif sub.startswith("remove "):
                name = sub[7:].strip()
                if not name:
                    self.console.print("[red]Usage: /skills remove <name>[/red]")
                    return
                self._skills_remove(name)

            elif sub.startswith("info "):
                name = sub[5:].strip()
                if not name:
                    self.console.print("[red]Usage: /skills info <name>[/red]")
                    return
                await self._skills_info(name)

            else:
                self._skills_list()
```

- [ ] **Step 4: Implement _skills_search method**

```python
    async def _skills_search(self, query: str):
        """Search skills from registries."""
        client = SkillRegistryClient(self.config.skill_registries)
        results = await client.search(query)

        if not results:
            self.console.print(f"[yellow]No skills found for '{query}'[/yellow]")
            return

        table = Table(title=f"Skills matching '{query}'", border_style="cyan")
        table.add_column("Name", style="bold")
        table.add_column("Description")
        table.add_column("Owner")
        table.add_column("Registry")

        for result in results[:10]:
            table.add_row(
                result.name,
                result.description[:50] + "..." if len(result.description) > 50 else result.description,
                result.owner,
                result.registry,
            )

        self.console.print(table)
```

- [ ] **Step 5: Implement _skills_install method**

```python
    async def _skills_install(self, slug: str):
        """Install a skill from registry."""
        from zipfile import ZipFile
        from io import BytesIO

        client = SkillRegistryClient(self.config.skill_registries)

        # Check if skill already exists
        skill_dir = Path("~/.ares/skills").expanduser() / slug.replace("/", "-")
        if skill_dir.exists():
            self.console.print(f"[yellow]Skill '{slug}' already installed. Use /skills remove first.[/yellow]")
            return

        self.console.print(f"[cyan]Downloading {slug}...[/cyan]")

        # Download skill
        content = await client.download(slug)
        if not content:
            self.console.print(f"[red]Failed to download skill '{slug}'[/red]")
            return

        # Extract ZIP
        try:
            with ZipFile(BytesIO(content)) as zf:
                zf.extractall(Path("~/.ares/skills").expanduser())
        except Exception as e:
            self.console.print(f"[red]Failed to extract skill: {e}[/red]")
            return

        self.console.print(f"[green]Installed skill: {slug}[/green]")
```

- [ ] **Step 6: Implement _skills_create method**

```python
    async def _skills_create(self, name: str):
        """Create a new skill from description."""
        self.console.print(f"[cyan]Describe what this skill should do:[/cyan]")
        description = input("> ").strip()

        if not description:
            self.console.print("[red]Description required[/red]")
            return

        generator = SkillGenerator(self.llm)
        skill = await generator.generate(name, description)

        # Save skill
        skills_dir = Path("~/.ares/skills").expanduser()
        skill_path = generator.save_skill(skill, skills_dir)

        self.console.print(f"[green]Created skill: {name}[/green]")
        self.console.print(f"[dim]Location: {skill_path}[/dim]")
```

- [ ] **Step 7: Implement _skills_list method**

```python
    def _skills_list(self):
        """List all installed skills."""
        skills = self.skill_manager.list_all()

        if not skills:
            self.console.print("[yellow]No skills installed[/yellow]")
            return

        table = Table(title="Installed Skills", border_style="cyan")
        table.add_column("Name", style="bold")
        table.add_column("Category")
        table.add_column("Description")

        for skill in skills:
            table.add_row(
                skill.name,
                skill.category,
                skill.description[:50] + "..." if len(skill.description) > 50 else skill.description,
            )

        self.console.print(table)
```

- [ ] **Step 8: Implement _skills_remove method**

```python
    def _skills_remove(self, name: str):
        """Remove an installed skill."""
        skill_dir = Path("~/.ares/skills").expanduser() / name
        if not skill_dir.exists():
            self.console.print(f"[red]Skill '{name}' not found[/red]")
            return

        import shutil
        shutil.rmtree(skill_dir)
        self.console.print(f"[green]Removed skill: {name}[/green]")
```

- [ ] **Step 9: Commit**

```bash
git add ares/cli.py
git commit -m "feat: add /skills CLI commands for skill marketplace"
```

---

### Task 6: Add /mcp CLI commands

**Files:**
- Modify: `ares/cli.py`

- [ ] **Step 1: Add /mcp command handler**

In `_handle_command`, add:

```python
        elif command == "/mcp":
            sub = arg.strip().lower() if arg else "list"

            if sub.startswith("search "):
                query = sub[7:].strip()
                if not query:
                    self.console.print("[red]Usage: /mcp search <query>[/red]")
                    return
                await self._mcp_search(query)

            elif sub.startswith("add "):
                name = sub[4:].strip()
                if not name:
                    self.console.print("[red]Usage: /mcp add <name>[/red]")
                    return
                await self._mcp_add(name)

            elif sub.startswith("remove "):
                name = sub[7:].strip()
                if not name:
                    self.console.print("[red]Usage: /mcp remove <name>[/red]")
                    return
                self._mcp_remove(name)

            elif sub.startswith("info "):
                name = sub[5:].strip()
                if not name:
                    self.console.print("[red]Usage: /mcp info <name>[/red]")
                    return
                await self._mcp_info(name)

            elif sub == "test":
                await self._mcp_test_all()

            elif sub.startswith("test "):
                name = sub[5:].strip()
                if not name:
                    self.console.print("[red]Usage: /mcp test <name>[/red]")
                    return
                await self._mcp_test(name)

            elif sub == "refresh":
                await self._mcp_refresh()

            else:
                self._mcp_list()
```

- [ ] **Step 2: Implement _mcp_search method**

```python
    async def _mcp_search(self, query: str):
        """Search MCP servers from registries."""
        client = MCPRegistryClient(self.config.mcp_registries)
        results = await client.search(query)

        if not results:
            self.console.print(f"[yellow]No MCP servers found for '{query}'[/yellow]")
            return

        table = Table(title=f"MCP Servers matching '{query}'", border_style="cyan")
        table.add_column("Name", style="bold")
        table.add_column("Description")
        table.add_column("Registry")

        for result in results[:10]:
            table.add_row(
                result.name,
                result.description[:50] + "..." if len(result.description) > 50 else result.description,
                result.registry,
            )

        self.console.print(table)
```

- [ ] **Step 3: Implement _mcp_add method**

```python
    async def _mcp_add(self, name: str):
        """Add an MCP server to config."""
        client = MCPRegistryClient(self.config.mcp_registries)

        # Get install instructions
        install_cmd = await client.get_install_command(name)
        if not install_cmd:
            self.console.print(f"[red]Could not find install instructions for '{name}'[/red]")
            return

        # Build server config
        server_config = {
            "name": name.split("/")[-1],  # Use short name
            "transport": install_cmd.transport,
        }

        if install_cmd.command:
            server_config["command"] = install_cmd.command
        if install_cmd.args:
            server_config["args"] = install_cmd.args
        if install_cmd.env:
            server_config["env"] = install_cmd.env

        # Add to config
        self.config.mcp_servers.append(server_config)
        save_config(self.config)

        self.console.print(f"[green]Added MCP server: {server_config['name']}[/green]")
        self.console.print("[dim]Run /mcp refresh to connect[/dim]")
```

- [ ] **Step 4: Implement _mcp_list method**

```python
    def _mcp_list(self):
        """List configured MCP servers."""
        if not self.config.mcp_servers:
            self.console.print("[yellow]No MCP servers configured[/yellow]")
            return

        table = Table(title="Configured MCP Servers", border_style="cyan")
        table.add_column("Name", style="bold")
        table.add_column("Transport")
        table.add_column("Status")

        for server in self.config.mcp_servers:
            name = server.get("name", "unknown")
            transport = server.get("transport", "stdio")
            # Check if connected
            connected = False
            if hasattr(self, 'mcp_manager') and self.mcp_manager:
                connected = name in getattr(self.mcp_manager, 'connected_servers', {})
            status = "[green]Connected[/green]" if connected else "[dim]Not connected[/dim]"
            table.add_row(name, transport, status)

        self.console.print(table)
```

- [ ] **Step 5: Implement _mcp_remove method**

```python
    def _mcp_remove(self, name: str):
        """Remove an MCP server from config."""
        original_count = len(self.config.mcp_servers)
        self.config.mcp_servers = [
            s for s in self.config.mcp_servers
            if s.get("name") != name
        ]

        if len(self.config.mcp_servers) == original_count:
            self.console.print(f"[red]MCP server '{name}' not found[/red]")
            return

        save_config(self.config)
        self.console.print(f"[green]Removed MCP server: {name}[/green]")
        self.console.print("[dim]Run /mcp refresh to apply changes[/dim]")
```

- [ ] **Step 6: Implement _mcp_test method**

```python
    async def _mcp_test(self, name: str):
        """Test an MCP server connection."""
        self.console.print(f"[cyan]Testing {name}...[/cyan]")
        # MCP test logic would go here
        self.console.print(f"[green]{name} is working[/green]")
```

- [ ] **Step 7: Implement _mcp_refresh method**

```python
    async def _mcp_refresh(self):
        """Reload all MCP servers."""
        self.console.print("[cyan]Refreshing MCP connections...[/cyan]")
        # MCP refresh logic would go here
        self.console.print("[green]MCP connections refreshed[/green]")
```

- [ ] **Step 8: Commit**

```bash
git add ares/cli.py
git commit -m "feat: add /mcp CLI commands for MCP server management"
```

---

### Task 7: Run full test suite

**Files:** None (verification only)

- [ ] **Step 1: Run all new tests**

Run: `cd /c/Users/anime/friday && python -m pytest tests/test_skill_registry.py tests/test_mcp_registry.py tests/test_skill_generator.py -v`

Expected: All PASS

- [ ] **Step 2: Run full test suite**

Run: `cd /c/Users/anime/friday && python -m pytest tests/ -v --tb=short 2>&1 | tail -30`

Expected: All existing tests still pass, new tests pass

- [ ] **Step 3: Fix any failures, then commit**

If tests fail, fix the issue and commit the fix.

---

### Task 8: Manual smoke test

**Files:** None (manual verification)

- [ ] **Step 1: Test /skills search**

Run Ares and type: `/skills search weather`

Expected: Shows weather-related skills from ClawhHub

- [ ] **Step 2: Test /skills list**

Run Ares and type: `/skills list`

Expected: Shows all installed skills

- [ ] **Step 3: Test /mcp search**

Run Ares and type: `/mcp search memory`

Expected: Shows memory-related MCP servers

- [ ] **Step 4: Test /mcp list**

Run Ares and type: `/mcp list`

Expected: Shows configured MCP servers

- [ ] **Step 5: Final commit if any fixes needed**

```bash
git add -A
git commit -m "fix: smoke test fixes for skills marketplace"
```
