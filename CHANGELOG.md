# Changelog

This initial changelog is summarized from the repository git history and grouped by feature area rather than raw commit messages.

## 2026-07-13

### Next.js Power Workspace
- Added a separate Ares-styled Next.js application for power users with streaming chat, thread controls, reusable file uploads, skills management, MCP server administration, watcher operations, command palette, and responsive navigation.
- Added a production workspace host to the unified `python -m ares --all` runtime on port 8766 while keeping the public marketing website independent and the advanced watcher console available on port 8080.
- Connected watcher creation, browser/Instagram DM presets, fleet queries, incident actions, MCP lifecycle operations, uploads, profile/soul personalization, Telegram, browser, model, and monitoring settings to the existing Ares agent and shared configuration.
- Added source freshness, execution telemetry, reliability, fleet health, and incident views with explicit metric definitions and secret-safe readiness states.
- Added a static-export synchronization pipeline, packaged frontend fallback, WebSocket protocol coverage, upload safety checks, settings round-trip tests, and rendered browser QA.

### Proactive Watchers
- Refactored watchers into the normal Ares agent tool plane with 12 first-class tools for capability discovery, lifecycle management, immediate checks, fleet/event queries, acknowledgement, and confirmed deletion.
- Added authenticated Playwright browser watchers and bounded workflows over existing local/MCP tools, including an Instagram DM starter recipe, phone-notification recipe, output extraction, step chaining, and dual opt-in guards for consequential background actions.
- Added `python -m ares --all` as the unified desktop API, agent, MCP, Telegram, watcher scheduler, and watcher dashboard runtime; `--server` remains a compatibility alias and the legacy watcher flag now routes to the unified process.
- Made watcher/dashboard dependencies part of the normal Ares install and exposed Browser/DM and Ares-tool sources directly in the dashboard monitor editor.
- Added the production watcher core: WAL-backed SQLite history, cross-process leases, website/JSON/Instagram Graph fetchers, hash/diff/threshold detection, concurrent scheduling, retry backoff, failure auto-pause, and retained check telemetry.
- Added Telegram, desktop, email, and webhook notifications with delivery audit logs and background retries, optional AI change analysis, and explicit webhook-only automatic actions.
- Added a FastAPI/WebSocket control plane plus a responsive Ares-styled dashboard for fleet health, incidents, telemetry, monitor configuration, and persistent delivery settings.
- Added terminal and Telegram watcher controls and automatic scheduler startup in the terminal, desktop server, and standalone Telegram runtimes.
- Added SSRF and redirect protections, control-plane token authentication, and secret-redacted API/WebSocket payloads.

## 2026-07-07

### Phone Bridge
- Added configuration for custom KDE Connect and ADB executable paths used by Android phone bridge tools.

## 2026-07-05

### Phone Bridge
- Added Android phone bridge tooling for bridge health, notifications, contact lookup, SMS, and confirmed call placement.
- Tightened phone bridge behavior by enforcing the enabled flag, fixing URI handling, and reducing duplicate subprocess calls.

### MCP and Browser Tooling
- Added default Playwright MCP configuration.
- Corrected the fetch MCP server command.
- Added browser mode design and implementation planning docs.

### CLI and Cross-platform Fixes
- Fixed CLI display issues and Windows shell compatibility.

### Documentation
- Added website and phone bridge planning docs.

## 2026-07-03

### Onboarding and Session Architecture
- Added terminal onboarding wizard.
- Added session architecture changes, cron completion toasts, datetime tool support, and onboarding documentation.

### Google/MCP
- Removed stale task references from the README and persisted Google tokens.

## 2026-06-29

### Documentation
- Expanded README architecture and per-component documentation.

### Cron Jobs
- Added cron job scheduling, including job storage, scheduling, execution, and related docs.

### Task System Removal
- Deleted the old tasks directory and removed the prior task system/background executor.

## 2026-06-28

### Voice Mode
- Added streaming agent output in voice mode.
- Replaced the previous LiveKit approach with a standalone continuous voice mode.

### Agent Streaming
- Fixed accumulated text handling in streaming responses.

## 2026-06-27 and Earlier

### Voice, Tools, Skills, and Core Assistant Work
- Added design/planning docs for voice, browser control, MCP client, persistent REPL, context management, web search, file tools, skills, retry/templates, proactive memory, and CLI UX.
- Implemented the core Ares terminal assistant, memory, context, skills, tools, and support modules reflected in the current README.
