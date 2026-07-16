"""Focused structured-preview coverage for marketplace tool upgrades."""

from __future__ import annotations

import json
from io import BytesIO
from zipfile import ZipFile

import pytest

from ares.mcp_registry import InstallCommand, MCPResult, MCPServerDetail
from ares.models import AppConfig, SkillDependency
from ares.skill_registry import SkillDetail, SkillResult, SkillVersion
from ares.tools import ToolExecutor
from ares.tools import executor as executor_module
from ares.tools.definitions import get_tool_definitions


class DummyStore:
    pass


def _skill_archive() -> bytes:
    output = BytesIO()
    with ZipFile(output, "w") as archive:
        archive.writestr(
            "weather-skill/SKILL.md",
            "---\n"
            "name: weather-skill\n"
            "description: Safe weather workflow.\n"
            "category: demo\n"
            "version: 1.2.0\n"
            "---\n\n"
            "# Weather Skill\n\n"
            "Use trusted weather data.\n",
        )
    return output.getvalue()


class FakeSkillClient:
    last_errors: dict[str, str] = {}

    def __init__(self) -> None:
        self.archive = _skill_archive()
        self.downloads: list[tuple[str, str | None, str | None]] = []

    async def search(self, query: str, registry: str | None):
        assert query
        return [
            SkillResult(
                slug="weather",
                name="Weather",
                description="Weather summaries.",
                version="1.2.0",
                owner="ares",
                score=1.0,
                registry=registry or "clawhub",
                canonical_url="https://example.test/weather",
            )
        ]

    async def get_skill(self, slug: str, registry: str | None):
        return SkillDetail(
            slug="weather",
            name="Weather",
            description="Weather summaries.",
            version="1.2.0",
            owner="ares",
            registry=registry or "clawhub",
            canonical_url="https://example.test/weather",
            dependencies=[
                SkillDependency(type="mcp_server", name="weather-mcp"),
                SkillDependency(type="mcp_server", name="missing-mcp"),
                SkillDependency(type="tool", name="web_search"),
            ],
            security_status="reviewed",
            files=["SKILL.md"],
        )

    async def get_versions(self, slug: str, registry: str | None):
        return [
            SkillVersion(version="1.2.0", created_at="2026-07-15", security_status="reviewed"),
            SkillVersion(version="1.1.0", created_at="2026-07-01"),
        ]

    async def download(self, slug: str, version: str | None, registry: str | None):
        self.downloads.append((slug, version, registry))
        return self.archive


class FakeMCPClient:
    last_errors: dict[str, str] = {}

    async def search(self, query: str, registry: str | None):
        assert query
        return [
            MCPResult(
                name="io.example/weather",
                title="Weather MCP",
                description="Reads weather data.",
                version="2.0.0",
                repository="https://example.test/weather-mcp",
                registry=registry or "mcp-registry",
                canonical_url="https://example.test/weather-mcp",
                verified=True,
            )
        ]

    async def get_server(self, name: str, registry: str | None):
        return MCPServerDetail(
            name="io.example/weather",
            title="Weather MCP",
            description="Reads weather data.",
            version="2.0.0",
            repository="https://example.test/weather-mcp",
            registry=registry or "mcp-registry",
            verified=True,
            packages=[{"identifier": "weather-mcp"}],
        )

    async def get_install_command(self, name: str, registry: str | None):
        return InstallCommand(
            source_name="io.example/weather",
            server_name="io.example/weather",
            transport="stdio",
            command="uvx",
            args=("weather-mcp==2.0.0",),
            env_requirements=("WEATHER_API_KEY",),
            registry=registry or "mcp-registry",
            repository="https://example.test/weather-mcp",
        )


def _executor(tmp_path, *, mcp_servers=None):
    config = AppConfig(
        data_dir=str(tmp_path / "data"),
        skill_dirs=[str(tmp_path / "skills")],
        mcp_servers=mcp_servers or [],
    )
    return ToolExecutor(DummyStore(), DummyStore(), config=config), config


@pytest.mark.asyncio
async def test_marketplace_legacy_search_output_remains_unchanged(tmp_path, monkeypatch):
    monkeypatch.setattr(executor_module, "SkillRegistryClient", lambda _registries: FakeSkillClient())
    monkeypatch.setattr(executor_module, "MCPRegistryClient", lambda _registries: FakeMCPClient())
    executor, _config = _executor(tmp_path)
    try:
        skill_output = await executor.execute_async("search_skill_marketplace", {"query": "weather"})
        mcp_output = await executor.execute_async("search_mcp_marketplace", {"query": "weather"})
    finally:
        executor.close()

    assert skill_output == "Marketplace skills (1):\n- @ares/weather [clawhub, 1.2.0] — Weather summaries."
    assert mcp_output == "MCP marketplace servers (1):\n- io.example/weather [mcp-registry, verified] — Reads weather data."


@pytest.mark.asyncio
async def test_skill_marketplace_structured_compare_reports_risk_and_dependencies(tmp_path, monkeypatch):
    fake = FakeSkillClient()
    monkeypatch.setattr(executor_module, "SkillRegistryClient", lambda _registries: fake)
    executor, _config = _executor(tmp_path, mcp_servers=[{"name": "weather-mcp"}])
    try:
        raw = await executor.execute_async(
            "search_skill_marketplace",
            {
                "query": "weather",
                "mode": "compare",
                "compare_slugs": ["@ares/weather"],
                "include_versions": True,
                "response_format": "structured",
            },
        )
    finally:
        executor.close()

    payload = json.loads(raw)
    row = payload["data"]["results"][0]
    assert payload["ok"] is True
    assert row["risk"]["security_status"] == "reviewed"
    assert row["permission_summary"]["execution"] == "none"
    assert row["compatibility"]["missing_required"] == ["mcp_server:missing-mcp"]
    assert row["versions"][0]["version"] == "1.2.0"
    assert payload["data"]["comparison"][0]["reference"] == "@ares/weather"


@pytest.mark.asyncio
async def test_skill_install_preview_sandboxes_without_writing_then_pins_confirmed_install(tmp_path, monkeypatch):
    fake = FakeSkillClient()
    monkeypatch.setattr(executor_module, "SkillRegistryClient", lambda _registries: fake)
    executor, _config = _executor(tmp_path, mcp_servers=[{"name": "weather-mcp"}])
    try:
        preview_raw = await executor.execute_async(
            "install_marketplace_skill",
            {
                "slug": "weather",
                "preview": True,
                "sandbox_validate": True,
                "pin_version": "1.2.0",
                "response_format": "structured",
            },
        )
        preview = json.loads(preview_raw)
        assert preview["status"] == "preview"
        assert preview["data"]["plan"]["sandbox"]["performed"] is True
        assert not (tmp_path / "skills" / "demo" / "weather-skill").exists()
        install_raw = await executor.execute_async(
            "install_marketplace_skill",
            {
                "slug": "weather",
                "pin_version": "1.2.0",
                "sandbox_validate": True,
                "confirm": True,
                "response_format": "structured",
            },
        )
    finally:
        executor.close()

    installed = json.loads(install_raw)
    skill_root = tmp_path / "skills" / "demo" / "weather-skill"
    provenance = json.loads((skill_root / ".ares-marketplace.json").read_text(encoding="utf-8"))
    assert installed["ok"] is True
    assert installed["data"]["plan"]["sandbox"]["performed"] is True
    assert provenance["pinned_version"] == "1.2.0"
    assert fake.downloads[-1][1] == "1.2.0"


@pytest.mark.asyncio
async def test_mcp_marketplace_structured_inspection_and_preview_never_executes_or_saves(tmp_path, monkeypatch):
    monkeypatch.setattr(executor_module, "MCPRegistryClient", lambda _registries: FakeMCPClient())
    persisted = []
    monkeypatch.setattr(executor_module, "save_config", persisted.append)
    executor, config = _executor(tmp_path)
    try:
        search_raw = await executor.execute_async(
            "search_mcp_marketplace",
            {
                "query": "weather",
                "mode": "inspect",
                "response_format": "structured",
            },
        )
        preview_raw = await executor.execute_async(
            "add_marketplace_mcp",
            {
                "name": "io.example/weather",
                "preview": True,
                "sandbox_validate": True,
                "pin_version": "2.0.0",
                "response_format": "structured",
            },
        )
    finally:
        executor.close()

    search = json.loads(search_raw)
    preview = json.loads(preview_raw)
    assert search["data"]["results"][0]["compatibility"]["install_plan_available"] is True
    assert search["data"]["results"][0]["plan"]["command"] == "uvx"
    assert search["data"]["results"][0]["permission_summary"]["execution"] == "none"
    assert preview["status"] == "preview"
    assert preview["data"]["sandbox"]["performed"] is False
    assert "never executed" in preview["data"]["sandbox"]["reason"]
    assert config.mcp_servers == []
    assert persisted == []


@pytest.mark.asyncio
async def test_mcp_marketplace_confirmed_pin_is_persisted_only_after_confirmation(tmp_path, monkeypatch):
    monkeypatch.setattr(executor_module, "MCPRegistryClient", lambda _registries: FakeMCPClient())
    persisted = []
    monkeypatch.setattr(executor_module, "save_config", persisted.append)
    executor, config = _executor(tmp_path)
    try:
        blocked_raw = await executor.execute_async(
            "add_marketplace_mcp",
            {
                "name": "io.example/weather",
                "pin_version": "2.0.0",
                "response_format": "structured",
            },
        )
        added_raw = await executor.execute_async(
            "add_marketplace_mcp",
            {
                "name": "io.example/weather",
                "pin_version": "2.0.0",
                "confirm": True,
                "response_format": "structured",
            },
        )
    finally:
        executor.close()

    blocked = json.loads(blocked_raw)
    added = json.loads(added_raw)
    assert blocked["ok"] is False
    assert blocked["errors"][0]["code"] == "confirmation_required"
    assert added["ok"] is True
    assert config.mcp_servers[0]["marketplace"]["pinned_version"] == "2.0.0"
    assert persisted == [config]


def test_marketplace_tool_schemas_expose_opt_in_upgrade_fields():
    definitions = {item["function"]["name"]: item["function"]["parameters"] for item in get_tool_definitions()}

    assert {"mode", "compare_slugs", "include_details", "response_format"}.issubset(
        definitions["search_skill_marketplace"]["properties"]
    )
    assert {"preview", "sandbox_validate", "pin_version", "replace", "response_format"}.issubset(
        definitions["install_marketplace_skill"]["properties"]
    )
    assert {"mode", "compare_names", "include_details", "response_format"}.issubset(
        definitions["search_mcp_marketplace"]["properties"]
    )
    assert {"preview", "sandbox_validate", "pin_version", "replace", "response_format"}.issubset(
        definitions["add_marketplace_mcp"]["properties"]
    )
