# KING Folder Watcher Demo Config

This config keeps visual testing isolated from the repository root. Use it when
you want to see the service working without indexing the whole project tree.

## Paths And Runtime

- watch_path: storage/folder_watcher_inbox
- database_path: storage/folder_watcher_demo.sqlite3
- api_host: 127.0.0.1
- api_port: 7475
- debounce_ms: 200
- scan_on_start: true
- auth_token:
- max_content_chars: 50000
- hash_chunk_bytes: 65536
- large_file_size: 10MB
- ai_summaries_enabled: true
- llm_queries_enabled: true
- llm_policy_file: tools/FOLDER_WATCHER_LLM_POLICY.md
- hot_file_event_threshold: 2
- hot_file_window_seconds: 86400
- anomaly_events_enabled: true
- ocr_enabled: true
- transcription_enabled: true
- subscriber_rate_limit_per_sec: 20
- webhook_rate_limit_per_sec: 5
- playlist_path: storage/folder_watcher_demo_new_arrivals.m3u

## Ignore Globs

- .git/**
- .next/**
- node_modules/**
- __pycache__/**
- .pytest_cache/**
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
- extension:.txt -> text
- extension:.json -> json
- extension:.toml -> toml
- mime-prefix:text/ -> text
- directory:models -> ml-model
- directory:prompts -> prompt
- directory:audio -> audio
- size-over:10MB -> large-file

## Directory Intent Rules

- models: .bin,.safetensors,.onnx,.pt,.pth
- prompts: .txt,.md,.json,.yaml,.yml
- audio: .mp3,.flac,.wav,.m4a,.ogg
- documents: .pdf,.docx,.txt,.md
