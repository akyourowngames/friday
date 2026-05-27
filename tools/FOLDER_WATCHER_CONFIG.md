# KING Folder Watcher Config

This markdown file is the control surface for the local folder watcher service.
The service reads these values at startup and through the config endpoint so the
watched path, database, ignore rules, and tagging behavior stay editable without
adding routing shortcuts in code.

## Paths And Runtime

- watch_path: .
- database_path: storage/folder_watcher.sqlite3
- api_host: 127.0.0.1
- api_port: 7474
- debounce_ms: 300
- scan_on_start: true
- auth_token:
- max_content_chars: 200000
- hash_chunk_bytes: 65536
- large_file_size: 100MB
- ai_summaries_enabled: false
- llm_queries_enabled: true
- llm_policy_file: tools/FOLDER_WATCHER_LLM_POLICY.md
- hot_file_event_threshold: 5
- hot_file_window_seconds: 86400
- anomaly_events_enabled: true
- ocr_enabled: true
- transcription_enabled: true
- subscriber_rate_limit_per_sec: 20
- webhook_rate_limit_per_sec: 5
- playlist_path: storage/folder_watcher_new_arrivals.m3u

## Environment Overrides

- KING_FOLDER_WATCHER_WATCH_PATH overrides `watch_path` at service startup.
- KING_FOLDER_WATCHER_DATABASE_PATH overrides `database_path` at service startup.
- KING_FOLDER_WATCHER_API_HOST overrides `api_host` at service startup.
- KING_FOLDER_WATCHER_API_PORT overrides `api_port` at service startup.
- KING_FOLDER_WATCHER_MAX_CONTENT_CHARS overrides `max_content_chars` at service startup.
- KING_FOLDER_WATCHER_CONFIG_FILE selects the markdown config file used by KING settings.
- KING_FOLDER_WATCHER_TARGET, KING_FOLDER_WATCHER_BASE_URL, and KING_FOLDER_WATCHER_TIMEOUT_MS control the registered KING `folder_watcher` client bridge.

## Ignore Globs

- AGENTS.md
- .claude/**
- .opencode/**
- .git/**
- .next/**
- node_modules/**
- __pycache__/**
- .pytest_cache/**
- todo.md
- test_output.txt
- tsconfig.tsbuildinfo
- storage/tool_embeddings.npy
- storage/tool_texts.json
- storage/small_talk_emb.npy
- storage/small_talk_text.txt
- storage/folder_watcher.sqlite3
- storage/folder_watcher.sqlite3-*
- *.pyc
- *.pyo
- *.tmp
- *.lock
- .DS_Store

## Text Extensions

- .py
- .md
- .txt
- .json
- .toml
- .yaml
- .yml
- .css
- .html
- .js
- .ts
- .tsx
- .ps1

## Tag Rules

- extension:.py -> python
- extension:.md -> markdown
- extension:.json -> json
- extension:.toml -> toml
- extension:.ts -> typescript
- extension:.tsx -> typescript
- mime-prefix:text/ -> text
- mime-prefix:audio/ -> audio
- mime-prefix:image/ -> image
- mime-prefix:video/ -> video
- directory:models -> ml-model
- directory:prompts -> prompt
- directory:audio -> audio
- size-over:100MB -> large-file

## Directory Intent Rules

- models: .bin,.safetensors,.onnx,.pt,.pth
- prompts: .txt,.md,.json,.yaml,.yml
- audio: .mp3,.flac,.wav,.m4a,.ogg
- documents: .pdf,.docx,.txt,.md

## API Contract

- `GET /health` returns service status.
- `GET /dashboard` renders the live browser dashboard.
- `GET /status` returns the markdown-backed feature status matrix.
- `GET /files/latest` returns recently indexed active files.
- `GET /files/diff` returns event-log changes since a timestamp.
- `GET /files/snapshot` reconstructs indexed file state from the event log.
- `GET /files/hot` returns frequently changed files in the configured window.
- `GET /files/anomalies` returns directory-intent anomaly events.
- `GET /files/search` runs content search using SQLite FTS5 when available.
- `POST /files/query` uses the LLM policy to generate read-only SQLite when the
  provider is available, then falls back to local index search when it is not.
- `POST /chat` replies naturally from markdown-owned LLM chat policy and
  indexed folder evidence, with optional selected-file context.
- `GET /llm/status` reports provider readiness, model, prompt policy, and LLM
  feature switches.
- `GET /files/duplicates` groups active files by identical SHA256 hash.
- `GET /files/duplicates/symlink-suggestions` returns duplicate link plans
  without modifying the filesystem.
- `GET /files/stats` returns index coverage and file breakdowns.
- `GET /files/details` returns bounded batches of file metadata, optional
  content excerpts, event counts, dependency counts, and selected filters.
- `GET /files/{id}` returns one indexed file record.
- `GET /files/{id}/content` returns extracted text content.
- `GET /files/{id}/content?offset=0&max_chars=5000` returns bounded content windows with `next_offset`.
- `GET /files/{id}/dependencies` returns indexed relationship edges from a file.
- `GET /files/{id}/dependents` returns indexed reverse relationship edges.
- `GET /files/{id}/deep-dive` returns provider-backed or local deep-dive context
  for one indexed file, including structured file understanding metadata.
- `GET /files/{id}/summary` returns a stored summary or generates one through
  the LLM policy when summaries are enabled and the provider is available.
- `POST /files/summarize-pending` summarizes pending text files through the LLM
  policy when summaries are enabled.
- `DELETE /files/{id}` removes the index record only, not the source file.
- `POST /files/{id}/tags` adds a user tag to the indexed record.
- `GET /config` returns the currently loaded watcher config.
- `PATCH /config` updates safe runtime config fields.
- `GET /export` exports the index as JSON or CSV.
- `GET /playlist/new-arrivals` returns audio arrivals as JSON or M3U.
- `POST /webhooks` registers a local HTTP subscriber for matching events.
- `WS /watch` streams watcher events to connected subscribers.
- KING's registered `folder_watcher` tool and main API `POST /folder-watcher`
  bridge call these HTTP endpoints and do not bypass this service or read its
  SQLite index directly.

## Verification Notes

- Focused tests live in `tests/test_folder_watcher.py`.
- KING tool bridge tests live in `tests/test_folder_watcher_tool.py`.
- The repository verification pipeline must include the focused watcher tests.
- Use `tools/FOLDER_WATCHER_DEMO_CONFIG.md` when you want an isolated visible
  inbox instead of indexing the repository root.
- The daemon uses the optional `watchdog` package for OS-backed file events.
- If `watchdog` is missing, CLI run mode reports the missing dependency instead
  of pretending live watching is active.
- PDF, Word, image, audio, and video extraction use optional local libraries and
  record extractor status in metadata instead of hiding missing capability.
- OCR and transcription hooks run only when the local dependencies and runtime
  assets are available; otherwise metadata reports an unavailable or empty hook.
- Deployment templates live under `deploy/folder_watcher/`.
- Codex/IDE agent instruction files, local todo scratchpads, build outputs, and
  selector cache artifacts stay out of this watcher index. They are not KING
  runtime tool-control evidence.
