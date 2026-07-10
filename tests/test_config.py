import json

from ares.models import AppConfig


def test_load_config_returns_defaults_for_corrupt_json(tmp_path, monkeypatch):
    from ares import config as config_module

    bad_config = tmp_path / "config.json"
    bad_config.write_text("{not-json", encoding="utf-8")
    monkeypatch.setattr(config_module, "CONFIG_PATH", bad_config)

    loaded = config_module.load_config()

    assert isinstance(loaded, AppConfig)
    assert loaded.model


def test_load_config_upgrades_legacy_default_mcp_servers(tmp_path, monkeypatch):
    from ares import config as config_module

    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "mcp_servers": [
                    {"name": "playwright", "command": "npx", "args": ["playwright"]},
                    {"name": "github", "command": "npx", "args": ["github"]},
                    {"name": "fetch", "command": "uvx", "args": ["fetch"]},
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(config_module, "CONFIG_PATH", config_path)

    loaded = config_module.load_config()

    assert {server["name"] for server in loaded.mcp_servers} == {
        "playwright",
        "github",
        "fetch",
        "windows",
    }


def test_load_config_preserves_custom_mcp_servers(tmp_path, monkeypatch):
    from ares import config as config_module

    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps({"mcp_servers": [{"name": "calendar", "server_url": "https://example.com/mcp"}]}),
        encoding="utf-8",
    )
    monkeypatch.setattr(config_module, "CONFIG_PATH", config_path)

    loaded = config_module.load_config()

    assert [server["name"] for server in loaded.mcp_servers] == ["calendar"]


def test_load_config_upgrades_legacy_agent_iteration_default(tmp_path, monkeypatch):
    from ares import config as config_module

    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps({"agent_max_iterations": 20, "mcp_servers": []}),
        encoding="utf-8",
    )
    monkeypatch.setattr(config_module, "CONFIG_PATH", config_path)

    loaded = config_module.load_config()

    assert loaded.agent_max_iterations == 40
    assert "windows" in {server["name"] for server in loaded.mcp_servers}
