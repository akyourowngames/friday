# Browser Modes — Design Spec

**Date:** 2026-07-05
**Status:** Approved
**Author:** Claude + User

## Summary

Add dual browser mode support to Ares: isolated Playwright Chrome (existing behavior) and system Chrome via CDP attach (uses real logged-in browser). Auto-detect mode tries system first, falls back to isolated. User can switch modes via `/browser` commands or by natural language hints.

## Motivation

The current Playwright MCP setup launches an empty, isolated Chrome profile — no cookies, no logins, no extensions. For many tasks (browsing logged-in sites, using saved passwords, accessing authenticated dashboards), users need their real Chrome. CDP attach solves this by connecting Playwright to an already-running Chrome instance with remote debugging enabled, giving Ares access to the user's actual browser session.

## Architecture

### New file: `ares/browser.py`

Standalone module with `BrowserManager` class. Handles mode detection, MCP argument building, Chrome launch, and natural language mode parsing.

```
ares/browser.py
├── class BrowserManager
│   ├── __init__(config: AppConfig)
│   ├── get_mcp_args(mode: str | None) → list[str]
│   ├── detect_chrome_cdp(port: int | None) → bool
│   ├── launch_system_chrome(port: int | None) → str
│   ├── _chrome_paths() → tuple[str, str]
│   └── get_mode_from_request(text: str) → str | None
```

### Modified file: `ares/models.py`

Add three config fields to `AppConfig`:

```python
browser_mode: str = "auto"           # "isolated" | "system" | "auto"
browser_cdp_port: int = 9222
browser_chrome_path: str = ""        # auto-detected if empty
```

### Modified file: `ares/config.py`

- Import `BrowserManager`
- `_ensure_mcp_defaults()` calls `BrowserManager(config).get_mcp_args()` to build mode-aware Playwright args
- On config load, if `browser_mode` is `"system"`, verify CDP is reachable; if not, fall back to `"isolated"` with a warning

### Modified file: `ares/cli.py`

- Import `BrowserManager`
- Create `self.browser_manager = BrowserManager(self.config)` in `__init__`
- Add `/browser` command group to COMPLETER and command dispatch
- Add `/browser` to help table

## Browser Modes

### Mode: `isolated`

Playwright launches its own Chrome with an empty profile directory. Safe, sandboxed, no access to user's real sessions.

**MCP args:**
```python
[
    "@playwright/mcp@latest",
    "--browser", "chrome",
    "--caps", "vision,devtools",
    "--user-data-dir", "~/.ares/data/playwright-profile",
    "--viewport-size", "1280x720",
]
```

### Mode: `system`

Playwright connects to the user's real Chrome via Chrome DevTools Protocol (CDP) on `localhost:9222`. Chrome must be running with `--remote-debugging-port=9222`.

**MCP args:**
```python
[
    "@playwright/mcp@latest",
    "--cdp-endpoint", "http://localhost:9222",
    "--caps", "vision,devtools",
]
```

### Mode: `auto` (default)

Check if CDP port is open → use system mode args. Otherwise → use isolated mode args. Does NOT auto-launch Chrome — only connects if it's already running with debugging.

**Logic:**
```python
if detect_chrome_cdp(port):
    return system_args
else:
    return isolated_args
```

## BrowserManager Methods

### `get_mcp_args(mode: str | None) → list[str]`

Builds the correct Playwright MCP arguments for the given mode. If mode is None, uses `config.browser_mode`. For `"auto"` mode, calls `detect_chrome_cdp()` to decide.

### `detect_chrome_cdp(port: int | None) → bool`

Connects to `127.0.0.1:{port}` with a 1-second timeout. Returns True if a service is listening (likely Chrome CDP).

```python
def detect_chrome_cdp(self, port: int | None = None) -> bool:
    import socket
    port = port or self.config.browser_cdp_port
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(1)
        return s.connect_ex(("127.0.0.1", port)) == 0
```

### `launch_system_chrome(port: int | None) → str`

Launches Chrome with `--remote-debugging-port` and `--user-data-dir` pointing to the user's real Chrome profile. Returns a status message.

```python
def launch_system_chrome(self, port: int | None = None) -> str:
    import platform, subprocess, os
    port = port or self.config.browser_cdp_port
    exe, profile = self._chrome_paths()
    try:
        subprocess.Popen([exe, f"--remote-debugging-port={port}", f"--user-data-dir={profile}"])
        return f"Launched Chrome with CDP on :{port}. Close ALL other Chrome windows first."
    except FileNotFoundError:
        return f"Chrome not found at {exe}. Set browser_chrome_path in config."
```

### `_chrome_paths() → tuple[str, str]`

Returns `(executable_path, profile_dir)` for the current platform:

| Platform | Executable | Profile |
|----------|-----------|---------|
| macOS | `/Applications/Google Chrome.app/Contents/MacOS/Google Chrome` | `~/Library/Application Support/Google/Chrome` |
| Windows | `C:\Program Files\Google\Chrome\Application\chrome.exe` | `%LOCALAPPDATA%\Google\Chrome\User Data` |
| Linux | `google-chrome` | `~/.config/google-chrome` |

If `config.browser_chrome_path` is set, it overrides the executable path.

### `get_mode_from_request(text: str) → str | None`

Parses natural language for browser mode hints. Used by the agent to auto-switch when the user says things like "open this in my chrome" or "use a clean browser".

| Phrase matches | Returns |
|----------------|---------|
| "my chrome", "my browser", "system browser", "real chrome" | `"system"` |
| "isolated", "sandbox", "clean browser" | `"isolated"` |
| No match | `None` (use current default) |

## CLI Commands

### `/browser` or `/browser status`

Shows current mode, CDP port, whether CDP is reachable, and Chrome path.

```
  Browser Mode: auto
  CDP Port: 9222
  CDP Status: ✓ available
  Chrome: C:\Program Files\Google\Chrome\Application\chrome.exe
```

### `/browser isolated`

Switches to isolated mode. Saves to config. Shows confirmation.

### `/browser system`

Switches to system mode. Shows security warning. Saves to config.

```
  ⚠ System Chrome mode gives Ares access to your real cookies and sessions.
  Only use when you need your logged-in browser.
  Switched to system mode.
```

### `/browser auto`

Switches to auto-detect mode. Saves to config.

### `/browser launch`

Launches system Chrome with CDP debugging on the configured port.

```
  Launched Chrome with CDP on :9222
  Profile: C:\Users\anime\AppData\Local\Google\Chrome\User Data
  ⚠ Close ALL other Chrome windows first — Chrome ignores the debug flag if the profile is already open.
```

### `/browser launch 9223`

Launch on a custom port.

## Integration Flow

### First launch / config load

1. `AresCLI.__init__()` creates `BrowserManager(self.config)`
2. `_ensure_mcp_defaults()` calls `browser_manager.get_mcp_args()` to build Playwright MCP args
3. MCP client starts Playwright with the correct args

### Mode switching via commands

1. User types `/browser system`
2. CLI updates `self.config.browser_mode = "system"`
3. Calls `save_config(self.config)`
4. Prints confirmation with security warning
5. Next MCP connection uses new mode args

### Auto-switching via natural language

1. User says "open github in my chrome"
2. Agent detects "my chrome" → `get_mode_from_request()` returns `"system"`
3. Agent updates `config.browser_mode = "system"` and calls `save_config()`
4. Agent tells user: "Switched to system Chrome. Restart Ares to use the new mode."
5. Note: MCP servers are started at init and cannot be reconnected mid-session. Mode changes take effect on next launch. This is a convenience for setting the mode without remembering the command syntax.

## Data Storage

### config.json additions

```json
{
  "browser_mode": "auto",
  "browser_cdp_port": 9222,
  "browser_chrome_path": ""
}
```

### Existing files unchanged

- `~/.ares/data/playwright-profile` — still used for isolated mode
- No new data files created

## Security

- System Chrome mode is **opt-in** — auto mode only connects if CDP is already open, never auto-launches
- Warning shown when switching to system mode
- No persistent token storage for CDP — connection is ephemeral per MCP session
- `browser_chrome_path` defaults to empty (auto-detected) — prevents config injection
- MCP tools run in the Playwright sandbox even in system mode — they can interact with the browser but not the filesystem beyond what the tool allows

## Error Handling

| Error | Message |
|-------|---------|
| Chrome not found at path | "Chrome not found at {exe}. Install Chrome or set `browser_chrome_path` in config." |
| CDP port occupied, not Chrome | "Port {port} is in use but doesn't look like Chrome CDP. Check what's running on that port." |
| Chrome running without debug flag | "Chrome is running but without debugging. Close all Chrome windows first, then use `/browser launch`." |
| CDP connection lost mid-session | "CDP connection lost. Chrome may have closed. Switching to isolated mode." |
| `subprocess.Popen` fails | "Failed to launch Chrome: {error}. Check your Chrome installation." |

## Testing Strategy

1. **Unit tests** for `BrowserManager.get_mcp_args()` — verify correct args for each mode
2. **Unit tests** for `BrowserManager.detect_chrome_cdp()` — mock socket to test open/closed/timeout
3. **Unit tests** for `BrowserManager.get_mode_from_request()` — verify phrase matching
4. **Unit tests** for `_chrome_paths()` — verify platform detection
5. **Integration test** — verify `_ensure_mcp_defaults()` builds correct args with different modes
6. **Manual test** — `/browser status`, `/browser isolated`, `/browser system`, `/browser launch`

## Dependencies

None new. Uses stdlib only: `socket`, `subprocess`, `platform`, `os`, `pathlib`.

## Scope

- CLI browser mode management only. The agent's ability to auto-switch modes is a natural language convenience — the underlying mechanism is just swapping MCP args.
- Firefox and other browsers are out of scope — this is Chrome-specific for now.
- Browser automation beyond what Playwright MCP provides (e.g., multi-tab management, cookie manipulation) is out of scope.
