# Changelog

This changelog is summarized from repository history and grouped by user-facing feature area rather than raw commit messages. Dates use the repository's local commit date.

## 2026-07-30

### Multi-Bot Telegram

- Added support for running multiple Telegram bots in a single group.
- Each bot has its own Ares agent instance and conversation history.
- Implemented @mention-based routing so bots only respond when tagged.
- Added `TelegramBotConfig` and `TelegramMultiConfig` models for multi-bot configuration.
- Created `MultiTelegramChannel` class to manage multiple bot instances.
- Added `--telegram-multi` CLI command to run multi-bot mode.
- Added `--telegram-multi-setup` for interactive multi-bot configuration.
- Integrated multi-bot support into unified runtime (`--all`).
- Added optional model override and system prompt suffix per bot.

### Healthcare Reporting Pipeline

- Added complete healthcare reporting pipeline with source retrieval, summarization, trend extraction, and BI chart generation.
- Implemented `healthcare-reporting`, `healthcare-retrieval`, and `healthcare-summarization` skills for end-to-end report generation.
- Added report validation, markdown assembly, and optional PDF/DOCX conversion.
- Created healthcare report directory structure under `~/.ares/data/healthcare-reports/`.

### Semantic Computer Control

- Added intelligent desktop automation with UI element recognition and context-aware actions.
- Implemented multi-step message sending workflows for various applications.
- Enhanced Windows MCP integration for reliable UI interaction and state management.
- Added error recovery and workflow monitoring for complex desktop tasks.

### Model Selection Improvements

- Streamlined provider/model switching with automatic endpoint alignment.
- Made interactive MCP control permissive for easier integration management.
- Improved Windows MCP execution reliability and recovery.

## 2026-07-23

### MCP Reliability

- Reworked MCP transport lifetime management so each AnyIO transport is opened, owned, and closed by the same long-lived asyncio task.
- Added serialized connection maintenance, periodic health probes, bounded reconnect backoff, and a reconnect monitor that survives an individual maintenance failure.
- Added immediate recovery after transport loss. Read-only calls may be retried once after a successful reconnect; mutation calls are never replayed when their remote outcome may be uncertain.
- Preserved cached MCP schemas during transport recovery and refreshed the agent tool catalog when a server reconnects.
- Gave voice and cron agents the same configured MCP manager and pre-turn recovery behavior used by the terminal, workspace, server, and Telegram surfaces.

### Documentation

- Rebuilt the README around the current product: installation extras, providers, desktop voice, runtime surfaces, architecture, capabilities, configuration, environment variables, troubleshooting, local storage, security boundaries, and development checks.
- Added detailed “Hey Jarvis,” Hindi/Hinglish translation, microphone routing, Bluetooth, interruption, SQLite, and Tk-process troubleshooting.
- Removed stale LiveKit, Sarvam, and `ares.dev` instructions that no longer match the implementation.

## 2026-07-22

### Desktop Voice Reliability

- Replaced transcription-based wake detection with a local openWakeWord “Hey Jarvis” model while retaining faster-whisper for post-wake commands.
- Added a background Whisper warm-up and a model lock so early commands wait safely instead of racing duplicate model loads.
- Added adaptive voice activity detection, wake-command pre-roll, bounded command windows, empty-transcript rearming, and protection against dropping captured audio while transcription is still running.
- Added Windows-default microphone discovery, friendly device names, explicit device pinning, and non-Bluetooth fallback for systems whose default endpoint is a narrow-band hands-free microphone.
- Added multilingual Whisper translation so Hindi and Hinglish speech can become English command text before agent execution.
- Added a crash-safe single-instance Windows mutex for desktop mode.

### Desktop UI and Process Isolation

- Moved the Tk/CustomTkinter status window into a dedicated child process so its main loop cannot block audio capture, tray actions, terminal work, or other desktop windows.
- Expanded the floating panel with transcript and response streaming, microphone status, tool start/progress/completion rows, structured result previews, and clickable local file paths.
- Improved tray state, global hotkey handling, clean shutdown, history refresh, and session reset behavior.
- Sanitized display and speech output so emoji, Markdown, URLs, paths, and decorative punctuation are not spoken literally.

### Interruption and Barge-In

- Enabled voice interruption in persisted configuration and added an **Enable/Disable Interruption** tray control.
- Lowered the hard-coded interruption energy floor in favor of an ambient-aware threshold suitable for quieter Realtek microphones.
- Preserved the opening audio of an interruption so short phrases such as “stop talking” are not consumed by the detector and lost before transcription.
- Prevented duplicate interruption tasks and allowed “Hey Jarvis” to interrupt Ares while TTS is playing.

### SQLite Resilience

- Reused shared SQLite connections across memory, conversations, goals, and reflection initialization to remove startup lock races.
- Added busy timeouts, WAL-aware retry helpers, migration retries, transaction cleanup, and rollback behavior for failed constructors.
- Ensured desktop shutdown closes owned stores and connections so a stopped process does not retain a database lock.

## 2026-07-21

### Desktop Voice Assistant

- Added `python -m ares --desktop` with a Windows system tray, floating always-on-top status panel, push-to-talk, recent history, mute, window-toggle hotkeys, and shared Ares agent access.
- Added `Ctrl+Space` push-to-talk, `Ctrl+Shift+M` TTS mute, and `Ctrl+Shift+H` panel visibility controls.
- Added faster-whisper speech recognition for English, Hindi, and Hinglish plus Edge TTS response playback.
- Added the initial desktop history, tray, hotkey, window, configuration, and regression-test modules.

### Voice and Telephony Simplification

- Removed the incomplete Sarvam STT/TTS and LiveKit worker/room/token implementation.
- Standardized local voice and desktop speech on faster-whisper for STT and Edge TTS for output.
- Kept Twilio webhook and Media Streams telephony while removing obsolete LiveKit configuration, CLI flags, workspace client code, and tests.

### Provider Routing and Package Organization

- Added provider-aware routing so OpenCode Zen, NVIDIA NIM, and GitHub Copilot models use their matching endpoints and credentials.
- Added reasoning-content extraction and an empty-content fallback for models that do not populate the normal `content` field.
- Extended configurable fast-model routing to substantive and tool-using turns without changing the selected primary model.
- Added standing-memory surfacing so important local memories remain available during ordinary conversation.
- Reorganized context, infrastructure, integrations, memory, skills, and multi-agent modules into explicit packages.

## 2026-07-20

### Parallel Specialists

- Replaced one-lock-per-resource execution with capacity-aware semaphores so independent specialists can make concurrent external, communication, and delegation calls.
- Kept SQLite writes, persistent REPL operations, and overlapping stateful work serialized.
- Scoped unresponsive-tool quarantine by resource so one hung integration no longer freezes unrelated specialist work.
- Added stronger run manifests, worker status, partial-result handling, and Telegram-visible specialist progress.

### Telegram, MCP, and Storage

- Added Telegram `/model` and `/provider` controls with persistence to shared configuration.
- Added MCP removal from both Telegram and the CLI, including safe confirmation and tool-catalog reload behavior.
- Reused the agent memory connection for conversation storage, eliminating a separate-connection startup race against `ares.db`.

## 2026-07-18

### Memory V3 and Reflection

- Added provenance-preserving memory observations, durable fact promotion, outcome-aware reflection runs, reviewed procedural learning, and compaction checkpoints.
- Added hybrid vector/keyword/metadata retrieval, query rewriting, time decay, MMR diversity, bounded injection, and inspectable retrieval diagnostics.
- Added background embedding warm-up, foreground model-call controls, interruption-aware reflection scheduling, and recovery of abandoned reflection runs.
- Added commitments, user context, proactive follow-ups, quiet hours, cooldowns, delivery state, and explicit approval for reusable learned procedures.

### Providers, Copilot, and Vision

- Added provider/model catalogs and switching for OpenCode Zen, NVIDIA NIM, and GitHub Copilot, including Copilot OAuth and token diagnostics.
- Added Local Vision V1 for user-supplied images and explicitly granted camera/screen sources, with OCR, object detection, comparison, verification, bounded watches, and conservative retention.
- Added vision controls to the CLI and workspace plus privacy lifecycle, notifications, and evidence metadata.

### Agent and Repository Structure

- Improved context blending, reflection, goals, proactive services, exports, skills, and tool execution.
- Split large top-level modules into maintainable `context`, `infra`, `integrations`, `memory`, `skills`, and `multi_agent` packages.
- Added broader regression coverage and worktree-aware latency benchmarks.

## 2026-07-17

### Multi-Provider Models

- Added model catalogs spanning free OpenCode Zen models, NVIDIA NIM, GPT, Claude, Gemini, and other compatible families.
- Added provider aliases, provider-specific base URLs, per-provider credential retention, and automatic provider selection when the active model changes.
- Added reasoning-model response extraction and compatibility for OpenAI-style APIs.

### Trusted Local Execution

- Removed former authorization restrictions from trusted local execution paths and expanded local tool access.
- Updated agent delegation and execution behavior to operate under the repository's trusted-local model.

## 2026-07-16

### Existing Tool Upgrades

- Upgraded memory, people, files, runtime, shell, web, research, cron, watcher, phone, delegation, skills, MCP, media, and export tools while retaining legacy call compatibility.
- Added structured results, previews, plans, verification metadata, provenance, safe batch operations, backups, undo information, and protected export behavior.
- Added project checks and stronger tool-call recovery, including recovery of text-form tool calls emitted outside normal structured tool-call fields.

### Security and Data Portability

- Added encrypted export primitives, safer archive handling, secret redaction, and export/import integrity metadata.
- Hardened shell, file, media, MCP, and watcher execution boundaries and cleaned generated or accidental repository content.

### Latency and Streaming

- Added response streaming and latency telemetry across context preparation, model time-to-first-token, tool selection, and completion.
- Moved reflection and memory statistics off the foreground response path where safe, cached reusable context, and kept normal follow-up turns streaming.
- Added latency benchmarks for normal and worktree-based development.

### Local Vision

- Added the local-first vision service, camera/screen source lifecycle, evidence events, OCR, object detection, and visual verification.
- Added a reliable Windows OCR fallback and direct routing of screen-reading requests to vision tools.

## 2026-07-15

### Native Multi-Agent Hardening

- Added deterministic delegation routing, isolated child histories, role-specific tool registries, run/session IDs, budgets, timeouts, retries, dependency waves, persistence, cancellation, and result synthesis.
- Added resource coordination, detached builder worktrees, project-check verification, mutation review, action grants, and adversarial regression coverage.
- Added terminal, workspace, server, and Telegram status/control surfaces plus deterministic and provider-backed smoke tests.

### Proactive Memory, Goals, and Workspace

- Completed proactive memory extraction, commitments, reflections, reminders, follow-ups, and reviewed self-improvement workflows.
- Expanded goals with hierarchy, evidence, check-ins, watcher links, derived progress, due/overdue views, and proactive signals.
- Upgraded workspace chat, background sessions, voice-related UI, artifacts, profile/personality settings, and operational status.

### Project Documentation

- Added the OpenAI Build Week submission dossier and expanded architecture, hardening, and tool-upgrade documentation.

## 2026-07-14

### Native Multi-Agent Runtime

- Added the native specialist runtime, durable run store, role policies, task dependencies, execution waves, model/budget controls, and root-owned synthesis.
- Added CLI, workspace, WebSocket, and tool controls for launching, listing, inspecting, cancelling, and resuming agent runs.
- Added the multi-agent operating guide, architecture documentation, offline smoke tests, and runtime regression suite.

### MCP and Windows Reliability

- Added live MCP readiness context on every turn so the model uses current integration state instead of stale conversation claims.
- Sanitized all Windows MCP responses, including malformed UTF-16 surrogate content, before writing JSON to stdio.

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
