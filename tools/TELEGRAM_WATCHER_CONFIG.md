# KING Telegram Watcher Config

This markdown file controls the Telegram watcher service. The runtime reads this
file at startup so Telegram behavior stays editable without adding phrase
shortcuts, user ids, folder paths, or credentials in code.

## Runtime

- token_env: TELEGRAM_BOT_TOKEN
- authorized_user_ids_env: KING_TELEGRAM_AUTHORIZED_USER_IDS
- authorized_chat_ids_env: KING_TELEGRAM_AUTHORIZED_CHAT_IDS
- unlock_pin_env: KING_TELEGRAM_UNLOCK_PIN
- state_path: storage/telegram_watcher_state.json
- session_log_path: storage/telegram_watcher_session.jsonl
- polling_timeout_seconds: 30
- request_timeout_ms: 15000
- api_host: 127.0.0.1
- api_port: 7480
- service_base_url: http://127.0.0.1:7480
- main_cli_autostart: false
- cli_bridge_enabled: true
- api_startup_wait_ms: 3000
- local_cli_chat_id: -9001
- local_cli_user_id: -9001
- folder_watcher_base_url: client_active_target
- folder_watcher_auth_env: KING_FOLDER_WATCHER_AUTH_TOKEN
- folder_watcher_timeout_ms: 12000
- max_file_size: 50MB
- max_results: 10
- max_scan_files: 5000
- default_new_window: 24h
- rate_limit_queries_per_minute: 30
- rate_limit_sends_per_minute: 10
- semantic_min_score: 0.35
- semantic_min_margin: 0.05
- fallback_action: ask
- push_check_interval_seconds: 30
- push_event_limit: 20
- push_auto_send: false
- startup_notice_enabled: true
- startup_notice_text: KING Telegram watcher is online. You can talk naturally.

## Environment Overrides

- `TELEGRAM_BOT_TOKEN` supplies the Telegram bot token from BotFather.
- `KING_TELEGRAM_AUTHORIZED_USER_IDS` is a comma-separated list of numeric Telegram user ids.
- `KING_TELEGRAM_AUTHORIZED_CHAT_IDS` is a comma-separated list of numeric Telegram chat ids.
- `KING_TELEGRAM_UNLOCK_PIN` supplies the panic-lock unlock PIN.
- `KING_TELEGRAM_CONFIG_FILE` selects this markdown config path.
- `KING_TELEGRAM_STATE_PATH` overrides `state_path`.
- `KING_TELEGRAM_SESSION_LOG` overrides `session_log_path`.
- `KING_TELEGRAM_API_HOST` overrides `api_host`.
- `KING_TELEGRAM_API_PORT` overrides `api_port`.
- `KING_TELEGRAM_SERVICE_BASE_URL` overrides `service_base_url`.
- `KING_TELEGRAM_MAIN_CLI_AUTOSTART` overrides `main_cli_autostart`.
- `KING_TELEGRAM_CLI_BRIDGE_ENABLED` overrides `cli_bridge_enabled`.
- `KING_TELEGRAM_API_STARTUP_WAIT_MS` overrides `api_startup_wait_ms`.
- `KING_TELEGRAM_LOCAL_CLI_CHAT_ID` overrides `local_cli_chat_id`.
- `KING_TELEGRAM_LOCAL_CLI_USER_ID` overrides `local_cli_user_id`.
- `KING_TELEGRAM_FOLDER_WATCHER_BASE_URL` overrides `folder_watcher_base_url`.
- `client_active_target` means derive the URL from the active target in
  `tools/FOLDER_WATCHER_CLIENT.md`, so Telegram watcher and KING's normal
  folder watcher bridge use the same running service.

## Allowed Zones

- desktop: %USERPROFILE%/Desktop | enabled: true
- downloads: %USERPROFILE%/Downloads | enabled: true
- documents: %USERPROFILE%/Documents | enabled: true
- drop: storage/telegram_drop | enabled: true

## Blocked Suffixes

- .env
- .pem
- .key
- .p12
- .pfx
- .gpg
- .kdbx
- .exe
- .dll
- .msi
- .bat
- .cmd
- .ps1

## Blocked Name Fragments

- credential
- credentials
- secret
- secrets
- password
- passwords
- token
- tokens
- desktop.ini

## Blocked Path Parts

- .ssh
- .gnupg
- __pycache__
- .git

## Command Aliases

- files: status
- status: status
- health: health
- latest: latest
- find: find
- search: search
- send: send
- sendfile: sendfile
- info: info
- new: new
- morning: new
- ls: list
- list: list
- stats: stats
- lockdown: lockdown
- unlock: unlock
- watch: watch_on
- watchon: watch_on
- watchoff: watch_off

## Action Semantics

- status: Service visibility, allowed zones, watcher reachability, and what the Telegram watcher can currently see.
- health: Runtime diagnostics, token presence, authorized-id configuration, state file, and session log configuration.
- latest: Recently modified or indexed files inside allowed zones, including recent media, documents, code, and downloads.
- find: Find files by name, path, extension, content hint, or fuzzy description and return a pickable list.
- search: Search indexed file content or local allowed-zone filenames for the user's requested evidence.
- send: Deliver the single requested file, or find likely matches and ask the user to pick when multiple safe files match.
- sendfile: Send any file from the PC by exact path, bypassing zone restrictions. Use /sendfile <path>.
- info: Show metadata, path, size, timestamps, summary, snippet, and tags before sending the file.
- new: Morning briefing or recent-arrival view for files created or modified during the configured time window.
- list: List configured allowed zones and safe visible file counts without exposing out-of-scope paths.
- stats: Folder watcher and allowed-zone file intelligence statistics.
- ask: Natural grounded conversation about allowed files when the request is broad, mixed, uncertain, or not clearly a delivery action.
- lockdown: Panic lock that stops Telegram file service responses until the configured unlock PIN is supplied.
- unlock: Unlock a locked Telegram watcher only when the configured PIN is supplied.
- watch_on: Enable proactive Telegram notifications for new allowed-zone watcher events.
- watch_off: Disable proactive Telegram notifications.

## CLI Forward Actions

- status
- health
- latest
- find
- search
- send
- sendfile
- info
- new
- list

## Contract

- Natural Telegram text is routed through the action semantics above, not phrase
  tables or keyword shortcuts.
- The default `ask` action uses the full KING agent (same as CLI) for natural
  conversation with memory, tools, and persona. If the agent is unavailable, it
  falls back to folder watcher chat and then file search.
- `/sendfile <path>` sends any file from the PC by exact path, bypassing zone
  restrictions. Blocked suffixes still apply.
- Normal `python main.py` CLI mode does not forward arbitrary messages to
  Telegram watcher. The foreground chat stays with KING.
- The Telegram watcher remains a separate background service and is exposed to
  KING through the `telegram_watcher` registry tool.
- Local requests reach Telegram only when KING explicitly calls the
  `telegram_watcher` tool; broad `ask` chat remains with the main KING
  assistant.
- Folder inventory questions such as counts, type breakdowns, and broad folder
  intelligence stay with KING's normal Folder Watcher path instead of the
  Telegram delivery bridge.
- Slash commands are supported for precision but are optional.
- Numeric replies only act on the current pick list for that chat.
- Files outside allowed zones are not listed, sent, or acknowledged by the
  `send` action. Use `/sendfile` for arbitrary paths.
- Blocked suffixes, name fragments, and path parts override zone membership.
- Missing Folder Watcher service falls back to bounded local scans of allowed
  zones only.
- Unauthorized Telegram user ids or chat ids are silently ignored.
- Unauthorized setup probes record only chat/user/update ids, not message text.
- Credentials and PINs must come from environment variables, not from markdown.
