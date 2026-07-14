"""Regression coverage for Playwright browser connection modes."""

import asyncio
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from rich.console import Console

from ares.cli import app as cli_module
from ares.browser import BrowserManager
from ares.cli import AresCLI
from ares.config import _ensure_mcp_defaults
from ares.models import AppConfig


def _playwright(config: AppConfig) -> dict:
    return next(server for server in config.mcp_servers if server["name"] == "playwright")


def test_get_mcp_args_for_all_explicit_modes():
    manager = BrowserManager(AppConfig(browser_cdp_port=9223))

    isolated = manager.get_mcp_args("isolated")
    system = manager.get_mcp_args("system")
    extension = manager.get_mcp_args("extension")

    assert "--browser" in isolated and "--user-data-dir" in isolated
    assert isolated[isolated.index("--user-data-dir") + 1] == str(
        Path("~/.ares/data").expanduser() / "playwright-profile"
    )
    assert "--cdp-endpoint" in system and "http://127.0.0.1:9223" in system
    assert extension == ["-y", "@playwright/mcp@latest", "--extension"]


def test_auto_mode_prioritizes_extension_then_cdp_then_isolated(monkeypatch):
    manager = BrowserManager(AppConfig(browser_mode="auto", browser_extension_token="token"))

    assert "--extension" in manager.get_mcp_args()
    monkeypatch.setattr(manager, "detect_extension_available", lambda: False)
    monkeypatch.setattr(manager, "detect_chrome_cdp", lambda *_args: True)
    assert "--cdp-endpoint" in manager.get_mcp_args()
    monkeypatch.setattr(manager, "detect_chrome_cdp", lambda *_args: False)
    assert "--user-data-dir" in manager.get_mcp_args()


def test_extension_token_is_only_exposed_to_extension_mode():
    manager = BrowserManager(AppConfig(browser_mode="extension", browser_extension_token="extension-secret"))

    assert manager.get_mcp_env() == {"PLAYWRIGHT_MCP_EXTENSION_TOKEN": "extension-secret"}
    assert manager.get_mcp_env("isolated") == {}


def test_detect_chrome_cdp_handles_open_closed_and_errors():
    manager = BrowserManager(AppConfig(browser_cdp_port=9333))
    with patch("ares.browser.socket.socket") as socket_factory:
        connection = MagicMock()
        socket_factory.return_value.__enter__.return_value = connection
        connection.connect_ex.return_value = 0
        assert manager.detect_chrome_cdp() is True
        connection.connect_ex.assert_called_with(("127.0.0.1", 9333))
        connection.connect_ex.return_value = 111
        assert manager.detect_chrome_cdp() is False
    with patch("ares.browser.socket.socket", side_effect=OSError("offline")):
        assert manager.detect_chrome_cdp() is False


def test_mode_hints_and_chrome_path_resolution():
    manager = BrowserManager(AppConfig(browser_chrome_path="C:/custom/chrome.exe"))

    assert manager.get_mode_from_request("Open this in my chrome") == "system"
    assert manager.get_mode_from_request("Use my existing tabs") == "extension"
    assert manager.get_mode_from_request("Use a clean browser") == "isolated"
    assert manager.get_mode_from_request("Research a topic") is None
    executable, profile = manager._chrome_paths()
    assert Path(executable) == Path("C:/custom/chrome.exe")
    assert profile


def test_launch_system_chrome_reports_safe_errors(monkeypatch):
    manager = BrowserManager(AppConfig(browser_chrome_path="C:/chrome.exe"))
    with patch("ares.browser.subprocess.Popen") as launch:
        message = manager.launch_system_chrome(9333)
        assert "CDP on :9333" in message
        assert "Close all other Chrome windows" in message
        assert "--remote-debugging-port=9333" in launch.call_args.args[0]
    with patch("ares.browser.subprocess.Popen", side_effect=FileNotFoundError):
        assert "not found" in manager.launch_system_chrome().lower()
    assert "between 1 and 65535" in manager.launch_system_chrome(70000)


def test_wait_for_chrome_cdp_observes_readiness(monkeypatch):
    manager = BrowserManager(AppConfig())
    attempts = iter([False, True])
    monkeypatch.setattr(manager, "detect_chrome_cdp", lambda: next(attempts))
    monkeypatch.setattr("ares.browser.time.sleep", lambda _seconds: None)

    assert manager.wait_for_chrome_cdp(timeout_seconds=1) is True


def test_config_rewrites_only_playwright_connection_settings():
    config = AppConfig(browser_mode="extension", browser_extension_token="extension-secret", mcp_servers=[])
    _ensure_mcp_defaults(config)

    playwright = _playwright(config)
    windows = next(server for server in config.mcp_servers if server["name"] == "windows")
    assert "--extension" in playwright["args"]
    assert playwright["env"]["PLAYWRIGHT_MCP_EXTENSION_TOKEN"] == "extension-secret"
    assert windows["name"] == "windows"

    config.browser_mode = "isolated"
    _ensure_mcp_defaults(config)
    assert "--user-data-dir" in _playwright(config)["args"]
    assert "PLAYWRIGHT_MCP_EXTENSION_TOKEN" not in _playwright(config).get("env", {})


def test_system_setting_falls_back_to_isolated_until_cdp_is_available(monkeypatch):
    monkeypatch.setattr(BrowserManager, "detect_chrome_cdp", lambda _self, *_args: False)
    config = AppConfig(browser_mode="system", mcp_servers=[])

    _ensure_mcp_defaults(config)

    assert config.browser_mode == "system"
    assert "--user-data-dir" in _playwright(config)["args"]
    manager = BrowserManager(config)
    assert "--cdp-endpoint" in manager.get_mcp_args("system")


def _cli_for_browser_tests() -> tuple[AresCLI, StringIO]:
    output = StringIO()
    app = AresCLI.__new__(AresCLI)
    app.console = Console(file=output, force_terminal=False, color_system=None)
    app.config = AppConfig(mcp_servers=[])
    app.browser_manager = BrowserManager(app.config)
    app._mcp_config_signature = app._get_mcp_config_signature(app.config)
    app._mcp_reconfigure_pending = False
    app.agent = SimpleNamespace(apply_config=lambda _config: None)
    return app, output


def test_browser_cli_commands_persist_mode_and_queue_reconnect(monkeypatch):
    app, output = _cli_for_browser_tests()
    saved = []
    monkeypatch.setattr(cli_module, "save_config", lambda config: saved.append(config.model_dump()))
    monkeypatch.setattr(BrowserManager, "detect_chrome_cdp", lambda _self, *_args: False)

    assert app._handle_command("/browser system") is True
    assert app.config.browser_mode == "system"
    assert app._mcp_reconfigure_pending is True
    assert "--user-data-dir" in _playwright(app.config)["args"]
    assert saved

    assert app._handle_command("/browser status") is True
    assert "Configured mode" in output.getvalue()


def test_browser_launch_reconnects_once_cdp_is_ready(monkeypatch):
    app, output = _cli_for_browser_tests()
    saved = []
    monkeypatch.setattr(cli_module, "save_config", lambda config: saved.append(config.model_dump()))
    monkeypatch.setattr(BrowserManager, "launch_system_chrome", lambda _self, _port=None: "Chrome launched")
    monkeypatch.setattr(BrowserManager, "wait_for_chrome_cdp", lambda _self: True)
    monkeypatch.setattr(BrowserManager, "detect_chrome_cdp", lambda _self, *_args: True)

    assert app._handle_command("/browser launch") is True
    assert "--cdp-endpoint" in _playwright(app.config)["args"]
    assert app._mcp_reconfigure_pending is True
    assert len(saved) == 1
    assert "CDP is ready" in output.getvalue()


def test_browser_mode_hint_reconfigures_before_a_browser_request(monkeypatch):
    app, _output = _cli_for_browser_tests()
    monkeypatch.setattr(cli_module, "save_config", lambda _config: None)
    refreshed = []

    async def refresh():
        refreshed.append(True)

    app._refresh_mcp_manager_if_needed = refresh
    asyncio.run(app._apply_browser_mode_hint("Open GitHub in my chrome"))

    assert app.config.browser_mode == "system"
    assert refreshed == [True]
