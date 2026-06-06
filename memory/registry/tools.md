# Friday Tool Registry

This registry documents the tools exposed by `assistant_cli.tools`.
Each tool implementation lives in `assistant_cli/tools/<tool_name>.py`.

## Routing Policy

- Normal chat must not be routed by local keyword, regex, or canned phrase checks.
- Slash commands are explicit commands and may call tools directly.
- Normal chat uses a prompt/model-driven planner over the registered tool specs before the assistant writes the final answer.
- A registry-driven prefilter skips the planner for casual messages with no tool-shaped catalog match.
- Tool behavior lives in registered tool specs and handlers, not natural-language assistant replies.
- Realtime web search requires `TAVILY_API_KEY`; other public HTTP tools use short timeouts from `FRIDAY_TOOL_TIMEOUT_SECONDS`.

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
python chat.py --tool url_fetch --tool-args 'url=https://example.com max_chars=1000'
python chat.py --tool file_list --tool-args 'path=. max_entries=20'
python chat.py --tool file_read --tool-args 'path=README.md max_chars=1200'
python chat.py --tool note_add --tool-args 'text="ship tool split"'
python chat.py --tool note_list --tool-args 'limit=5'
```
