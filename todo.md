Here's the full plan — architecture, features, edge cases, everything.

---

# FOLDER WATCHER SERVICE — FULL GOD TIER PLAN

---

## WHAT IT IS

A persistent background service that runs on your machine (or a server) and owns a folder. It knows everything that happens in that folder — what arrived, when, what it contains, how it changed, what it means. Your AI doesn't go looking for files. Your AI asks the service and the service already knows. The service is the source of truth for your local filesystem intelligence layer.

---

## CORE PHILOSOPHY

Most people build a script that lists files. That's wrong. This is a stateful daemon with memory, an index, an event log, and an API surface. The difference between a script and this service is the same as the difference between asking someone "what's in the fridge" every time versus having a chef who already knows the inventory, remembers what came in this morning, can tell you what changed since yesterday, and will text you when the thing you asked about arrives.

---

## LAYER 1 — THE WATCHER CORE

This is the lowest layer. It interfaces directly with the OS.

**Mechanism:** Use OS-native inode event APIs. On Linux this is `inotify`. On macOS it is `FSEvents`. On Windows it is `ReadDirectoryChangesW`. Libraries like `watchdog` (Python) or `chokidar` (Node) wrap these so you don't write platform code yourself. The key property is that the OS pushes events to you. You are not polling. There is no loop running `os.listdir()` every N seconds. The kernel wakes your process when something changes. This is why latency is in the single-digit milliseconds.

**What events you capture:**
- `FILE_CREATED` — new file landed
- `FILE_MODIFIED` — existing file was written to
- `FILE_DELETED` — file was removed
- `FILE_MOVED` — file was renamed or moved within the watched tree
- `DIR_CREATED` — new subdirectory appeared
- `DIR_DELETED` — subdirectory removed

**Recursive watching:** You watch the root folder and all subdirectories, including ones that get created after the watcher starts. When a new directory is created, the watcher automatically attaches to it.

**Debouncing:** Editors like VS Code write files in multiple rapid bursts. A single save can trigger 4-6 `FILE_MODIFIED` events in 50ms. You debounce by holding a per-file timer that resets on each event and only fires the handler after 300ms of silence. This way your downstream logic sees one clean `MODIFIED` event per actual user save.

**Ignore patterns:** You never want to index `.git/`, `__pycache__/`, `node_modules/`, `.DS_Store`, swap files, temp files, lock files. These are configured as glob patterns and filtered before anything hits the index. This list should be editable at runtime via a config file that the watcher itself watches — so you can add a pattern and it takes effect immediately without restart.

---

## LAYER 2 — THE INDEX (SQLITE)

Every file that passes through the watcher gets a record in SQLite. SQLite is the right choice here because it is a single file, zero config, embedded in your process, queryable with real SQL, and fast enough for hundreds of thousands of file records without breaking a sweat.

**Schema — files table:**

Every file gets: a UUID primary key, the absolute path, filename, extension, MIME type (sniffed from magic bytes, not just extension), file size in bytes, sha256 hash of contents, created timestamp (from the OS), modified timestamp (from the OS), indexed timestamp (when your daemon first saw it), a JSON blob for arbitrary metadata, a text field for the auto-generated summary, a tags array stored as JSON, a watch status (active / deleted), and a last seen timestamp.

**Why the hash matters:** If a file is deleted and a new file with the same name arrives later, you know they are different files because the hash differs. If two files in different folders have identical content, you know they are duplicates. If a file is "modified" but the hash is the same, the write was a no-op and you can skip re-indexing. The hash is your ground truth.

**Schema — events table:**

Every watcher event gets logged. Event ID, timestamp (microseconds), event type (CREATED / MODIFIED / DELETED / MOVED), the file UUID it refers to, the old path (for moves), the new path (for moves), and a processed flag so you can replay events if a downstream consumer missed them.

This event log is the audit trail. Your AI can ask "what happened in the last 10 minutes" and get a precise ordered list of every filesystem event. Nothing is lost.

**Schema — tags table:**

Auto-generated and user-defined tags live here, linked to file UUIDs. Tags have a source field — `auto:extension`, `auto:mime`, `auto:content`, `auto:ai`, `user`. This lets you filter by how a tag was generated.

**Full text search:** SQLite has a built-in FTS5 extension. You create a virtual FTS table linked to the files table. When a file is indexed, its extracted text content gets inserted into the FTS index. Your AI can then do `query: "agent loop async"` and get ranked results from actual file contents, not just filenames.

---

## LAYER 3 — THE INGEST PIPELINE

When a `FILE_CREATED` or `FILE_MODIFIED` event arrives from the watcher core, it goes through a pipeline of processors before landing in the index. Each processor runs independently and can fail without breaking the others.

**Step 1 — stat reader:** Read size, timestamps, permissions from the OS. Fast, always runs.

**Step 2 — hash computer:** SHA256 the file. Skip if file hasn't changed (compare to stored hash first). For large files, hash in 64KB chunks so you don't block the event loop.

**Step 3 — MIME sniffer:** Read the first 512 bytes of the file and check against magic byte signatures. A file named `.txt` that is actually a zip archive will be correctly identified as `application/zip`. This matters for routing later processors.

**Step 4 — metadata extractor:** Runs based on MIME type.
- Audio files: read ID3/EXIF tags — title, artist, album, duration, bitrate, BPM
- Images: read EXIF — dimensions, color space, GPS coords if present, camera model
- Video: read container metadata — duration, codec, resolution, frame rate
- PDF: read document properties — title, author, page count, creation date
- Code files: detect language, count lines, extract import statements, extract function/class names from the AST
- JSON/YAML/TOML: parse and store the top-level keys as metadata so you can query by structure

**Step 5 — content extractor:** Pull raw text from the file for FTS indexing.
- Plain text, markdown, code: read directly
- PDF: use `pdfplumber` or `pypdf` to extract text per page
- Word docs: use `python-docx`
- Audio: skip (no text content) unless you want to hook in Whisper for transcription
- Images: skip unless you hook in an OCR pipeline

**Step 6 — auto tagger:** Apply deterministic rules first. `.py` extension gets tag `python`. MIME `audio/*` gets tag `audio`. File in a `models/` subdirectory gets tag `ml-model`. File over 100MB gets tag `large-file`. These rules are configurable.

**Step 7 — AI summarizer:** Optional, async, runs in the background after the sync pipeline completes. Takes the extracted text, sends it to Claude (or whatever model you use), gets back a 2-3 sentence summary and a list of semantic tags. Stores both in the index. For code files the prompt says "describe what this code does in 2 sentences and list the key concepts." For documents it says "summarize this in 2 sentences." For configs it says "what does this configuration control?" This is the step that turns your file index into a semantic knowledge base.

**Step 8 — dedup detection:** After hashing, check if any other file in the index has the same hash. If yes, mark both as duplicates and link them. Surface this in the API.

The entire pipeline is async and non-blocking. The watcher core continues catching events while the pipeline processes each file. A slow AI summarizer on a large PDF does not hold up indexing of the `.py` file that just arrived.

---

## LAYER 4 — THE API SURFACE

This is what your AI actually talks to. Built with FastAPI (Python) or Express (Node). Runs on localhost, configurable port (default 7474).

**REST endpoints:**

`GET /files/latest?n=10&ext=py&since=<unix_ts>&dir=models/`
Returns the N most recently indexed files. Filterable by extension, MIME type, subdirectory, time range, tag. Response includes full metadata, tags, summary if available. This is the "what's new" endpoint your AI calls at the start of every session.

`GET /files/diff?since=<unix_ts>`
Returns all events (CREATED, MODIFIED, DELETED, MOVED) that occurred after the given timestamp. Designed for AI agents that checkpoint their last-seen timestamp and only want incremental updates. Zero redundancy, perfect for agents that run on a loop.

`GET /files/<uuid>`
Full record for a single file. Everything the index knows about it.

`GET /files/search?q=<text>&limit=20`
Full text search across all indexed file contents. Uses FTS5. Returns ranked results with file metadata and a snippet of the matching content.

`POST /files/query`
Body: `{"query": "show me all python files related to the agent loop that were modified this week"}`. This endpoint takes a natural language query, sends it to Claude with the index schema and recent file list as context, and Claude generates the appropriate SQL query, which runs against the index, and the results come back. Your AI asks in English, gets back real data.

`GET /files/duplicates`
All duplicate file pairs grouped by hash.

`GET /files/stats`
Total files, breakdown by extension, breakdown by MIME type, total size on disk, files added today/this week, average summary coverage, FTS index size.

`GET /files/<uuid>/summary`
The AI-generated summary for a specific file. If not yet generated, triggers async generation and returns a 202 Accepted. AI can poll or use the webhook to be notified when it's ready.

`GET /files/<uuid>/content`
Returns the extracted text content of a file (not the raw bytes). Safe for AI consumption, already extracted and cleaned.

`DELETE /files/<uuid>`
Removes a file record from the index. Does not delete the actual file from disk.

`POST /files/<uuid>/tags`
Add a user tag to a file.

`GET /config`
Returns current watcher config — watched path, ignore patterns, pipeline settings.

`PATCH /config`
Update config at runtime. Add ignore patterns, toggle AI summarizer on/off, change watched directory. Changes take effect within 1 second without restart.

**WebSocket endpoint:**

`WS /watch`
Your AI connects once. From that point on it receives a JSON message for every filesystem event in real-time. Message format: event type, timestamp, file UUID, filename, path, metadata snapshot. No polling. No diff calls. The service pushes everything. Multiple subscribers supported simultaneously — your AI, your MUSE agent, a monitoring dashboard, all connected at once, all getting the same stream.

The WS connection supports a filter query param: `WS /watch?ext=mp3` means only push events for mp3 files. Useful for MUSE subscribing only to audio events while your code agent subscribes to Python files.

**Webhook support:**

`POST /webhooks`
Register an HTTP endpoint. Body: `{"url": "http://localhost:8000/on-file", "events": ["CREATED", "MODIFIED"], "filter": {"ext": ["py", "json"]}}`. From now on, whenever a matching event fires, the watcher POSTs a JSON payload to your URL. This is how your AI agent gets notified passively without maintaining a WS connection. Your agent's own server just handles the incoming POST.

---

## LAYER 5 — AGENT INTEGRATION PATTERNS

**Pattern 1 — Cold start query.** Your AI boots up, has no idea what's in the folder. It calls `GET /files/latest?n=20` and `GET /files/stats`. Now it has full situational awareness in one round trip. It stores the current timestamp.

**Pattern 2 — Incremental sync.** Next time your AI runs, it calls `GET /files/diff?since=<stored_ts>`. Gets back only what changed. Updates its internal state. Stores new timestamp. Scales to folders with 50,000 files because it never re-reads the whole thing.

**Pattern 3 — Reactive via WebSocket.** Your AI maintains a WS connection. When a file arrives that matches its interest (new `.py` file, new audio, new config), it immediately processes it — summarizes it, asks a question about it, routes it to the right handler. Zero latency from file arrival to AI response.

**Pattern 4 — Natural language file query.** User says to your AI "what was the last thing I was working on?" Your AI calls `POST /files/query` with that exact phrase. The service resolves it against the index and returns the most recently modified files with their summaries. Your AI synthesizes a natural response.

**Pattern 5 — Webhook-driven pipeline.** Your AI has its own local server. It registers a webhook with the file watcher. When a new audio file lands, the watcher POSTs to your AI's server. Your AI's server wakes up, fetches the file metadata, routes it to MUSE, which starts playback or adds it to the playlist. The user dropped a file in the folder and music started playing. No polling. No manual commands.

---

## GOD TIER FEATURES

**Change velocity tracking.** The index tracks how often each file is modified per day. Files that are modified frequently get a `hot` tag automatically. Your AI can call `GET /files?tag=hot` to find what you are actively working on without you telling it anything.

**Directory intent detection.** The service watches what kinds of files land in which subdirectories and learns patterns. `models/` always gets `.bin` and `.safetensors`. `prompts/` always gets `.txt` and `.json`. `audio/` always gets `.mp3` and `.flac`. If a `.exe` lands in `prompts/`, the service flags it as anomalous and emits a special `ANOMALY` event. Your AI can respond to this.

**File relationship graph.** If file A imports file B (detected via AST parsing of Python imports), the index stores an edge between them. `GET /files/<uuid>/dependencies` returns all files that this file imports. `GET /files/<uuid>/dependents` returns all files that import this file. Your AI can understand your codebase's structure without reading every file.

**Content-addressed dedup with symlink suggestion.** When two files have the same hash, the service can suggest (or automatically create) a symlink so only one copy exists on disk.

**Automatic playlist generation for MUSE.** When new audio files land, the service emits a structured event to MUSE specifically. MUSE subscribes to `WS /watch?mime=audio/*`. Every new mp3 or flac triggers MUSE to add it to a "new arrivals" playlist automatically. The folder is now a drop zone.

**Snapshot and diff.** `GET /files/snapshot?at=<unix_ts>` returns what the index looked like at any past timestamp, reconstructed from the event log. `GET /files/diff?from=<ts1>&to=<ts2>` shows everything that changed between two points in time. You can ask "what did my project folder look like three days ago."

**Summary coverage reporting.** `GET /files/stats` includes what percentage of files have been AI-summarized. You can call `POST /files/summarize-pending` to kick off a background job that summarizes all files that don't have summaries yet. Useful on first run against an existing folder.

**Export the entire index.** `GET /export?format=json` or `GET /export?format=csv`. Your AI can pull the entire knowledge base as a single JSON blob and reason over it offline. Useful for "analyze everything in my project folder and tell me what I should focus on today."

**Config hot reload.** The service watches its own config file. Edit the ignore patterns, watched path, or API settings and they apply within a second. No restart, no dropped WS connections.

**Auth token.** All API endpoints optionally require a bearer token configured at startup. Prevents other processes on the machine from querying your file index. Simple but necessary if you run other services locally.

**Rate limiting per subscriber.** Each WS client and each webhook URL gets its own rate limit. A misbehaving AI agent that floods the query endpoint doesn't affect other consumers.

---

## DEPLOYMENT OPTIONS

**Option 1 — Local daemon, foreground.** You run `python watcher.py --path ~/projects/my-ai`. It logs to stdout. You keep the terminal open. Simple for development.

**Option 2 — systemd service (Linux).** Write a unit file. `systemctl enable folder-watcher` and it starts at boot, restarts on crash, logs to journald. This is the production option on Linux.

**Option 3 — launchd plist (macOS).** Same idea. Drops a plist into `~/Library/LaunchAgents/`. Runs on login, background, invisible.

**Option 4 — Docker container.** Mount the watched folder as a volume. Expose port 7474. Works anywhere Docker runs. Now your AI in another container can call `http://watcher:7474/files/latest` and it just works.

**Option 5 — Remote server.** Run the watcher on a remote machine with a large disk. Mount the watched folder over NFS or SSHFS. Expose the API over HTTPS with nginx in front. Now your AI can query a remote file store from anywhere.

---

## WHAT YOUR AI GETS

From your AI's perspective, the folder is no longer a dumb directory. It is a queryable, searchable, real-time knowledge base. Your AI never has to ask "what's in there" the slow way. It calls one endpoint and immediately knows everything — what's new, what changed, what things mean, how they relate, what you've been working on. When you drop a file in the folder, your AI knows within 12 milliseconds and can act on it without you saying anything. That is the point of this entire system.