"""Agent-tool safety tests for the marketplace integration."""

from __future__ import annotations

import pytest

from ares.models import AppConfig
from ares.tools import ToolExecutor
from ares.tools import executor as executor_module


class DummyStore:
    pass


@pytest.mark.asyncio
async def test_agent_marketplace_mutations_require_explicit_confirmation(tmp_path, monkeypatch):
    config = AppConfig(data_dir=str(tmp_path / "data"), mcp_servers=[])
    executor = ToolExecutor(DummyStore(), DummyStore(), config=config)
    persisted = []
    monkeypatch.setattr(executor_module, "save_config", persisted.append)

    try:
        blocked_install = await executor.execute_async("install_marketplace_skill", {"slug": "weather"})
        blocked_mcp = await executor.execute_async("add_marketplace_mcp", {"name": "playwright"})
        added_mcp = await executor.execute_async(
            "add_marketplace_mcp", {"name": "playwright", "confirm": True}
        )
    finally:
        executor.close()

    assert blocked_install.startswith("CONFIRM REQUIRED")
    assert blocked_mcp.startswith("CONFIRM REQUIRED")
    assert "Added to shared config" in added_mcp
    assert [server["name"] for server in config.mcp_servers] == ["playwright"]
    assert persisted == [config]
