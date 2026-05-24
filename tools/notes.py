import json
import time
from datetime import datetime
from pathlib import Path

from config import settings
from tools.registry import tool
from tools.runtime import (
    coerce_bool,
    emit_trace,
    error_payload,
    make_trace,
    normalize_int,
    normalize_response_format,
    structured_error,
    structured_success,
    utc_now_iso,
)

_NOTES_VERSION = "2.0.0"
NOTES_FILE = Path(settings.notes_file).expanduser()
if not NOTES_FILE.is_absolute():
    NOTES_FILE = Path(__file__).resolve().parent.parent / NOTES_FILE


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def _load_notes() -> dict:
    if NOTES_FILE.exists():
        return json.loads(NOTES_FILE.read_text(encoding="utf-8"))
    return {}


def _save_notes(notes: dict) -> None:
    NOTES_FILE.parent.mkdir(parents=True, exist_ok=True)
    NOTES_FILE.write_text(json.dumps(notes, indent=2, ensure_ascii=False), encoding="utf-8")


def _migrate(notes: dict) -> dict:
    """Upgrade old flat-string notes to structured format."""
    changed = False
    for title, value in list(notes.items()):
        if isinstance(value, str):
            content = value
            tags = []
            rest = content
            saved_ts = ""
            if "_Saved: " in rest:
                parts = rest.rsplit("_Saved: ", 1)
                rest = parts[0].rstrip()
                saved_ts = parts[1].split("\n")[0].strip()
            if "_Tags: " in rest:
                parts = rest.rsplit("_Tags: ", 1)
                rest = parts[0].rstrip()
                tags = [t.strip() for t in parts[1].split(",") if t.strip()]
            notes[title] = {
                "content": rest.strip(),
                "created": saved_ts or _now(),
                "updated": saved_ts or _now(),
                "tags": tags,
            }
            changed = True
    if changed:
        _save_notes(notes)
    return notes


def _format_preview(text: str, max_len: int = 80) -> str:
    text = text.replace("\n", " ")
    return text[:max_len] + "..." if len(text) > max_len else text


def _find_note_title(notes: dict, title: str) -> tuple[str | None, str | None, list[str]]:
    if title in notes:
        return title, None, []
    wanted = title.strip().lower()
    matches = [k for k in notes if wanted and wanted in k.lower()]
    if not matches:
        return None, None, []
    if len(matches) > 1:
        shown = "\n".join(f"- {match}" for match in matches)
        return None, f"Ambiguous note title '{title}'. Matches:\n{shown}", matches
    return matches[0], None, matches


def _note_record(n: dict, resolved_title: str, requested_title: str) -> dict:
    return {
        "title": resolved_title,
        "requested_title": requested_title,
        "content": n.get("content", ""),
        "tags": list(n.get("tags", [])),
        "created": n.get("created", ""),
        "updated": n.get("updated", ""),
    }


def _notes_trace(
    tool_name: str,
    started_at: str,
    started: float,
    inputs_received: int,
    schema_valid: bool,
    execution_path: str,
    status: str,
    output_fields: int,
    error_code: str | None = None,
) -> dict:
    return make_trace(
        tool_name,
        _NOTES_VERSION,
        started_at,
        started,
        inputs_received,
        schema_valid,
        execution_path,
        status,
        output_fields,
        {"count": 1, "systems": ["notes_storage"]},
        error_code,
    )


def _notes_error(
    tool_name: str,
    error: dict,
    response_format: str,
    trace_enabled: bool,
    started: float,
    started_at: str,
    inputs_received: int,
    legacy: str,
    execution_path: str = "input_validation",
):
    trace = _notes_trace(tool_name, started_at, started, inputs_received, False, execution_path, "FAILED", 1, error["code"])
    emit_trace(trace, trace_enabled)
    if response_format == "structured":
        return structured_error(tool_name, _NOTES_VERSION, error, started, trace)
    return legacy


def _notes_success(
    tool_name: str,
    result: dict,
    response_format: str,
    trace_enabled: bool,
    started: float,
    started_at: str,
    inputs_received: int,
    legacy: str,
    execution_path: str,
    status: str = "SUCCESS",
):
    trace = _notes_trace(tool_name, started_at, started, inputs_received, True, execution_path, status, len(result))
    emit_trace(trace, trace_enabled)
    if response_format == "structured":
        return structured_success(tool_name, _NOTES_VERSION, result, started, trace)
    return legacy


def _storage_error(tool_name: str, response_format: str, trace_enabled: bool, started: float, started_at: str, inputs_received: int, legacy: str):
    error = error_payload(
        "STORAGE_ERROR",
        "Note storage could not be read or written.",
        "storage_path",
        str(NOTES_FILE),
        "readable and writable notes file",
        True,
        "Verify storage permissions and retry if still needed.",
    )
    return _notes_error(tool_name, error, response_format, trace_enabled, started, started_at, inputs_received, legacy, "storage")


@tool(
    name="note_save",
    description="Save a note with a title, content, and optional tags. Overwrites existing note with same title",
    examples=[
        "save a note about project ideas",
        "take a note: meeting at 3pm",
        "note down the api endpoints",
        "save recipe for pasta with tags: food, recipes",
    ],
    param_descriptions={
        "title": "Note title (used as unique identifier)",
        "content": "Note body text",
        "tags": "Optional comma-separated tags for categorizing",
        "response_format": "legacy or structured. Default legacy preserves existing output",
        "trace_enabled": "When true, emit a machine-readable trace entry",
    },
)
def note_save(
    title: str,
    content: str,
    tags: str = "",
    response_format: str = "legacy",
    trace_enabled: bool = False,
):
    started = time.perf_counter()
    started_at = utc_now_iso()
    inputs_received = 5
    response_format = normalize_response_format(response_format)
    trace_enabled = coerce_bool(trace_enabled)
    title = str(title or "").strip()
    if not title:
        error = error_payload(
            "EMPTY_TITLE",
            "title must not be empty.",
            "title",
            title,
            "non-empty note title",
            False,
            "Pass a title that identifies the note.",
        )
        return _notes_error("note_save", error, response_format, trace_enabled, started, started_at, inputs_received, "Error: title is required")
    try:
        notes = _migrate(_load_notes())
        now = _now()
        tag_list = [t.strip() for t in tags.split(",") if t.strip()] if tags else []
        is_new = title not in notes
        existing = notes.get(title, {})
        notes[title] = {
            "content": content,
            "created": existing.get("created", now) if not is_new else now,
            "updated": now,
            "tags": tag_list if tag_list else (existing.get("tags", []) if not is_new else []),
        }
        _save_notes(notes)
        action = "created" if is_new else "updated"
        legacy = f"{'Updated' if not is_new else 'Saved'} note '{title}'"
        result = {
            "action": action,
            "title": title,
            "is_new": is_new,
            "tags": notes[title]["tags"],
            "content_length": len(content),
            "storage_path": str(NOTES_FILE),
            "note_count": len(notes),
        }
        return _notes_success("note_save", result, response_format, trace_enabled, started, started_at, inputs_received, legacy, "save")
    except (json.JSONDecodeError, OSError):
        return _storage_error("note_save", response_format, trace_enabled, started, started_at, inputs_received, "Error saving note: storage unavailable")


@tool(
    name="note_read",
    description="Read a note by title (exact match preferred, falls back to partial match). Shows content, tags, and timestamps",
    examples=[
        "show me my note about project ideas",
        "what did I write about python tips",
        "read my note about weekend plans",
    ],
    param_descriptions={
        "title": "Note title or partial title to read",
        "response_format": "legacy or structured. Default legacy preserves existing output",
        "trace_enabled": "When true, emit a machine-readable trace entry",
    },
)
def note_read(title: str, response_format: str = "legacy", trace_enabled: bool = False):
    started = time.perf_counter()
    started_at = utc_now_iso()
    inputs_received = 3
    response_format = normalize_response_format(response_format)
    trace_enabled = coerce_bool(trace_enabled)
    title = str(title or "").strip()
    if not title:
        error = error_payload(
            "EMPTY_TITLE",
            "title must not be empty.",
            "title",
            title,
            "non-empty note title",
            False,
            "Pass the note title to read.",
        )
        return _notes_error("note_read", error, response_format, trace_enabled, started, started_at, inputs_received, "Error: title is required")
    try:
        notes = _migrate(_load_notes())
        match, amb_error, _ = _find_note_title(notes, title)
        if amb_error:
            error = error_payload(
                "AMBIGUOUS_TITLE",
                "Multiple notes matched the requested title.",
                "title",
                title,
                "unique note title",
                False,
                "Use the exact note title from note_list.",
            )
            if response_format == "structured":
                return _notes_error("note_read", error, response_format, trace_enabled, started, started_at, inputs_received, amb_error, "resolve")
            return amb_error
        if match:
            n = notes[match]
            record = _note_record(n, match, title)
            legacy_parts = [n["content"]]
            if match != title:
                legacy_parts.insert(0, f"[{match}]\n")
            if n.get("tags"):
                legacy_parts.append(f"\nTags: {', '.join(n['tags'])}")
            legacy_parts.append(f"\nCreated: {n.get('created', '?')}  |  Updated: {n.get('updated', '?')}")
            legacy = "".join(legacy_parts)
            result = {"found": True, "note": record, "note_count": len(notes)}
            return _notes_success("note_read", result, response_format, trace_enabled, started, started_at, inputs_received, legacy, "read")
        if not notes:
            legacy = "No notes saved"
            result = {"found": False, "note": None, "note_count": 0, "available_titles": []}
            return _notes_success("note_read", result, response_format, trace_enabled, started, started_at, inputs_received, legacy, "read", "PARTIAL")
        legacy = f"Note '{title}' not found. Titles:\n" + "\n".join(f"- {t}" for t in notes)
        result = {"found": False, "note": None, "note_count": len(notes), "available_titles": list(notes)}
        return _notes_success("note_read", result, response_format, trace_enabled, started, started_at, inputs_received, legacy, "read", "PARTIAL")
    except (json.JSONDecodeError, OSError):
        return _storage_error("note_read", response_format, trace_enabled, started, started_at, inputs_received, "Error reading notes: storage unavailable")


@tool(
    name="note_update",
    description="Update an existing note's content and/or tags. Merges tags if not provided",
    examples=[
        "update my project ideas note with new content",
        "edit my meeting notes",
        "add tags to my recipe note",
    ],
    param_descriptions={
        "title": "Title of the note to update",
        "content": "New content to replace existing (leave empty to keep)",
        "tags": "New comma-separated tags (leave empty to keep existing)",
        "response_format": "legacy or structured. Default legacy preserves existing output",
        "trace_enabled": "When true, emit a machine-readable trace entry",
    },
)
def note_update(
    title: str,
    content: str = "",
    tags: str = "",
    response_format: str = "legacy",
    trace_enabled: bool = False,
):
    started = time.perf_counter()
    started_at = utc_now_iso()
    inputs_received = 5
    response_format = normalize_response_format(response_format)
    trace_enabled = coerce_bool(trace_enabled)
    title = str(title or "").strip()
    if not title:
        error = error_payload(
            "EMPTY_TITLE",
            "title must not be empty.",
            "title",
            title,
            "non-empty note title",
            False,
            "Pass the note title to update.",
        )
        return _notes_error("note_update", error, response_format, trace_enabled, started, started_at, inputs_received, "Error: title is required")
    try:
        notes = _migrate(_load_notes())
        match, amb_error, _ = _find_note_title(notes, title)
        if amb_error:
            if response_format == "structured":
                error = error_payload(
                    "AMBIGUOUS_TITLE",
                    "Multiple notes matched the requested title.",
                    "title",
                    title,
                    "unique note title",
                    False,
                    "Use the exact note title from note_list.",
                )
                return _notes_error("note_update", error, response_format, trace_enabled, started, started_at, inputs_received, amb_error, "resolve")
            return amb_error
        if not match:
            error = error_payload(
                "NOTE_NOT_FOUND",
                "The requested note was not found.",
                "title",
                title,
                "existing note title",
                False,
                "Use note_list to see available titles.",
            )
            return _notes_error("note_update", error, response_format, trace_enabled, started, started_at, inputs_received, f"Note '{title}' not found", "resolve")
        n = notes[match]
        changed_fields = []
        if content:
            n["content"] = content
            changed_fields.append("content")
        if tags:
            n["tags"] = [t.strip() for t in tags.split(",") if t.strip()]
            changed_fields.append("tags")
        n["updated"] = _now()
        _save_notes(notes)
        legacy = f"Updated note '{match}'"
        result = {
            "title": match,
            "requested_title": title,
            "changed_fields": changed_fields or ["updated"],
            "note": _note_record(n, match, title),
        }
        return _notes_success("note_update", result, response_format, trace_enabled, started, started_at, inputs_received, legacy, "update")
    except (json.JSONDecodeError, OSError):
        return _storage_error("note_update", response_format, trace_enabled, started, started_at, inputs_received, "Error updating note: storage unavailable")


@tool(
    name="note_delete",
    description="Delete a note by exact or partial title match",
    examples=[
        "delete my note about project ideas",
        "remove note about meeting",
        "delete note python tips",
    ],
    param_descriptions={
        "title": "Note title or partial title to delete",
        "response_format": "legacy or structured. Default legacy preserves existing output",
        "trace_enabled": "When true, emit a machine-readable trace entry",
    },
)
def note_delete(title: str, response_format: str = "legacy", trace_enabled: bool = False):
    started = time.perf_counter()
    started_at = utc_now_iso()
    inputs_received = 3
    response_format = normalize_response_format(response_format)
    trace_enabled = coerce_bool(trace_enabled)
    title = str(title or "").strip()
    if not title:
        error = error_payload(
            "EMPTY_TITLE",
            "title must not be empty.",
            "title",
            title,
            "non-empty note title",
            False,
            "Pass the note title to delete.",
        )
        return _notes_error("note_delete", error, response_format, trace_enabled, started, started_at, inputs_received, "Error: title is required")
    try:
        notes = _migrate(_load_notes())
        match, amb_error, _ = _find_note_title(notes, title)
        if amb_error:
            if response_format == "structured":
                error = error_payload(
                    "AMBIGUOUS_TITLE",
                    "Multiple notes matched the requested title.",
                    "title",
                    title,
                    "unique note title",
                    False,
                    "Use the exact note title from note_list.",
                )
                return _notes_error("note_delete", error, response_format, trace_enabled, started, started_at, inputs_received, amb_error, "resolve")
            return amb_error
        if match:
            del notes[match]
            _save_notes(notes)
            legacy = f"Deleted note '{match}'"
            result = {"deleted": True, "title": match, "requested_title": title, "note_count": len(notes)}
            return _notes_success("note_delete", result, response_format, trace_enabled, started, started_at, inputs_received, legacy, "delete")
        if not notes:
            legacy = "No notes saved"
            result = {"deleted": False, "title": None, "note_count": 0}
            return _notes_success("note_delete", result, response_format, trace_enabled, started, started_at, inputs_received, legacy, "delete", "PARTIAL")
        error = error_payload(
            "NOTE_NOT_FOUND",
            "The requested note was not found.",
            "title",
            title,
            "existing note title",
            False,
            "Use note_list to see available titles.",
        )
        legacy = f"Note '{title}' not found. Titles:\n" + "\n".join(f"- {t}" for t in notes)
        if response_format == "structured":
            return _notes_error("note_delete", error, response_format, trace_enabled, started, started_at, inputs_received, legacy, "resolve")
        return legacy
    except (json.JSONDecodeError, OSError):
        return _storage_error("note_delete", response_format, trace_enabled, started, started_at, inputs_received, "Error deleting note: storage unavailable")


@tool(
    name="note_list",
    description="List all saved notes. Optionally filter by tag",
    examples=[
        "list my notes",
        "what notes do I have",
        "show notes tagged with food",
        "list notes with tag recipes",
    ],
    param_descriptions={
        "tag": "Optional tag to filter notes by (e.g. 'food', 'work')",
        "preview_chars": "Preview length for note content when filtering by tag, from 20 to 200",
        "response_format": "legacy or structured. Default legacy preserves existing output",
        "trace_enabled": "When true, emit a machine-readable trace entry",
    },
)
def note_list(
    tag: str = "",
    preview_chars: int = 60,
    response_format: str = "legacy",
    trace_enabled: bool = False,
):
    started = time.perf_counter()
    started_at = utc_now_iso()
    inputs_received = 4
    response_format = normalize_response_format(response_format)
    trace_enabled = coerce_bool(trace_enabled)
    preview_chars, preview_error = normalize_int(
        preview_chars,
        "preview_chars",
        60,
        20,
        200,
        "Use preview_chars between 20 and 200.",
        "INVALID_PREVIEW_CHARS",
    )
    if preview_error is not None:
        return _notes_error("note_list", preview_error, response_format, trace_enabled, started, started_at, inputs_received, "Error: invalid preview_chars")
    try:
        notes = _migrate(_load_notes())
        if not notes:
            legacy = "No notes saved"
            result = {"notes": [], "count": 0, "tag_filter": tag or None}
            return _notes_success("note_list", result, response_format, trace_enabled, started, started_at, inputs_received, legacy, "list", "PARTIAL")
        items = []
        structured = []
        for title, n in notes.items():
            if tag:
                ntags = [t.lower() for t in n.get("tags", [])]
                if tag.lower() not in ntags:
                    continue
            preview = _format_preview(n["content"], preview_chars)
            ts = n.get("updated", "")
            structured.append(
                {
                    "title": title,
                    "preview": preview,
                    "updated": ts,
                    "tags": list(n.get("tags", [])),
                }
            )
            line = f"- {title}"
            if tag:
                line += f"  → {preview}"
            else:
                line += f"  ({ts})" if ts else ""
            items.append(line)
        if tag and not items:
            legacy = f"No notes with tag '{tag}'"
            result = {"notes": [], "count": 0, "tag_filter": tag}
            return _notes_success("note_list", result, response_format, trace_enabled, started, started_at, inputs_received, legacy, "list", "PARTIAL")
        legacy = "\n".join(items)
        result = {"notes": structured, "count": len(structured), "tag_filter": tag or None, "total_notes": len(notes)}
        return _notes_success("note_list", result, response_format, trace_enabled, started, started_at, inputs_received, legacy, "list")
    except (json.JSONDecodeError, OSError):
        return _storage_error("note_list", response_format, trace_enabled, started, started_at, inputs_received, "Error listing notes: storage unavailable")


@tool(
    name="note_search",
    description="Search notes by keyword in title or content. Shows matching content previews",
    examples=[
        "find notes about python",
        "search my notes for wifi password",
        "search for meeting",
    ],
    param_descriptions={
        "keyword": "Search term to match in title or content",
        "preview_chars": "Preview length for matched content, from 20 to 300",
        "limit": "Maximum number of matches to return, from 1 to 50",
        "response_format": "legacy or structured. Default legacy preserves existing output",
        "trace_enabled": "When true, emit a machine-readable trace entry",
    },
)
def note_search(
    keyword: str,
    preview_chars: int = 100,
    limit: int = 20,
    response_format: str = "legacy",
    trace_enabled: bool = False,
):
    started = time.perf_counter()
    started_at = utc_now_iso()
    inputs_received = 5
    response_format = normalize_response_format(response_format)
    trace_enabled = coerce_bool(trace_enabled)
    keyword = str(keyword or "").strip()
    if not keyword:
        error = error_payload(
            "EMPTY_KEYWORD",
            "keyword must not be empty.",
            "keyword",
            keyword,
            "non-empty search keyword",
            False,
            "Pass text to search for in note titles and content.",
        )
        return _notes_error("note_search", error, response_format, trace_enabled, started, started_at, inputs_received, "Error: keyword is required")
    preview_chars, preview_error = normalize_int(
        preview_chars,
        "preview_chars",
        100,
        20,
        300,
        "Use preview_chars between 20 and 300.",
        "INVALID_PREVIEW_CHARS",
    )
    if preview_error is not None:
        return _notes_error("note_search", preview_error, response_format, trace_enabled, started, started_at, inputs_received, "Error: invalid preview_chars")
    limit, limit_error = normalize_int(
        limit,
        "limit",
        20,
        1,
        50,
        "Use limit between 1 and 50.",
        "INVALID_LIMIT",
    )
    if limit_error is not None:
        return _notes_error("note_search", limit_error, response_format, trace_enabled, started, started_at, inputs_received, "Error: invalid limit")
    try:
        notes = _migrate(_load_notes())
        if not notes:
            legacy = "No notes saved"
            result = {"keyword": keyword, "matches": [], "count": 0, "truncated": False}
            return _notes_success("note_search", result, response_format, trace_enabled, started, started_at, inputs_received, legacy, "search", "PARTIAL")
        kw = keyword.lower()
        matches = []
        structured = []
        for title, n in notes.items():
            if kw in title.lower() or kw in n["content"].lower():
                preview = _format_preview(n["content"], preview_chars)
                ts = n.get("updated", "")
                structured.append({"title": title, "preview": preview, "updated": ts, "tags": list(n.get("tags", []))})
                line = f'- {title}: "{preview}"'
                if ts:
                    line += f"  ({ts})"
                matches.append(line)
        total_matches = len(matches)
        truncated = total_matches > limit
        if truncated:
            matches = matches[:limit]
            structured = structured[:limit]
        if not matches:
            legacy = f"No notes matching '{keyword}'"
            result = {"keyword": keyword, "matches": [], "count": 0, "truncated": False, "total_matches": 0}
            return _notes_success("note_search", result, response_format, trace_enabled, started, started_at, inputs_received, legacy, "search", "PARTIAL")
        legacy = f"Results for '{keyword}' ({total_matches}):\n" + "\n".join(matches)
        if truncated:
            legacy += f"\n...[showing {limit} of {total_matches}]"
        result = {
            "keyword": keyword,
            "matches": structured,
            "count": len(structured),
            "truncated": truncated,
            "total_matches": total_matches,
        }
        return _notes_success("note_search", result, response_format, trace_enabled, started, started_at, inputs_received, legacy, "search")
    except (json.JSONDecodeError, OSError):
        return _storage_error("note_search", response_format, trace_enabled, started, started_at, inputs_received, "Error searching notes: storage unavailable")
