# Browser Modes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add triple browser mode support — isolated Playwright Chrome, system Chrome via CDP attach, and extension mode for existing tabs — with auto-detect, CLI commands, and a Chrome launch helper.

**Architecture:** New `ares/browser.py` module with `BrowserManager` class that builds mode-aware Playwright MCP args, detects CDP availability, launches Chrome, and manages the Playwright extension. Config gets three new fields. CLI gets `/browser` command group. `_ensure_mcp_defaults()` in config.py becomes mode-aware.

**Tech Stack:** Python stdlib (socket, subprocess, platform, os, pathlib), Rich, prompt_toolkit

## Browser Modes Research Summary

### Mode 1: Isolated (Default)
- Playwright launches its own Chrome with an empty profile
- No access to user's real sessions/cookies
- Safe, sandboxed
- MCP args: `--browser chrome --user-data-dir ~/.ares/data/playwright-profile`

### Mode 2: CDP Endpoint (System Chrome)
- Launch Chrome with `--remote-debugging-port=9222`
- Playwright connects via `--cdp-endpoint http://localhost:9222`
- **Issue:** Chrome profile lock — can't have two instances using same profile
- **Solution:** Either close all Chrome windows first, or use separate `--user-data-dir`
- MCP args: `--cdp-endpoint http://localhost:9222`

### Mode 3: Extension (Recommended for Real Chrome)
- Install Playwright Chrome Extension from Chrome Web Store
- Use `--extension` flag in Playwright MCP
- Connects to existing browser tabs without launching new instance
- **No profile lock issues** — works with your existing Chrome
- Preserves all logged-in sessions, cookies, extensions
- User selects which tab to control on first interaction
- MCP args: `--extension`

### Comparison Table

| Feature | Isolated | CDP Endpoint | Extension |
|---------|----------|--------------|-----------|
| Launches new Chrome | Yes | Yes | No |
| Uses real profile | No | Yes (risky) | Yes (safe) |
| Profile lock issues | No | Yes | No |
| Real cookies/sessions | No | Yes | Yes |
| Installation required | No | No | Chrome extension |
| Tab selection | N/A | No | Yes |
| Connection approval | No | No | Yes (bypassable) |
| Best for | Testing | Automation | Personal use |

## Research Findings & Best Practices

### Key Discovery: Extension Mode is Superior for Personal Use

The Playwright Chrome Extension (`--extension`) is the recommended approach for accessing real Chrome because:

1. **No Profile Lock Conflicts** — Unlike CDP endpoint mode, extension mode doesn't require launching a separate Chrome instance. It connects to your existing browser, so there's no conflict with your daily-driver Chrome.

2. **Preserves Full Browser State** — Extensions, saved passwords, bookmarks, history, and all logged-in sessions are immediately available.

3. **User Control via Tab Selection** — On first connection, the extension shows a dialog letting you choose which tab the AI assistant controls. This is safer than CDP mode where all tabs are accessible.

4. **Bypassable Approval** — The `PLAYWRIGHT_MCP_EXTENSION_TOKEN` environment variable allows auto-approval for trusted connections.

### CDP Endpoint Mode Limitations

- **Chrome Profile Lock:** Chrome uses a file lock on the user data directory. Two Chrome instances cannot share the same profile simultaneously.
- **Restart Required:** Must close all Chrome windows before launching with `--remote-debugging-port`.
- **Security Risk:** All tabs are accessible without tab selection.
- **Fragile:** If Chrome crashes, the CDP connection is lost and must be re-established.

### Extension Mode Requirements

- **Chrome/Edge/Chromium only** — No Firefox or Safari support.
- **Extension installation** — One-time setup from Chrome Web Store.
- **Connection approval** — First connection requires manual approval (bypassable with token).

### Auto-Mode Priority Order

When `browser_mode` is set to `"auto"`, the priority order is:
1. **Extension** (if token configured) — Safest, most convenient
2. **CDP Endpoint** (if port is open) — Good for automation
3. **Isolated** (fallback) — Always works, no dependencies

This ensures the assistant prefers the safest, most user-friendly mode first.

---

## File Map

| File | Action | Responsibility |
|------|--------|---------------|
| `ares/browser.py` | **Create** | BrowserManager: mode detection, MCP arg building, Chrome launch, extension management, NLP parsing |
| `ares/models.py` | **Modify** | Add `browser_mode`, `browser_cdp_port`, `browser_chrome_path`, `browser_extension_token` to AppConfig |
| `ares/config.py` | **Modify** | Import BrowserManager, make `_ensure_mcp_defaults()` mode-aware, add CDP/extension fallback on load |
| `ares/cli.py` | **Modify** | Import BrowserManager, create instance, add `/browser` command group |
| `tests/test_browser.py` | **Create** | Unit tests for BrowserManager methods |

---

### Task 1: Add browser config fields to AppConfig

**Files:**
- Modify: `ares/models.py`

- [ ] **Step 1: Add the four new fields**

In `ares/models.py`, add after the `cron_log_retention_days` field (line ~128) inside the `AppConfig` class:

```python
    browser_mode: str = "auto"           # "isolated" | "system" | "extension" | "auto"
    browser_cdp_port: int = 9222
    browser_chrome_path: str = ""        # auto-detected if empty
    browser_extension_token: str = ""    # Playwright extension auth token (bypass approval)
```

- [ ] **Step 2: Verify model loads with defaults**

Run: `cd /c/Users/anime/friday && python -c "from ares.models import AppConfig; c = AppConfig(); print(f'mode={c.browser_mode} port={c.browser_cdp_port} path={repr(c.browser_chrome_path)}')"`
Expected: `mode=auto port=9222 path=''`

- [ ] **Step 3: Commit**

```bash
git add ares/models.py
git commit -m "feat: add browser_mode, browser_cdp_port, browser_chrome_path to AppConfig"
```

---

### Task 2: Create BrowserManager — tests first

**Files:**
- Create: `ares/browser.py`
- Create: `tests/test_browser.py`

- [ ] **Step 1: Write failing tests for get_mcp_args**

Create `tests/test_browser.py`:

```python
"""Tests for BrowserManager."""

from unittest.mock import patch, MagicMock
import socket

from ares.browser import BrowserManager
from ares.models import AppConfig


class TestGetMcpArgs:
    def test_isolated_mode_returns_isolated_args(self):
        config = AppConfig(browser_mode="isolated")
        bm = BrowserManager(config)
        args = bm.get_mcp_args("isolated")
        assert "@playwright/mcp@latest" in args
        assert "--browser" in args
        assert "chrome" in args
        assert "--user-data-dir" in args

    def test_system_mode_returns_cdp_args(self):
        config = AppConfig(browser_mode="system")
        bm = BrowserManager(config)
        args = bm.get_mcp_args("system")
        assert "@playwright/mcp@latest" in args
        assert "--cdp-endpoint" in args
        assert "http://localhost:9222" in args
        assert "--user-data-dir" not in args

    def test_extension_mode_returns_extension_args(self):
        config = AppConfig(browser_mode="extension")
        bm = BrowserManager(config)
        args = bm.get_mcp_args("extension")
        assert "@playwright/mcp@latest" in args
        assert "--extension" in args
        assert "--user-data-dir" not in args
        assert "--cdp-endpoint" not in args

    def test_extension_mode_with_token(self):
        config = AppConfig(browser_mode="extension", browser_extension_token="test-token-123")
        bm = BrowserManager(config)
        args = bm.get_mcp_args("extension")
        assert "--extension" in args
        # Token is passed via env, not args
        env = bm.get_mcp_env("extension")
        assert env.get("PLAYWRIGHT_MCP_EXTENSION_TOKEN") == "test-token-123"

    def test_auto_mode_uses_extension_when_available(self):
        config = AppConfig(browser_mode="auto")
        bm = BrowserManager(config)
        with patch.object(bm, "detect_chrome_cdp", return_value=False):
            with patch.object(bm, "detect_extension_available", return_value=True):
                args = bm.get_mcp_args("auto")
                assert "--extension" in args

    def test_auto_mode_uses_system_when_cdp_available(self):
        config = AppConfig(browser_mode="auto")
        bm = BrowserManager(config)
        with patch.object(bm, "detect_chrome_cdp", return_value=True):
            with patch.object(bm, "detect_extension_available", return_value=False):
                args = bm.get_mcp_args("auto")
                assert "--cdp-endpoint" in args

    def test_auto_mode_uses_isolated_when_nothing_available(self):
        config = AppConfig(browser_mode="auto")
        bm = BrowserManager(config)
        with patch.object(bm, "detect_chrome_cdp", return_value=False):
            with patch.object(bm, "detect_extension_available", return_value=False):
                args = bm.get_mcp_args("auto")
                assert "--user-data-dir" in args

    def test_none_mode_uses_config_default(self):
        config = AppConfig(browser_mode="isolated")
        bm = BrowserManager(config)
        args = bm.get_mcp_args(None)
        assert "--user-data-dir" in args

    def test_custom_cdp_port(self):
        config = AppConfig(browser_mode="system", browser_cdp_port=9223)
        bm = BrowserManager(config)
        args = bm.get_mcp_args("system")
        assert "http://localhost:9223" in args
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /c/Users/anime/friday && python -m pytest tests/test_browser.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'ares.browser'`

- [ ] **Step 3: Implement BrowserManager with get_mcp_args and detect_chrome_cdp**

Create `ares/browser.py`:

```python
"""Browser mode management — isolated Playwright, system Chrome via CDP, or extension."""

from __future__ import annotations

import logging
import os
import platform
import socket
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)


class BrowserManager:
    """Manages browser modes: isolated Playwright, system Chrome via CDP, or extension."""

    def __init__(self, config):
        self.config = config

    def get_mcp_args(self, mode: str | None = None) -> list[str]:
        """Build Playwright MCP args for the given mode."""
        mode = mode or self.config.browser_mode

        # Extension mode — connects to existing browser tabs
        if mode == "extension":
            return self._extension_args()

        # Auto mode — try extension first, then CDP, then isolated
        if mode == "auto":
            if self.detect_extension_available():
                return self._extension_args()
            if self.detect_chrome_cdp():
                return self._cdp_args()
            return self._isolated_args()

        # System mode — connect via CDP
        if mode == "system":
            return self._cdp_args()

        # Isolated mode (default)
        return self._isolated_args()

    def get_mcp_env(self, mode: str | None = None) -> dict[str, str]:
        """Build environment variables for Playwright MCP."""
        mode = mode or self.config.browser_mode
        env = {}

        if mode == "extension" and self.config.browser_extension_token:
            env["PLAYWRIGHT_MCP_EXTENSION_TOKEN"] = self.config.browser_extension_token

        return env

    def _isolated_args(self) -> list[str]:
        """Args for isolated Playwright Chrome with empty profile."""
        return [
            "@playwright/mcp@latest",
            "--browser",
            "chrome",
            "--caps",
            "vision,devtools",
            "--user-data-dir",
            "~/.ares/data/playwright-profile",
            "--viewport-size",
            "1280x720",
        ]

    def _cdp_args(self) -> list[str]:
        """Args for connecting to Chrome via CDP endpoint."""
        return [
            "@playwright/mcp@latest",
            "--cdp-endpoint",
            f"http://localhost:{self.config.browser_cdp_port}",
            "--caps",
            "vision,devtools",
        ]

    def _extension_args(self) -> list[str]:
        """Args for Playwright extension mode (existing browser tabs)."""
        return [
            "@playwright/mcp@latest",
            "--extension",
        ]

    def detect_chrome_cdp(self, port: int | None = None) -> bool:
        """Check if Chrome CDP is available on the given port."""
        port = port or self.config.browser_cdp_port
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(1)
                return s.connect_ex(("127.0.0.1", port)) == 0
        except Exception:
            return False

    def detect_extension_available(self) -> bool:
        """Check if Playwright extension is likely available.

        Returns True if an extension token is configured (user has installed extension).
        We can't detect the extension directly, so we rely on config.
        """
        return bool(self.config.browser_extension_token)

    def launch_system_chrome(self, port: int | None = None) -> str:
        """Launch Chrome with remote debugging enabled."""
        port = port or self.config.browser_cdp_port
        exe, profile = self._chrome_paths()

        try:
            subprocess.Popen(
                [exe, f"--remote-debugging-port={port}", f"--user-data-dir={profile}"],
            )
            return (
                f"Launched Chrome with CDP on :{port}\n"
                f"Profile: {profile}\n"
                "Close ALL other Chrome windows first — Chrome ignores the debug "
                "flag if the profile is already open."
            )
        except FileNotFoundError:
            return (
                f"Chrome not found at {exe}. Install Chrome or set "
                "browser_chrome_path in config."
            )
        except Exception as exc:
            return f"Failed to launch Chrome: {exc}"

    def _chrome_paths(self) -> tuple[str, str]:
        """Return (executable_path, profile_dir) for the current platform."""
        if self.config.browser_chrome_path:
            exe = self.config.browser_chrome_path
        else:
            exe = self._default_chrome_exe()

        profile = self._default_chrome_profile()
        return exe, profile

    @staticmethod
    def _default_chrome_exe() -> str:
        system = platform.system()
        if system == "Darwin":
            return "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
        if system == "Windows":
            return str(
                Path(os.environ.get("PROGRAMFILES", "C:/Program Files"))
                / "Google/Chrome/Application/chrome.exe"
            )
        return "google-chrome"

    @staticmethod
    def _default_chrome_profile() -> str:
        system = platform.system()
        if system == "Darwin":
            return str(Path("~/Library/Application Support/Google/Chrome").expanduser())
        if system == "Windows":
            return str(
                Path(os.environ.get("LOCALAPPDATA", ""))
                / "Google/Chrome/User Data"
            )
        return str(Path("~/.config/google-chrome").expanduser())

    def get_mode_from_request(self, text: str) -> str | None:
        """Parse natural language for browser mode hints."""
        lower = text.lower()
        # Extension mode hints
        if any(phrase in lower for phrase in ["my tabs", "my browser tabs", "existing tabs", "logged in"]):
            return "extension"
        # System/CDP mode hints
        if any(phrase in lower for phrase in ["my chrome", "my browser", "system browser", "real chrome"]):
            return "system"
        # Isolated mode hints
        if any(phrase in lower for phrase in ["isolated", "sandbox", "clean browser"]):
            return "isolated"
        return None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /c/Users/anime/friday && python -m pytest tests/test_browser.py::TestGetMcpArgs -v`
Expected: All 6 tests PASS

- [ ] **Step 5: Commit**

```bash
git add ares/browser.py tests/test_browser.py
git commit -m "feat: add BrowserManager with get_mcp_args and detect_chrome_cdp"
```

---

### Task 3: Add tests for detect_chrome_cdp, get_mode_from_request, _chrome_paths

**Files:**
- Modify: `tests/test_browser.py`

- [ ] **Step 1: Write failing tests for detect_chrome_cdp**

Add to `tests/test_browser.py`:

```python
class TestDetectChromeCdp:
    def test_returns_true_when_port_open(self):
        config = AppConfig()
        bm = BrowserManager(config)
        with patch("ares.browser.socket.socket") as mock_sock:
            instance = MagicMock()
            mock_sock.return_value.__enter__ = MagicMock(return_value=instance)
            mock_sock.return_value.__exit__ = MagicMock(return_value=False)
            instance.connect_ex.return_value = 0
            assert bm.detect_chrome_cdp(9222) is True

    def test_returns_false_when_port_closed(self):
        config = AppConfig()
        bm = BrowserManager(config)
        with patch("ares.browser.socket.socket") as mock_sock:
            instance = MagicMock()
            mock_sock.return_value.__enter__ = MagicMock(return_value=instance)
            mock_sock.return_value.__exit__ = MagicMock(return_value=False)
            instance.connect_ex.return_value = 111  # Connection refused
            assert bm.detect_chrome_cdp(9222) is False

    def test_returns_false_on_exception(self):
        config = AppConfig()
        bm = BrowserManager(config)
        with patch("ares.browser.socket.socket", side_effect=Exception("oops")):
            assert bm.detect_chrome_cdp(9222) is False

    def test_uses_config_port_by_default(self):
        config = AppConfig(browser_cdp_port=9333)
        bm = BrowserManager(config)
        with patch("ares.browser.socket.socket") as mock_sock:
            instance = MagicMock()
            mock_sock.return_value.__enter__ = MagicMock(return_value=instance)
            mock_sock.return_value.__exit__ = MagicMock(return_value=False)
            instance.connect_ex.return_value = 0
            bm.detect_chrome_cdp()
            instance.connect_ex.assert_called_with(("127.0.0.1", 9333))


class TestGetModeFromRequest:
    def test_detects_my_chrome(self):
        config = AppConfig()
        bm = BrowserManager(config)
        assert bm.get_mode_from_request("open this in my chrome") == "system"

    def test_detects_my_browser(self):
        config = AppConfig()
        bm = BrowserManager(config)
        assert bm.get_mode_from_request("browse with my browser") == "system"

    def test_detects_real_chrome(self):
        config = AppConfig()
        bm = BrowserManager(config)
        assert bm.get_mode_from_request("use real chrome") == "system"

    def test_detects_my_tabs(self):
        config = AppConfig()
        bm = BrowserManager(config)
        assert bm.get_mode_from_request("use my tabs") == "extension"

    def test_detects_existing_tabs(self):
        config = AppConfig()
        bm = BrowserManager(config)
        assert bm.get_mode_from_request("connect to existing tabs") == "extension"

    def test_detects_logged_in(self):
        config = AppConfig()
        bm = BrowserManager(config)
        assert bm.get_mode_from_request("browse logged in sites") == "extension"

    def test_detects_isolated(self):
        config = AppConfig()
        bm = BrowserManager(config)
        assert bm.get_mode_from_request("use an isolated browser") == "isolated"

    def test_detects_sandbox(self):
        config = AppConfig()
        bm = BrowserManager(config)
        assert bm.get_mode_from_request("open in sandbox mode") == "isolated"

    def test_detects_clean_browser(self):
        config = AppConfig()
        bm = BrowserManager(config)
        assert bm.get_mode_from_request("use a clean browser") == "isolated"

    def test_returns_none_for_no_match(self):
        config = AppConfig()
        bm = BrowserManager(config)
        assert bm.get_mode_from_request("search the web for python docs") is None

    def test_case_insensitive(self):
        config = AppConfig()
        bm = BrowserManager(config)
        assert bm.get_mode_from_request("Open in MY CHROME") == "system"


class TestChromePaths:
    def test_custom_chrome_path_overrides_default(self):
        config = AppConfig(browser_chrome_path="/custom/chrome")
        bm = BrowserManager(config)
        exe, profile = bm._chrome_paths()
        assert exe == "/custom/chrome"
        assert "Chrome" in profile or "chrome" in profile.lower()

    def test_default_paths_are_non_empty(self):
        config = AppConfig()
        bm = BrowserManager(config)
        exe, profile = bm._chrome_paths()
        assert exe
        assert profile
```

- [ ] **Step 2: Run all browser tests**

Run: `cd /c/Users/anime/friday && python -m pytest tests/test_browser.py -v`
Expected: All tests PASS

- [ ] **Step 3: Commit**

```bash
git add tests/test_browser.py
git commit -m "test: add tests for detect_chrome_cdp, get_mode_from_request, _chrome_paths"
```

---

### Task 4: Make _ensure_mcp_defaults mode-aware

**Files:**
- Modify: `ares/config.py`

- [ ] **Step 1: Write failing test for mode-aware MCP args**

Add to `tests/test_browser.py`:

```python
class TestConfigIntegration:
    def test_ensure_mcp_defaults_uses_isolated_mode(self):
        from ares.config import _ensure_mcp_defaults
        config = AppConfig(browser_mode="isolated", mcp_servers=[])
        result = _ensure_mcp_defaults(config)
        playwright = next(s for s in result.mcp_servers if s["name"] == "playwright")
        assert "--user-data-dir" in playwright["args"]

    def test_ensure_mcp_defaults_uses_system_mode(self):
        from ares.config import _ensure_mcp_defaults
        config = AppConfig(browser_mode="system", mcp_servers=[])
        bm = BrowserManager(config)
        with patch.object(bm, "detect_chrome_cdp", return_value=True):
            # Patch the BrowserManager instantiation in _ensure_mcp_defaults
            with patch("ares.config.BrowserManager", return_value=bm):
                result = _ensure_mcp_defaults(config)
                playwright = next(s for s in result.mcp_servers if s["name"] == "playwright")
                assert "--cdp-endpoint" in playwright["args"]

    def test_ensure_mcp_defaults_uses_extension_mode(self):
        from ares.config import _ensure_mcp_defaults
        config = AppConfig(browser_mode="extension", mcp_servers=[])
        result = _ensure_mcp_defaults(config)
        playwright = next(s for s in result.mcp_servers if s["name"] == "playwright")
        assert "--extension" in playwright["args"]

    def test_ensure_mcp_defaults_sets_extension_token_env(self):
        from ares.config import _ensure_mcp_defaults
        config = AppConfig(
            browser_mode="extension",
            browser_extension_token="test-token-123",
            mcp_servers=[]
        )
        result = _ensure_mcp_defaults(config)
        playwright = next(s for s in result.mcp_servers if s["name"] == "playwright")
        assert playwright.get("env", {}).get("PLAYWRIGHT_MCP_EXTENSION_TOKEN") == "test-token-123"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /c/Users/anime/friday && python -m pytest tests/test_browser.py::TestConfigIntegration -v`
Expected: FAIL — `_ensure_mcp_defaults` doesn't import BrowserManager yet

- [ ] **Step 3: Update _ensure_mcp_defaults to be mode-aware**

In `ares/config.py`, update the imports and function:

```python
"""Configuration management for Ares."""

import json
import logging
from pathlib import Path

from pydantic import ValidationError

from ares.browser import BrowserManager
from ares.models import AppConfig, DEFAULT_MCP_SERVERS

logger = logging.getLogger(__name__)

DEFAULT_DATA_DIR = Path("~/.ares/data").expanduser()
CONFIG_PATH = Path("~/.ares/config.json").expanduser()


def ensure_data_dir(data_dir: Path | None = None) -> Path:
    """Create the data directory if it doesn't exist. Returns the path."""
    d = data_dir or Path(load_config().data_dir).expanduser()
    d.mkdir(parents=True, exist_ok=True)
    return d


def get_db_path(data_dir: Path | None = None) -> Path:
    """Return the path to the SQLite database file."""
    return ensure_data_dir(data_dir) / "ares.db"


def _ensure_mcp_defaults(config: AppConfig) -> AppConfig:
    """Inject default MCP servers if the user hasn't configured any.

    Builds mode-aware Playwright args based on browser_mode config.
    """
    if not config.mcp_servers:
        config.mcp_servers = [s.copy() for s in DEFAULT_MCP_SERVERS]

    # Update playwright server args based on browser mode
    bm = BrowserManager(config)
    for server in config.mcp_servers:
        if server.get("name") == "playwright":
            server["args"] = bm.get_mcp_args()
            # Add environment variables (e.g., extension token)
            env = bm.get_mcp_env()
            if env:
                server.setdefault("env", {}).update(env)

    return config


def load_config() -> AppConfig:
    """Load config from ~/.ares/config.json, or return defaults.

    Invalid or partially-written config files should not prevent Ares from
    starting; log the problem and continue with safe defaults.
    """
    if CONFIG_PATH.exists():
        try:
            with open(CONFIG_PATH) as f:
                data = json.load(f)
            config = AppConfig(**data)
            return _ensure_mcp_defaults(config)
        except (OSError, json.JSONDecodeError, TypeError, ValidationError) as exc:
            logger.warning("Failed to load config from %s; using defaults: %s", CONFIG_PATH, exc)
    return _ensure_mcp_defaults(AppConfig())


def save_config(config: AppConfig) -> None:
    """Save config to ~/.ares/config.json."""
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_PATH, "w") as f:
        json.dump(config.model_dump(), f, indent=2)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /c/Users/anime/friday && python -m pytest tests/test_browser.py::TestConfigIntegration -v`
Expected: All tests PASS

- [ ] **Step 5: Commit**

```bash
git add ares/config.py tests/test_browser.py
git commit -m "feat: make _ensure_mcp_defaults mode-aware with BrowserManager"
```

---

### Task 5: Add /browser CLI commands

**Files:**
- Modify: `ares/cli.py`

- [ ] **Step 1: Add import and BrowserManager instance**

In `ares/cli.py`, add to imports (after the existing imports):

```python
from ares.browser import BrowserManager
```

In `AresCLI.__init__()`, after `self.profile_manager.ensure_exists()` (around line 107), add:

```python
        self.browser_manager = BrowserManager(self.config)
```

- [ ] **Step 2: Add /browser to COMPLETER**

In the `COMPLETER` list, add `"/browser"`:

```python
COMPLETER = WordCompleter([
    "/help", "/memory", "/model", "/clear",
    "/forget", "/export", "/import", "/reset", "/exit",
    "/soul", "/profile", "/context", "/setup", "/browser",
    "/skills", "/skills search", "/skills categories", "/skills load",
], ignore_case=True)
```

- [ ] **Step 3: Add /browser to help table**

In the `/help` section of `_handle_command`, add:

```python
            table.add_row("/browser [status|isolated|system|extension|auto|launch]", "Manage browser mode")
```

- [ ] **Step 4: Add /browser command handler**

In `_handle_command`, add a new `elif` block (after the `/setup` handler):

```python
        elif command == "/browser":
            sub = arg.strip().lower() if arg else "status"

            if sub == "status":
                table = Table(title="Browser", border_style="cyan", show_header=False)
                table.add_column("Setting", style="bold")
                table.add_column("Value")
                cdp_ok = self.browser_manager.detect_chrome_cdp()
                cdp_status = "[green]available[/green]" if cdp_ok else "[dim]not available[/dim]"
                ext_ok = self.browser_manager.detect_extension_available()
                ext_status = "[green]configured[/green]" if ext_ok else "[dim]not configured[/dim]"
                table.add_row("Mode", self.config.browser_mode)
                table.add_row("CDP Port", str(self.config.browser_cdp_port))
                table.add_row("CDP Status", cdp_status)
                table.add_row("Extension", ext_status)
                exe, _ = self.browser_manager._chrome_paths()
                table.add_row("Chrome", exe)
                self.console.print(table)

            elif sub in ("isolated", "system", "extension", "auto"):
                self.config.browser_mode = sub
                save_config(self.config)
                if sub == "system":
                    self.console.print(
                        "[yellow]Warning: System Chrome mode gives Ares access "
                        "to your real cookies and sessions. Only use when needed.[/yellow]"
                    )
                elif sub == "extension":
                    if not self.config.browser_extension_token:
                        self.console.print(
                            "[yellow]Extension mode requires the Playwright Chrome Extension. "
                            "Install from Chrome Web Store and set browser_extension_token in config.[/yellow]"
                        )
                    else:
                        self.console.print(
                            "[green]Extension mode will connect to your existing browser tabs.[/green]"
                        )
                self.console.print(f"[green]Browser mode set to {sub}.[/green]")

            elif sub.startswith("launch"):
                parts = sub.split()
                port = int(parts[1]) if len(parts) > 1 else None
                result = self.browser_manager.launch_system_chrome(port)
                self.console.print(result)

            else:
                self.console.print(
                    "[red]Usage: /browser [status|isolated|system|extension|auto|launch [port]][/red]"
                )
```

- [ ] **Step 5: Verify CLI still imports cleanly**

Run: `cd /c/Users/anime/friday && python -c "from ares.cli import AresCLI; print('OK')"`
Expected: `OK`

- [ ] **Step 6: Commit**

```bash
git add ares/cli.py
git commit -m "feat: add /browser CLI commands for mode management"
```

---

### Task 6: Run full test suite

**Files:** None (verification only)

- [ ] **Step 1: Run all browser tests**

Run: `cd /c/Users/anime/friday && python -m pytest tests/test_browser.py -v`
Expected: All PASS

- [ ] **Step 2: Run full test suite**

Run: `cd /c/Users/anime/friday && python -m pytest tests/ -v --tb=short 2>&1 | tail -30`
Expected: All existing tests still pass, new tests pass

- [ ] **Step 3: Fix any failures, then commit**

If tests fail, fix the issue and commit the fix.

---

### Task 7: Manual smoke test

**Files:** None (manual verification)

- [ ] **Step 1: Test /browser status**

Run: `cd /c/Users/anime/friday && python -c "from ares.browser import BrowserManager; from ares.models import AppConfig; bm = BrowserManager(AppConfig()); print('Isolated:', bm.get_mcp_args('isolated')); print('System:', bm.get_mcp_args('system')); print('Extension:', bm.get_mcp_args('extension'))"`

Verify: Three different arg lists printed

- [ ] **Step 2: Test mode detection**

Run: `cd /c/Users/anime/friday && python -c "from ares.browser import BrowserManager; from ares.models import AppConfig; bm = BrowserManager(AppConfig()); print('CDP available:', bm.detect_chrome_cdp()); print('Extension available:', bm.detect_extension_available()); print('Mode hints:', bm.get_mode_from_request('open in my chrome'), bm.get_mode_from_request('use my tabs'))"`

Verify: CDP status, extension status, and mode hints print correctly

- [ ] **Step 3: Test config integration**

Run: `cd /c/Users/anime/friday && python -c "from ares.config import load_config; c = load_config(); pw = next(s for s in c.mcp_servers if s['name']=='playwright'); print('Args:', pw['args'][:3]); print('Env:', pw.get('env', {}))"`

Verify: Playwright args and env are present and mode-appropriate

---

### Task 8: Install and test Playwright Chrome Extension (Optional)

**Files:** None (manual setup)

This task is optional but recommended for accessing real Chrome with logged-in sessions.

- [ ] **Step 1: Install Playwright Chrome Extension**

1. Open Chrome and go to: https://chromewebstore.google.com/detail/playwright-extension/mmlmfjhmonkocbjadbfplnigmagldckm
2. Click "Add to Chrome"
3. Confirm the installation
4. Note the extension icon in Chrome toolbar

- [ ] **Step 2: Get extension token**

1. Click the Playwright extension icon in Chrome
2. Visit the extension's status page or options
3. Copy the `PLAYWRIGHT_MCP_EXTENSION_TOKEN` value
4. This token authenticates the MCP server with your browser

- [ ] **Step 3: Configure Ares with extension token**

Option A — Via CLI:
```bash
cd /c/Users/anime/friday && python -c "
from ares.config import load_config, save_config
config = load_config()
config.browser_mode = 'extension'
config.browser_extension_token = 'YOUR_TOKEN_HERE'
save_config(config)
print('Config updated!')
"
```

Option B — Edit `~/.ares/config.json` manually:
```json
{
  "browser_mode": "extension",
  "browser_extension_token": "YOUR_TOKEN_HERE"
}
```

- [ ] **Step 4: Test extension mode**

1. Make sure Chrome is open with the extension installed
2. Run Ares: `python -m ares`
3. Type: `/browser status` — should show Extension: configured
4. Type: `/browser extension` — switches to extension mode
5. Ask Ares: "Go to github.com and tell me the page title"
6. A connection approval dialog should appear in Chrome
7. Approve the connection
8. Ares should navigate and read the page

- [ ] **Step 5: Test with logged-in session**

1. Make sure you're logged into a website (e.g., GitHub, Gmail)
2. Ask Ares: "Open GitHub and show me my notifications"
3. Ares should see your logged-in state via the extension
4. Verify it can read your actual notifications

- [ ] **Step 6: Bypass approval (optional)**

To skip approval dialogs on each connection:

1. Copy the extension token from Step 2
2. Make sure it's saved in config (Step 3)
3. The token is passed via environment variable to Playwright MCP
4. Future connections will auto-approve

- [ ] **Step 7: Document setup**

If extension mode works, update the project README with:
- How to install the extension
- How to get the token
- How to configure Ares
- Benefits (real sessions, no profile lock, existing tabs)

- [ ] **Step 4: Final commit if any fixes needed**

```bash
git add -A
git commit -m "fix: smoke test fixes for browser modes"
```
