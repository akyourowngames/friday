---
name: browser-use
description: Use Playwright MCP for browser and web-page automation. Use when the user asks to "open a website", "use a web app", "log in to a website", fill a browser form, inspect a page, or operate Google, YouTube, GitHub, a dashboard, or another web service.
category: automation
version: 1.0.0
examples:
  - prompt: Open YouTube and search for a video.
  - prompt: Log in to the website and fill out the form.
test_commands:
  - python -m pytest tests/test_prompts.py tests/test_skills.py tests/test_mcp_client.py -q
---

# Browser Use

## Scope

Use Playwright MCP for browser and web-page automation: websites, web apps,
browser navigation, search pages, logins, forms, buttons, dashboards, page
inspection, and reading/scraping rendered content. This is the default browser
route even when the user refers to Chrome casually.

Do not use Windows MCP Snapshot/Click/Type for normal web pages. Windows MCP is
for native Windows apps, OS dialogs, desktop UI, and a user explicitly asking to
operate the visible desktop/browser window.

## Playwright-First Loop

1. Confirm that `mcp__playwright__browser_*` tools are connected. If they are,
   use them before any Windows MCP tool.
2. Navigate with `mcp__playwright__browser_navigate` when a URL is known.
3. Observe with `mcp__playwright__browser_snapshot` before interacting and after
   every meaningful page state change. Prefer DOM/accessibility targets over
   coordinates.
4. Interact with `mcp__playwright__browser_click`,
   `mcp__playwright__browser_type`, `mcp__playwright__browser_fill_form`,
   `mcp__playwright__browser_select_option`, and
   `mcp__playwright__browser_press_key` as appropriate.
5. Use `mcp__playwright__browser_wait_for` for a known page condition. Do not
   poll blindly.
6. Use `mcp__playwright__browser_take_screenshot` only when visual reasoning is
   required, such as identifying color, layout, a canvas, an image-only control,
   or a visual error that is absent from the snapshot.
7. Verify the final page state with a fresh Playwright snapshot or read-back.

## Fallback

Fall back to Windows MCP only when Playwright is disconnected, cannot reach the
target, or the user explicitly asks to operate the actual visible desktop/Chrome
window. State the fallback reason, then use the native computer-use observe-act-
verify loop. Never choose Windows MCP merely because it has Snapshot/Click tools.

## Safety

Do not enter passwords, one-time codes, or private account credentials. Do not
submit forms, send messages, purchase, delete data, or change account settings
without clear user intent and a final verification step.
