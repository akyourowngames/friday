"""Playwright browser-mode management for Ares.

The manager owns only connection configuration.  The MCP server remains the
single browser automation implementation, so browser tools still flow through
the standard MCP lifecycle and retain its readiness diagnostics.
"""

from __future__ import annotations

import os
import platform
import socket
import subprocess
import time
from pathlib import Path
from typing import Literal


BrowserMode = Literal["isolated", "system", "extension", "auto"]
VALID_BROWSER_MODES = frozenset({"isolated", "system", "extension", "auto"})


class BrowserManager:
    """Build and inspect Playwright MCP connection modes.

    ``isolated`` uses Ares' dedicated persistent profile, ``system`` attaches
    to a Chrome instance started with CDP, and ``extension`` connects to a tab
    the user explicitly approves through the official Playwright extension.
    """

    def __init__(self, config) -> None:
        self.config = config

    def resolve_mode(self, mode: str | None = None) -> BrowserMode:
        """Return the effective mode, resolving the safe auto priority."""
        requested = str(mode or self.config.browser_mode or "auto").strip().lower()
        if requested not in VALID_BROWSER_MODES:
            return "isolated"
        if requested == "isolated" or requested == "extension":
            return requested
        if requested == "system":
            # Keep the requested setting intact, but avoid starting a
            # Playwright MCP process that is guaranteed to fail while CDP is
            # down. Passing mode explicitly still lets callers inspect the
            # system-mode argument shape without probing the machine.
            if mode is None and not self.detect_chrome_cdp():
                return "isolated"
            return "system"
        if self.detect_extension_available():
            return "extension"
        if self.detect_chrome_cdp():
            return "system"
        return "isolated"

    def get_mcp_args(self, mode: str | None = None) -> list[str]:
        """Build current Playwright MCP CLI arguments for the selected mode."""
        selected = self.resolve_mode(mode)
        if selected == "extension":
            return self._extension_args()
        if selected == "system":
            return self._cdp_args()
        return self._isolated_args()

    def get_mcp_env(self, mode: str | None = None) -> dict[str, str]:
        """Return only Playwright-specific private environment values."""
        if self.resolve_mode(mode) != "extension":
            return {}
        token = str(self.config.browser_extension_token or "").strip()
        return {"PLAYWRIGHT_MCP_EXTENSION_TOKEN": token} if token else {}

    def detect_chrome_cdp(self, port: int | None = None) -> bool:
        """Return whether a local process is accepting Chrome CDP connections."""
        target_port = int(port or self.config.browser_cdp_port)
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as connection:
                connection.settimeout(1.0)
                return connection.connect_ex(("127.0.0.1", target_port)) == 0
        except (OSError, ValueError):
            return False

    def detect_extension_available(self) -> bool:
        """A configured extension token is the reliable local availability signal."""
        return bool(str(self.config.browser_extension_token or "").strip())

    def wait_for_chrome_cdp(self, timeout_seconds: float = 8.0) -> bool:
        """Wait briefly for a Chrome launched by Ares to expose its CDP port."""
        deadline = time.monotonic() + max(0.0, timeout_seconds)
        while True:
            if self.detect_chrome_cdp():
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.2)

    def launch_system_chrome(self, port: int | None = None) -> str:
        """Launch Chrome with CDP using the user's real Chrome profile.

        Chrome prevents a second process from owning the same profile.  The
        response makes this explicit rather than hiding a profile-lock failure.
        """
        target_port = int(port or self.config.browser_cdp_port)
        if not 1 <= target_port <= 65535:
            return "Browser CDP port must be between 1 and 65535."
        executable, profile = self._chrome_paths()
        try:
            subprocess.Popen(
                [
                    executable,
                    f"--remote-debugging-port={target_port}",
                    f"--user-data-dir={profile}",
                ],
                close_fds=False,
            )
        except FileNotFoundError:
            return (
                f"Chrome was not found at {executable}. Install Chrome or set "
                "browser_chrome_path in Ares settings."
            )
        except OSError as exc:
            return f"Failed to launch Chrome: {exc}"
        return (
            f"Launched Chrome with CDP on :{target_port}.\n"
            f"Profile: {profile}\n"
            "Close all other Chrome windows first; Chrome may ignore the debug flag "
            "when that profile is already in use."
        )

    def _isolated_args(self) -> list[str]:
        # These arguments are passed directly to Node (not through a shell), so
        # expand ``~`` here.  Otherwise the MCP process may treat it as a
        # literal directory named ``~`` instead of Ares' persistent data path.
        profile = str(Path(self.config.data_dir).expanduser() / "playwright-profile")
        return [
            "@playwright/mcp@latest",
            "--browser",
            "chrome",
            "--caps",
            "vision,devtools",
            "--user-data-dir",
            profile,
            "--viewport-size",
            "1280x720",
        ]

    def _cdp_args(self) -> list[str]:
        return [
            "@playwright/mcp@latest",
            "--cdp-endpoint",
            f"http://127.0.0.1:{self.config.browser_cdp_port}",
            "--caps",
            "vision,devtools",
        ]

    @staticmethod
    def _extension_args() -> list[str]:
        return ["@playwright/mcp@latest", "--extension"]

    def _chrome_paths(self) -> tuple[str, str]:
        if str(self.config.browser_chrome_path or "").strip():
            executable = str(Path(self.config.browser_chrome_path).expanduser())
        else:
            executable = self._default_chrome_executable()
        return executable, self._default_chrome_profile()

    @staticmethod
    def _default_chrome_executable() -> str:
        system = platform.system()
        if system == "Darwin":
            return "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
        if system == "Windows":
            roots = [
                os.environ.get("PROGRAMFILES", ""),
                os.environ.get("PROGRAMFILES(X86)", ""),
                os.environ.get("LOCALAPPDATA", ""),
            ]
            candidates = [
                Path(root) / "Google" / "Chrome" / "Application" / "chrome.exe"
                for root in roots
                if root
            ]
            for candidate in candidates:
                if candidate.exists():
                    return str(candidate)
            return str(candidates[0]) if candidates else "chrome.exe"
        return "google-chrome"

    @staticmethod
    def _default_chrome_profile() -> str:
        system = platform.system()
        if system == "Darwin":
            return str(Path("~/Library/Application Support/Google/Chrome").expanduser())
        if system == "Windows":
            local_app_data = os.environ.get("LOCALAPPDATA")
            if local_app_data:
                return str(Path(local_app_data) / "Google" / "Chrome" / "User Data")
            return str(Path("~/AppData/Local/Google/Chrome/User Data").expanduser())
        return str(Path("~/.config/google-chrome").expanduser())

    @staticmethod
    def get_mode_from_request(text: str) -> BrowserMode | None:
        """Recognize intentional browser-mode hints in a natural-language request."""
        request = str(text or "").casefold()
        if any(phrase in request for phrase in ("my tabs", "my browser tabs", "existing tabs", "logged in")):
            return "extension"
        if any(phrase in request for phrase in ("my chrome", "my browser", "system browser", "real chrome")):
            return "system"
        if any(phrase in request for phrase in ("isolated", "sandbox", "clean browser")):
            return "isolated"
        return None
