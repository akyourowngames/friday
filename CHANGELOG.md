# Changelog

This initial changelog is summarized from the repository git history and grouped by feature area rather than raw commit messages.

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
