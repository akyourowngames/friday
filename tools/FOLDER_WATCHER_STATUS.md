# KING Folder Watcher Status

This file is the human-readable status source for the folder watcher service.
The API exposes it through `GET /status` so KING can report capability status
without pretending planned surfaces are already complete.

## Implemented

- Markdown config loading from `tools/FOLDER_WATCHER_CONFIG.md`
- Isolated demo config in `tools/FOLDER_WATCHER_DEMO_CONFIG.md`
- SQLite file index with active and deleted status
- SQLite event log for create, modify, delete, move, unchanged, and directory events
- SHA256 content hashing with configured chunk size
- MIME sniffing from common magic bytes with extension fallback
- Text extraction for configured text extensions
- Python AST metadata extraction for imports, functions, and classes
- JSON and TOML top-level metadata extraction
- Configured auto-tag rules for extension, MIME prefix, directory, and size
- Duplicate detection by SHA256 hash
- SQLite FTS5 search with local content fallback
- REST API for health, latest files, diff, search, query, duplicates, stats, content, tags, config, export, webhooks, and status
- Bulk file detail endpoint at `/files/details` with metadata, hashes, sizes, event counts, relationship counts, and optional content excerpts
- WebSocket event stream at `/watch`
- Webhook registration and background dispatch
- Watchdog-backed recursive live folder watching
- CLI commands for `run`, `scan`, and `stats`
- Browser dashboard at `/dashboard`
- Markdown-owned LLM policy in `tools/FOLDER_WATCHER_LLM_POLICY.md`
- Provider-backed natural-language SQL generation when the configured LLM key is present
- Provider-backed natural chat endpoint and dashboard chat panel
- Simple provider-backed chat that can use indexed file evidence when helpful
- Chat context includes extension and MIME size rollups so size/count questions are answered from the index
- SQLite authorizer guard for read-only LLM-generated SQL
- Provider-backed on-demand summaries and summarize-pending endpoint when summaries are enabled
- PDF text extraction and document metadata
- Word document text extraction
- Audio metadata extraction with transcript sidecar and local Whisper hook support
- Image metadata and EXIF extraction with OCR hook support
- Video metadata extraction through OpenCV and optional ffprobe detail
- File relationship graph and dependency/dependent endpoints
- File deep-dive endpoint using selected file content, metadata, graph, events, and LLM policy
- Change velocity tracking and hot-file tagging in API responses
- Directory intent anomaly detection
- Snapshot reconstruction endpoint
- Symlink suggestion endpoint for duplicate groups
- MUSE-compatible new-arrivals playlist export
- Config file hot reload with live `CONFIG_RELOADED` events
- Per-subscriber and per-webhook rate limiting
- Systemd, launchd, Docker, and Compose deployment templates
- Remote HTTPS deployment guide and nginx reverse proxy template
- Focused tests for config, indexing, API, duplicate detection, and live watchdog events
- Official KING verification pipeline coverage

## Partial

- Natural-language file query falls back to local index search when the configured LLM provider is unavailable
- Natural chat falls back to local indexed context when the configured LLM provider is unavailable
- Summary endpoint reports pending status when summaries are disabled or the configured LLM provider is unavailable
- OCR depends on local Tesseract runtime availability.
- Audio transcription depends on sidecar transcripts or local faster-whisper availability.
- Event ordering uses timestamp precision from Python time, not explicit microsecond sequencing guarantees

## Planned

## How To See It

- Run `python folder_watcher_service.py run --config tools/FOLDER_WATCHER_DEMO_CONFIG.md --port 7475`
- Open `http://127.0.0.1:7475/dashboard`
- Add or edit files inside `storage/folder_watcher_inbox`
- Watch stats, latest files, search, and live events update in the dashboard
