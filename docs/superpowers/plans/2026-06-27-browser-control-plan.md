# Browser Control Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add Playwright MCP server to Ares config so Ares has 40+ browser control tools (navigate, click, type, screenshot, snapshot, etc.) via existing MCP infrastructure.

**Architecture:** Microsoft's `@playwright/mcp` stdio server runs as a child process, same as the Google MCP bridge. Ares's `MCPClientManager` connects at startup, discovers all tools, and merges them into Ares's tool list as `mcp__playwright__browser_*`. The existing LLM tool-calling loop IS the browser control loop — no new agent framework needed.

**Tech Stack:** Node.js (npx), `@playwright/mcp` v0.0.76, Chromium (already installed), stdio MCP transport.

---

### Task 1: Add Playwright MCP config entry

**Files:**
- Modify: `~/.ares/config.json` (mcp_servers array)

**Changes in context:**

The `mcp_servers` array in `~/.ares/config.json` currently has one entry (google). We add a second entry for playwright:

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

- [ ] **Step 1: Verify prerequisites**

Run each of these to confirm the stack is ready:

```bash
node --version
```
Expected: `v24.16.0` (or similar — just confirm node is installed)

```bash
npx @playwright/mcp@latest --help
```
Expected: help text with `--browser`, `--caps`, `--headless` etc. flags. (This also installs the package if not cached.)

```bash
# Verify Chromium is installed (Windows path)
ls "C:\Users\anime\AppData\Local\ms-playwright" 2>/dev/null || echo "checking alternate path"
```
Expected: a `chromium-*` directory exists.

- [ ] **Step 2: Edit `~/.ares/config.json`**

Read the current config, add the playwright entry to the `mcp_servers` array, and write back.

Expected result: `mcp_servers` array has two entries — `"google"` and `"playwright"`.

- [ ] **Step 3: Start Ares and verify tools are discovered**

Run Ares:

```bash
ares
```

Expected: Ares starts without errors. The startup log should show no "Failed to connect MCP server 'playwright'" warning. A Chrome window opens (headed mode).

Test by asking Ares: "Go to google.com and tell me what the page title says."

Expected: Ares navigates to google.com, sees the page, and reports the title.

- [ ] **Step 4: Commit**

```bash
git add -A
git commit -m "feat: add Playwright MCP browser control server"
```

---

## Self-Review

**Spec coverage check:**
- Config change covers the entire spec: Playwright MCP as stdio server, headed mode, persistent profile, chromium browser, vision+devtools caps, 1280x720 viewport. All 40+ tools are auto-discovered by MCPClientManager — zero code changes needed.
- The think loop is Ares's existing `agent_max_iterations` — no implementation needed, it already works.

**Placeholder scan:** Clean. No TBDs, no TODOs, no "implement later".

**Type consistency:** N/A — no new code types introduced.
