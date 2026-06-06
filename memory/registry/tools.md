# Friday Tool Registry

This registry documents the tools exposed by `assistant_cli.tools`.
Each tool implementation lives in `assistant_cli/tools/<tool_name>.py`.

## Routing Policy

- Normal chat must not be routed by local keyword, regex, or canned phrase checks.
- Slash commands are explicit commands and may call tools directly.
- Normal chat uses NVIDIA native function calling over the registered tool schemas before the assistant writes the final answer.
- Tool behavior lives in registered tool specs and handlers, not natural-language assistant replies.
- Realtime web search requires `TAVILY_API_KEY`; other public HTTP tools use short timeouts from `FRIDAY_TOOL_TIMEOUT_SECONDS`.
- Project/task auto-routing uses the dedicated one-tool-per-file schemas. The legacy `project_manage` tool remains available for explicit `/tool` commands.
- Project task follow-ups that refer to a listed group must preserve the complete referenced set; project-scoped bulk actions use the dedicated bulk tools.

## Commands

```text
/tools
/tool <name> <json|key=value args>
python chat.py --tool <name> --tool-args "<key=value args>"
```

## Registered Tools

- `realtime_search`: Tavily current web search.
- `weather`: current weather by place or coordinates.
- `geocode`: place name to latitude/longitude.
- `reverse_geocode`: latitude/longitude to place name.
- `current_time`: date/time by timezone.
- `calculator`: safe arithmetic evaluation.
- `unit_convert`: temperature, length, mass, and volume conversion.
- `hash_text`: sha256, sha1, or md5 hashing.
- `base64_encode`: text to base64.
- `base64_decode`: base64 to text.
- `json_format`: JSON validation and pretty print.
- `uuid_generate`: UUID generation.
- `random_number`: random integer generation.
- `password_generate`: local random password generation.
- `project_create`, `project_list`, `project_get`, `project_update`, `project_archive`: persistent SQLite project operations.
- `task_create`, `task_create_many`, `task_list`, `task_update`, `task_bulk_update`: persistent task creation, reads, and updates.
- `task_complete`, `task_complete_all`, `task_reopen_all`, `task_delete`: task lifecycle operations.
- `project_manage`: legacy explicit-command gateway for project/task operations; excluded from normal auto-routing.
- `url_fetch`: HTTP/HTTPS text fetch.
- `file_list`: workspace-safe file listing.
- `file_read`: workspace-safe text file reading.
- `note_add`: append local note JSONL.
- `note_list`: list local notes.

## CLI Prompts

```powershell
python chat.py --tool realtime_search --tool-args 'query="latest NVIDIA NIM models" max_results=5'
python chat.py --tool weather --tool-args 'location=Delhi'
python chat.py --tool geocode --tool-args 'location="New Delhi" count=3'
python chat.py --tool reverse_geocode --tool-args 'latitude=28.65195 longitude=77.23149'
python chat.py --tool current_time --tool-args 'timezone=Asia/Kolkata'
python chat.py --tool calculator --tool-args 'expression="(22 / 7) * 3"'
python chat.py --tool unit_convert --tool-args 'value=72 from_unit=fahrenheit to_unit=celsius'
python chat.py --tool hash_text --tool-args 'text=friday algorithm=sha256'
python chat.py --tool base64_encode --tool-args 'text=friday'
python chat.py --tool base64_decode --tool-args 'text=ZnJpZGF5'
python chat.py --tool json_format --tool-args 'json_text="{\"b\":2,\"a\":1}"'
python chat.py --tool uuid_generate --tool-args 'count=3'
python chat.py --tool random_number --tool-args 'minimum=1 maximum=10 count=3'
python chat.py --tool password_generate --tool-args 'length=20 symbols=true'
python chat.py --tool project_create --tool-args 'name=Friday description="Local AI assistant"'
python chat.py --tool project_list
python chat.py --tool project_get --tool-args 'project="my Friday project"'
python chat.py --tool project_update --tool-args 'project=Friday description="My AI assistant"'
python chat.py --tool project_archive --tool-args 'project=Friday'
python chat.py --tool task_create --tool-args 'project=Friday title="fix voice latency" priority=high due="tomorrow 5pm"'
python chat.py --tool task_create_many --tool-args 'project=Friday tasks="[{\"title\":\"Tool sanity\"},{\"title\":\"JSONL audit\"}]"'
python chat.py --tool task_list --tool-args 'project=Friday status=pending'
python chat.py --tool task_update --tool-args 'project=Friday task="JSONL audit" priority=urgent due="next Friday at 6pm"'
python chat.py --tool task_bulk_update --tool-args 'project=Friday match_status=pending priority=high'
python chat.py --tool task_complete --tool-args 'project=Friday task="fix voice latency"'
python chat.py --tool task_complete_all --tool-args 'project=Friday'
python chat.py --tool task_reopen_all --tool-args 'project=Friday'
python chat.py --tool task_delete --tool-args 'project=Friday task="obsolete task"'
python chat.py --tool project_manage --tool-args action=project_create name=Friday
python chat.py --tool project_manage --tool-args action=task_create project=Friday title="fix voice latency" priority=high due=tomorrow tags=voice
python chat.py --tool project_manage --tool-args action=task_create_many project=Friday tasks='[{"title":"Tool sanity"},{"title":"Persistence check"},{"title":"Latency pass"},{"title":"JSONL audit"}]'
python chat.py --tool project_manage --tool-args action=task_bulk_update project=Friday match_status=pending priority=high due="tomorrow 5pm"
python chat.py --tool project_manage --tool-args action=task_complete_all project=Friday
python chat.py --tool project_manage --tool-args action=task_complete task="fix voice latency" project=Friday
python chat.py --tool project_manage --tool-args action=task_list project=Friday status=pending
python chat.py --tool project_manage --tool-args action=summary
python chat.py --tool url_fetch --tool-args 'url=https://example.com max_chars=1000'
python chat.py --tool file_list --tool-args 'path=. max_entries=20'
python chat.py --tool file_read --tool-args 'path=README.md max_chars=1200'
python chat.py --tool note_add --tool-args 'text="ship tool split"'
python chat.py --tool note_list --tool-args 'limit=5'
```
