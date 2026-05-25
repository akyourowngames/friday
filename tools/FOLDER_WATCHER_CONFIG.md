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

## API Contract

- `GET /health` returns service status.
- `GET /files/latest` returns recently indexed active files.
- `GET /files/diff` returns event-log changes since a timestamp.
- `GET /files/search` runs content search using SQLite FTS5 when available.
- `POST /files/query` resolves a local natural-language file query through the
  current index without claiming provider-backed SQL generation.
- `GET /files/duplicates` groups active files by identical SHA256 hash.
- `GET /files/stats` returns index coverage and file breakdowns.
- `GET /files/{id}` returns one indexed file record.
- `GET /files/{id}/content` returns extracted text content.
- `GET /files/{id}/summary` returns a stored summary or a clear pending status.
- `DELETE /files/{id}` removes the index record only, not the source file.
- `POST /files/{id}/tags` adds a user tag to the indexed record.
- `GET /config` returns the currently loaded watcher config.
- `PATCH /config` updates safe runtime config fields.
- `GET /export` exports the index as JSON or CSV.
- `POST /webhooks` registers a local HTTP subscriber for matching events.
- `WS /watch` streams watcher events to connected subscribers.

## Verification Notes

- Focused tests live in `tests/test_folder_watcher.py`.
- The repository verification pipeline must include the focused watcher tests.
- The daemon uses the optional `watchdog` package for OS-backed file events.
- If `watchdog` is missing, CLI run mode reports the missing dependency instead
  of pretending live watching is active.
- Codex/IDE agent instruction files, local todo scratchpads, build outputs, and
  selector cache artifacts stay out of this watcher index. They are not KING
  runtime tool-control evidence.
