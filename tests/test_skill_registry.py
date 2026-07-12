"""Regression coverage for the community skills registry boundary."""

from __future__ import annotations

from io import BytesIO
from zipfile import ZipFile

import httpx
import pytest

from ares.models import SkillRegistry
from ares.skill_registry import SafeSkillInstaller, SkillRegistryClient, SkillValidationError
from ares.skills import SkillManager


def _client_factory(handler):
    transport = httpx.MockTransport(handler)
    return lambda: httpx.AsyncClient(transport=transport)


def _archive(files: dict[str, str]) -> bytes:
    output = BytesIO()
    with ZipFile(output, "w") as bundle:
        for name, content in files.items():
            bundle.writestr(name, content)
    return output.getvalue()


def test_client_sorts_priority_and_filters_disabled_registries():
    client = SkillRegistryClient(
        [
            SkillRegistry(name="low", api_base="https://low.test", priority=1),
            SkillRegistry(name="off", api_base="https://off.test", priority=99, enabled=False),
            SkillRegistry(name="high", api_base="https://high.test", priority=10),
        ]
    )

    assert [registry.name for registry in client.registries] == ["high", "low"]


@pytest.mark.asyncio
async def test_clawhub_search_details_and_versions_normalize_current_api_shapes():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["User-Agent"] == "ares-marketplace/0.1"
        if request.url.path.endswith("/search"):
            assert request.url.params["q"] == "weather"
            assert request.url.params["nonSuspiciousOnly"] == "true"
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "skill": {
                                "slug": "weather",
                                "displayName": "Weather",
                                "summary": "Forecasts without guesswork.",
                                "tags": {"latest": "1.2.0"},
                            },
                            "owner": {"handle": "ares"},
                            "score": 0.9,
                            "stats": {"stars": 42, "downloads": 900},
                        }
                    ]
                },
            )
        if request.url.path.endswith("/skills/weather"):
            return httpx.Response(
                200,
                json={
                    "skill": {"slug": "weather", "displayName": "Weather", "summary": "Forecasts"},
                    "latestVersion": {"version": "1.2.0", "files": [{"path": "SKILL.md"}]},
                    "owner": {"handle": "ares"},
                    "metadata": {"requires": {"mcp": ["weather-api"]}},
                    "moderation": {"verdict": "clean", "isSuspicious": False},
                },
            )
        if request.url.path.endswith("/skills/weather/versions"):
            return httpx.Response(200, json={"versions": [{"version": "1.2.0", "changelog": "Better data"}]})
        raise AssertionError(f"Unexpected request: {request.url}")

    client = SkillRegistryClient(
        [SkillRegistry(name="clawhub", api_base="https://clawhub.ai/api/v1", priority=10)],
        client_factory=_client_factory(handler),
    )

    results = await client.search("weather")
    detail = await client.get_skill("weather")
    versions = await client.get_versions("weather")

    assert results[0].slug == "weather"
    assert results[0].reference == "@ares/weather"
    assert results[0].canonical_url == "https://clawhub.ai/ares/skills/weather"
    assert detail is not None
    assert detail.dependencies[0].name == "weather-api"
    assert detail.security_status == "clean"
    assert results[0].stars == 42
    assert results[0].downloads == 900
    assert versions[0].changelog == "Better data"


@pytest.mark.asyncio
async def test_search_keeps_results_when_a_secondary_registry_is_unavailable():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "good.test":
            return httpx.Response(200, json={"results": [{"slug": "demo", "summary": "A good skill"}]})
        return httpx.Response(503, text="down")

    client = SkillRegistryClient(
        [
            SkillRegistry(name="good", api_base="https://good.test", priority=10),
            SkillRegistry(name="bad", api_base="https://bad.test", priority=5),
        ],
        client_factory=_client_factory(handler),
    )

    results = await client.search("demo")

    assert [item.slug for item in results] == ["demo"]
    assert "HTTP 503" in client.last_errors["bad"]


@pytest.mark.asyncio
async def test_download_refuses_external_json_handoff_instead_of_following_it():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["slug"] == "demo"
        assert request.url.params["ownerHandle"] == "ares"
        return httpx.Response(200, json={"sourceRef": "public-github"})

    client = SkillRegistryClient(
        [SkillRegistry(name="clawhub", api_base="https://clawhub.ai/api/v1")],
        client_factory=_client_factory(handler),
    )

    assert await client.download("@ares/demo") is None
    assert "external source handoff" in client.last_errors["clawhub"]


@pytest.mark.asyncio
async def test_publish_uses_configured_bearer_token_and_validated_multipart_files(tmp_path):
    skill_dir = tmp_path / "publishable"
    skill_dir.mkdir()
    skill_file = skill_dir / "SKILL.md"
    skill_file.write_text(
        "---\nname: publishable\ndescription: A safe skill for publishing.\nversion: 1.0.0\n---\n\n# Publishable\n\n1. Verify work.\n",
        encoding="utf-8",
    )
    skill = SkillManager.parse_skill_file(skill_file)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/api/v1/skills"
        assert request.headers["Authorization"] == "Bearer clh-private"
        assert b'name="files[]"' in request.content
        assert b'clh-private' not in request.content
        return httpx.Response(200, json={"slug": "publishable", "url": "https://clawhub.ai/test/skills/publishable"})

    client = SkillRegistryClient(
        [SkillRegistry(name="clawhub", api_base="https://clawhub.ai/api/v1", auth_token="clh-private")],
        client_factory=_client_factory(handler),
    )

    response = await client.publish(skill=skill)

    assert response["slug"] == "publishable"


def test_safe_installer_installs_only_validated_instruction_files_and_tracks_dependencies(tmp_path):
    archive = _archive(
        {
            "weather/SKILL.md": "---\nname: weather\ndescription: Provides a weather workflow.\ncategory: research\nrequires:\n  mcp:\n    - weather-api\n---\n\n# Weather\n\n1. Ask for a place.\n2. Verify the forecast.\n",
            "weather/references/api.md": "# API\nUse the user's configured provider.\n",
        }
    )

    installation = SafeSkillInstaller(tmp_path).install(
        archive,
        provenance={"registry": "clawhub", "slug": "weather", "version": "1.0.0"},
    )

    assert installation.path == tmp_path / "research" / "weather"
    assert (installation.path / "SKILL.md").exists()
    assert installation.dependencies[0].name == "weather-api"
    assert "clawhub" in (installation.path / ".ares-marketplace.json").read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "files, expected",
    [
        (
            {
                "skill/SKILL.md": "---\nname: bad\ndescription: A bad workflow for testing.\n---\n\n# Bad\n",
                "../outside.txt": "not allowed",
            },
            "inside the skill directory",
        ),
        (
            {
                "skill/SKILL.md": "---\nname: code\ndescription: Code should not arrive in a skill.\n---\n\n# Code\n",
                "skill/run.ps1": "Start-Process calc",
            },
            "Unsupported skill file",
        ),
    ],
)
def test_safe_installer_rejects_traversal_and_executable_content_without_writing(tmp_path, files, expected):
    with pytest.raises(SkillValidationError, match=expected):
        SafeSkillInstaller(tmp_path).install(_archive(files))

    assert list(tmp_path.rglob("SKILL.md")) == []
