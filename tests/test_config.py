import json
from pathlib import Path

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


def test_browser_mode_config_has_safe_defaults_and_validates_port():
    config = AppConfig()

    assert config.browser_mode == "auto"
    assert config.browser_cdp_port == 9222
    assert config.browser_chrome_path == ""
    assert config.browser_extension_token == ""
    playwright = next(server for server in config.mcp_servers if server["name"] == "playwright")
    profile = playwright["args"][playwright["args"].index("--user-data-dir") + 1]
    assert Path(profile).is_absolute()
    try:
        AppConfig(browser_cdp_port=70000)
    except Exception as exc:
        assert "browser_cdp_port" in str(exc)
    else:
        raise AssertionError("an invalid CDP port must be rejected")


def test_telegram_config_defaults_to_disabled_and_parses_allowlist(tmp_path, monkeypatch):
    from ares import config as config_module

    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps({"telegram": {"enabled": True, "allowed_chat_ids": [12345]}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(config_module, "CONFIG_PATH", config_path)

    loaded = config_module.load_config()

    assert loaded.telegram.enabled is True
    assert loaded.telegram.allowed_chat_ids == [12345]
    assert loaded.telegram.bot_token == ""
    assert loaded.telegram.audio_transcription_enabled is True
    assert loaded.telegram.audio_stt_backend == "auto"
