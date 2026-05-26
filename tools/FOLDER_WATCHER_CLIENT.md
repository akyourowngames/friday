# KING Folder Watcher Client

This file controls how the registered `folder_watcher` KING tool reaches the
folder watcher HTTP service. Keep service targets and enabled read-only actions
here instead of adding routing shortcuts in code.

## Runtime

- active_target: demo
- default_timeout_ms: 12000
- max_limit: 500

## Targets

- demo: http://127.0.0.1:7475 | auth_env: KING_FOLDER_WATCHER_AUTH_TOKEN
- local: http://127.0.0.1:7474 | auth_env: KING_FOLDER_WATCHER_AUTH_TOKEN

## Enabled Actions

- ask
- query
- stats
- details
- search
- latest
- content
- deep_dive
- status

## Action Semantics

- ask: Natural conversation with the folder watcher when the user wants a grounded answer from indexed folder evidence, especially broad questions, mixed requests, or requests that need the watcher's LLM policy.
- query: Natural-language file query over indexed folder evidence when the user wants matching files, filtered evidence, or a watcher-backed search result that may include local fallback or provider status.
- stats: Folder inventory statistics when the user wants counts, total size, file type distribution, extension totals, media/image/Python counts, largest files, or other aggregate facts.
- details: Bounded file metadata when the user wants file rows, paths, sizes, hashes, tags, timestamps, event counts, relationship counts, or optional content excerpts.
- search: Text search across indexed files when the user wants matching filenames, paths, content snippets, or specific terms found in the watched folder.
- latest: Recent indexed files when the user wants newest watcher events, latest files, recent audio/media/documents, or files changed most recently.
- content: Raw readable content for one previously identified watcher file when the user asks to read, open, show, quote, or inspect that specific file.
- deep_dive: Detailed analysis for one previously identified watcher file when the user asks to deeply inspect, explain, summarize, or investigate that specific file.
- status: Watcher runtime health when the user asks whether the watcher service, target, index, providers, OCR, transcription, or background runtime is available.

## Contract

- The client is read-only in v1.
- It calls the folder watcher HTTP API and does not read the SQLite database directly.
- Tool answers must be composed from returned JSON fields.
- Service, auth, timeout, and HTTP failures must be returned as typed structured errors.
- Do not add keyword routing or phrase shortcuts for folder watcher access.
