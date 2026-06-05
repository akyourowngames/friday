# Friday Tool Registry

This registry documents the tools exposed by `assistant_cli.tools`.

## Routing Policy

- Normal chat must not be routed by local keyword, regex, or canned phrase checks.
- Slash commands are explicit commands and may call tools directly.
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
