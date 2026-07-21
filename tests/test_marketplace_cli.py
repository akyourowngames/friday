"""CLI-level regression tests for marketplace command dispatch and confirmation."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from ares.cli import marketplace as marketplace_module
from ares.integrations.mcp_registry import MCPResult
from ares.models import AppConfig
from ares.skills.registry import SkillResult
from tests.test_cli import make_cli


@pytest.mark.asyncio
async def test_skills_search_dispatches_to_registry_and_renders_results(monkeypatch):
    class FakeSkillClient:
        last_errors = {}

        async def search(self, query, registry):
            assert query == "weather forecast"
            assert registry == "clawhub"
            return [
                SkillResult(
                    slug="weather",
                    name="Weather",
                    description="Forecasts",
                    version="1.0.0",
                    owner="ares",
                    score=1.0,
                    registry="clawhub",
                )
            ]

    app = make_cli()
    monkeypatch.setattr(marketplace_module, "SkillRegistryClient", lambda _registries: FakeSkillClient())

    assert await app._handle_marketplace_command('/skills search "weather forecast" --registry clawhub')

    output = app.console_file.getvalue()
    assert "Marketplace Skills" in output
    assert "weather" in output


@pytest.mark.asyncio
async def test_mcp_add_shows_plan_then_requires_explicit_confirmation(monkeypatch):
    app = make_cli()
    app.config = AppConfig(mcp_servers=[])
    persisted = []
    app._reconfigure_marketplace_mcp = AsyncMock()
    monkeypatch.setattr(marketplace_module, "save_config", persisted.append)

    assert await app._handle_marketplace_command("/mcp add playwright")
    assert app.config.mcp_servers == []
    assert "Confirmation is required" in app.console_file.getvalue()

    assert await app._handle_marketplace_command("/mcp add playwright --yes")
    assert [server["name"] for server in app.config.mcp_servers] == ["playwright"]
    assert persisted == [app.config]
    app._reconfigure_marketplace_mcp.assert_awaited_once()


@pytest.mark.asyncio
async def test_mcp_search_dispatches_to_registry_and_renders_results(monkeypatch):
    class FakeMCPClient:
        last_errors = {}

        async def search(self, query, registry):
            assert query == "memory"
            assert registry is None
            return [
                MCPResult(
                    name="io.example/memory",
                    title="Memory",
                    description="Persistent memory",
                    version="1.0.0",
                    repository="",
                    registry="mcp-registry",
                    verified=True,
                )
            ]

    app = make_cli()
    monkeypatch.setattr(marketplace_module, "MCPRegistryClient", lambda _registries: FakeMCPClient())

    assert await app._handle_marketplace_command("/mcp search memory")

    output = app.console_file.getvalue()
    assert "MCP Marketplace" in output
    assert "io.example/memory" in output
