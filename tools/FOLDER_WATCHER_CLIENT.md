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

## Contract

- The client is read-only in v1.
- It calls the folder watcher HTTP API and does not read the SQLite database directly.
- Tool answers must be composed from returned JSON fields.
- Service, auth, timeout, and HTTP failures must be returned as typed structured errors.
- Do not add keyword routing or phrase shortcuts for folder watcher access.
