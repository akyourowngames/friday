# Ares Browser Control — Design Spec

**Date:** 2026-06-27
**Status:** Draft

## Overview

Give Ares full computer-use-grade browser control: navigate, click, type, read rendered pages, fill forms, take screenshots, and loop until a goal is achieved. No external API needed — just a Playwright-controlled browser window driven through Ares's existing MCP infrastructure.

## Architecture

```
┌─────────────────────────────────────────────────┐
│  Ares (LLM + Tool Loop)                         │
│                                                  │
│  agent_max_iterations: 20 cap on loop            │
│                                                  │
│  Calls:  mcp__playwright__browser_snapshot       │
│          mcp__playwright__browser_click          │
│          mcp__playwright__browser_type           │
│          mcp__playwright__browser_navigate       │
│          mcp__playwright__browser_take_screenshot │
│          ... 40+ tools                           │
└──────────────┬──────────────────────────────────┘
               │ stdio transport (MCP protocol)
┌──────────────▼──────────────────────────────────┐
│  Playwright MCP Server (npx @playwright/mcp)     │
│                                                   │
│  ┌────────────┐  ┌──────────┐  ┌──────────────┐  │
│  │ Chromium   │  │ Snapshot │  │ Screenshot   │  │
│  │ (headed)   │  │ (a11y    │  │ (viewport/   │  │
│  │            │  │  tree)   │  │  full-page)  │  │
│  └────────────┘  └──────────┘  └──────────────┘  │
│                                                   │
│  ┌────────────┐  ┌──────────┐  ┌──────────────┐  │
│  │ DevTools   │  │ Video    │  │ Network      │  │
│  │ (tracing)  │  │ (record) │  │ (intercept)  │  │
│  └────────────┘  └──────────┘  └──────────────┘  │
└──────────────────────────────────────────────────┘
```

### Key design properties

- **Zero bridge code.** The Playwright MCP server is a ready-made stdio server from Microsoft. Ares's existing `MCPClientManager` connects, discovers tools, and routes calls — exactly like the Google MCP bridge.
- **Headed mode by default.** A visible Chrome window opens so the user can watch what Ares does.
- **Persistent profile.** Cookies and sessions stored at `~/.ares/data/playwright-profile/` survive restarts. This is a dedicated profile — separate from the user's real Chrome — so no session leakage.
- **Dual introspection.** `browser_snapshot` reads the structured accessibility tree (reliable, fast). `browser_take_screenshot` captures pixels (for visual reasoning). The model can use both.

## Configuration

Single addition to `~/.ares/config.json` `mcp_servers` array:

```json
{
  "name": "playwright",
  "transport": "stdio",
  "command": "npx",
  "args": [
    "@playwright/mcp@latest",
    "--browser", "chrome",
    "--caps", "vision,devtools",
    "--user-data-dir", "~/.ares/data/playwright-profile",
    "--viewport-size", "1280x720"
  ]
}
```

### Flag rationale

| Flag | Value | Why |
|------|-------|-----|
| `--browser` | `chrome` | Uses installed Chrome — best rendering, devtools, CDP support |
| `--caps` | `vision,devtools` | `vision` adds coordinate-based mouse tools (click_xy, drag_xy, move_xy, scroll) alongside accessibility-tree interaction. `devtools` adds tracing and video recording for debugging |
| `--user-data-dir` | `~/.ares/data/playwright-profile` | Persists cookies, localStorage, sessions to a dedicated directory so logins survive restarts |
| `--viewport-size` | `1280x720` | Standard desktop resolution; matches typical web app designs |

Headed mode is the default — no `--headless` flag needed. Profile persistence is the default — no `--isolated` flag needed.

## Available Tools (excerpt)

40+ tools, categorized:

**Navigation:** `browser_navigate`, `browser_navigate_back`, `browser_wait_for`

**Interaction (DOM-aware):** `browser_click`, `browser_hover`, `browser_type`, `browser_fill_form`, `browser_select_option`, `browser_drag`, `browser_drop`, `browser_file_upload`, `browser_press_key`

**Introspection:** `browser_snapshot` (accessibility tree), `browser_take_screenshot` (PNG/JPEG), `browser_console_messages`, `browser_network_requests`

**Coordinate-based (vision cap):** `browser_mouse_click_xy`, `browser_mouse_drag_xy`, `browser_mouse_move_xy`, `browser_mouse_down`, `browser_mouse_up`, `browser_mouse_wheel`

**Tab management:** `browser_tabs` (new, close, select)

**Storage:** `browser_cookie_*`, `browser_localstorage_*`, `browser_sessionstorage_*`

**DevTools:** `browser_start_tracing`, `browser_stop_tracing`, `browser_start_video`, `browser_stop_video`, `browser_highlight`, `browser_annotate`

**Page scripting:** `browser_evaluate` (execute JS on page or element)

**Other:** `browser_resize`, `browser_close`, `browser_handle_dialog`, `browser_pdf_save`, `browser_run_code_unsafe` (RCE-equivalent — use with care)

## The Think Loop

Ares's native tool-calling loop IS the browser control loop. No separate agent framework needed.

Typical flow for "go to Google AI Studio and ask Gemini a question":

1. Ares calls `browser_navigate(url="https://aistudio.google.com/")`
2. Ares calls `browser_snapshot()` → sees the accessibility tree, finds the login/chatform
3. Ares calls `browser_type(selector="...", value="Prompt text")`
4. Ares calls `browser_click(selector="...")` or `browser_press_key(key="Enter")`
5. Ares calls `browser_snapshot()` or `browser_take_screenshot()` → reads the response
6. Loops until the answer is confirmed or `agent_max_iterations` (default 20) caps it

For visual tasks ("is this button visible?", "what color is the header?"), Ares uses `browser_take_screenshot` and reasons about the image — the model is multimodal.

## Error Handling

- **Timeout:** `--timeout-navigation` (default 60s) and `--timeout-action` (default 5s) control how long Playwright waits. Adjustable via config args.
- **Server crash:** MCPClientManager logs the failure, marks the server disconnected, and Ares continues without browser tools.
- **Bad selectors:** Playwright returns clear error messages about missing elements, which Ares reads and can retry with `browser_snapshot` to find the correct selector.
- **Dialog popups:** `browser_handle_dialog` lets Ares accept/dismiss alert/confirm/prompt dialogs that would otherwise block interaction.

## Security Considerations

- The Playwright MCP runs in its own process, sandboxed by the OS. It has filesystem access to the profile directory and can navigate anywhere.
- `browser_run_code_unsafe` is RCE-equivalent in the browser process — Ares should be instructed not to use it without explicit user consent.
- The persistent profile stores cookies. If the user has sessions in there, Ares can act as the logged-in user on those sites. This is the intended behavior but should be understood.
- `--allowed-hosts` and `--blocked-origins` flags are available if domain restrictions are wanted later.

## Out of Scope

- **Desktop control beyond the browser.** This is browser-only. For desktop-native apps, a computer-use Docker setup would be a separate project.
- **Headless server mode.** Can be added later by adding `--headless` flag.
- **Self-hosted Playwright grid.** Not needed for a personal assistant.
