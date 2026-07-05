# Browser Modes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add dual browser mode support — isolated Playwright Chrome and system Chrome via CDP attach — with auto-detect, CLI commands, and a Chrome launch helper.

**Architecture:** New `ares/browser.py` module with `BrowserManager` class that builds mode-aware Playwright MCP args, detects CDP availability, and launches Chrome. Config gets three new fields. CLI gets `/browser` command group. `_ensure_mcp_defaults()` in config.py becomes mode-aware.

**Tech Stack:** Python stdlib (socket, subprocess, platform, os, pathlib), Rich, prompt_toolkit

---

## File Map

| File | Action | Responsibility |
|------|--------|---------------|
| `ares/browser.py` | **Create** | BrowserManager: mode detection, MCP arg building, Chrome launch, NLP parsing |
| `ares/models.py` | **Modify** | Add `browser_mode`, `browser_cdp_port`, `browser_chrome_path` to AppConfig |
| `ares/config.py` | **Modify** | Import BrowserManager, make `_ensure_mcp_defaults()` mode-aware, add CDP fallback on load |
| `ares/cli.py` | **Modify** | Import BrowserManager, create instance, add `/browser` command group |
| `tests/test_browser.py` | **Create** | Unit tests for BrowserManager methods |

---

### Task 1: Add browser config fields to AppConfig

**Files:**
- Modify: `ares/models.py`

- [ ] **Step 1: Add the three new fields**

In `ares/models.py`, add after the `cron_log_retention_days` field (line ~128) inside the `AppConfig` class:

```python
    browser_mode: str = "auto"           # "isolated" | "system" | "auto"
    browser_cdp_port: int = 9222
    browser_chrome_path: str = ""        # auto-detected if empty
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

    def test_auto_mode_uses_system_when_cdp_available(self):
        config = AppConfig(browser_mode="auto")
        bm = BrowserManager(config)
        with patch.object(bm, "detect_chrome_cdp", return_value=True):
            args = bm.get_mcp_args("auto")
            assert "--cdp-endpoint" in args

    def test_auto_mode_uses_isolated_when_cdp_unavailable(self):
        config = AppConfig(browser_mode="auto")
        bm = BrowserManager(config)
        with patch.object(bm, "detect_chrome_cdp", return_value=False):
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
"""Browser mode management — isolated Playwright or system Chrome via CDP."""

from __future__ import annotations

import logging
import os
import platform
import socket
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)


class BrowserManager:
    """Manages browser modes: isolated Playwright or system Chrome via CDP."""

    def __init__(self, config):
        self.config = config

    def get_mcp_args(self, mode: str | None = None) -> list[str]:
        """Build Playwright MCP args for the given mode."""
        mode = mode or self.config.browser_mode

        if mode == "system" or (mode == "auto" and self.detect_chrome_cdp()):
            return [
                "@playwright/mcp@latest",
                "--cdp-endpoint",
                f"http://localhost:{self.config.browser_cdp_port}",
                "--caps",
                "vision,devtools",
            ]

        # isolated mode
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

    def detect_chrome_cdp(self, port: int | None = None) -> bool:
        """Check if Chrome CDP is available on the given port."""
        port = port or self.config.browser_cdp_port
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(1)
                return s.connect_ex(("127.0.0.1", port)) == 0
        except Exception:
            return False

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
        if any(phrase in lower for phrase in ["my chrome", "my browser", "system browser", "real chrome"]):
            return "system"
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
            table.add_row("/browser [status|isolated|system|auto|launch]", "Manage browser mode")
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
                table.add_row("Mode", self.config.browser_mode)
                table.add_row("CDP Port", str(self.config.browser_cdp_port))
                table.add_row("CDP Status", cdp_status)
                exe, _ = self.browser_manager._chrome_paths()
                table.add_row("Chrome", exe)
                self.console.print(table)

            elif sub in ("isolated", "system", "auto"):
                self.config.browser_mode = sub
                save_config(self.config)
                if sub == "system":
                    self.console.print(
                        "[yellow]Warning: System Chrome mode gives Ares access "
                        "to your real cookies and sessions. Only use when needed.[/yellow]"
                    )
                self.console.print(f"[green]Browser mode set to {sub}.[/green]")

            elif sub.startswith("launch"):
                parts = sub.split()
                port = int(parts[1]) if len(parts) > 1 else None
                result = self.browser_manager.launch_system_chrome(port)
                self.console.print(result)

            else:
                self.console.print(
                    "[red]Usage: /browser [status|isolated|system|auto|launch [port]][/red]"
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

Run: `cd /c/Users/anime/friday && python -c "from ares.browser import BrowserManager; from ares.models import AppConfig; bm = BrowserManager(AppConfig()); print(bm.get_mcp_args('isolated')); print(bm.get_mcp_args('system'))"`

Verify: Two different arg lists printed

- [ ] **Step 2: Test mode detection**

Run: `cd /c/Users/anime/friday && python -c "from ares.browser import BrowserManager; from ares.models import AppConfig; bm = BrowserManager(AppConfig()); print('CDP available:', bm.detect_chrome_cdp()); print('Mode hint:', bm.get_mode_from_request('open in my chrome'))"`

Verify: CDP status prints, mode hint returns "system"

- [ ] **Step 3: Test config integration**

Run: `cd /c/Users/anime/friday && python -c "from ares.config import load_config; c = load_config(); pw = next(s for s in c.mcp_servers if s['name']=='playwright'); print('Args:', pw['args'][:3])"`

Verify: Playwright args are present and mode-appropriate

- [ ] **Step 4: Final commit if any fixes needed**

```bash
git add -A
git commit -m "fix: smoke test fixes for browser modes"
```
