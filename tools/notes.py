import json
from datetime import datetime
from pathlib import Path

from tools.registry import tool

NOTES_FILE = Path(__file__).resolve().parent.parent / "storage" / "notes.json"


def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def _load_notes():
    if NOTES_FILE.exists():
        return json.loads(NOTES_FILE.read_text(encoding="utf-8"))
    return {}


def _save_notes(notes):
    NOTES_FILE.parent.mkdir(parents=True, exist_ok=True)
    NOTES_FILE.write_text(json.dumps(notes, indent=2, ensure_ascii=False), encoding="utf-8")


def _migrate(notes):
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
    },
)
def note_save(title: str, content: str, tags: str = "") -> str:
    notes = _load_notes()
    notes = _migrate(notes)
    now = _now()
    tag_list = [t.strip() for t in tags.split(",") if t.strip()] if tags else []
    is_new = title not in notes
    notes[title] = {
        "content": content,
        "created": notes[title]["created"] if not is_new else now,
        "updated": now,
        "tags": tag_list if tag_list else (notes[title].get("tags", []) if not is_new else []),
    }
    _save_notes(notes)
    return f"{'Updated' if not is_new else 'Saved'} note '{title}'"


@tool(
    name="note_read",
    description="Read a note by title (exact match preferred, falls back to partial match). Shows content, tags, and timestamps",
    examples=[
        "show me my note about project ideas",
        "what did I write about python tips",
        "read my note about weekend plans",
    ],
)
def note_read(title: str) -> str:
    notes = _load_notes()
    notes = _migrate(notes)
    if title in notes:
        n = notes[title]
        parts = [n["content"]]
        if n.get("tags"):
            parts.append(f"\nTags: {', '.join(n['tags'])}")
        parts.append(f"\nCreated: {n.get('created', '?')}  |  Updated: {n.get('updated', '?')}")
        return "".join(parts)
    for k in notes:
        if title.lower() in k.lower():
            n = notes[k]
            parts = [f"[{k}]\n{n['content']}"]
            if n.get("tags"):
                parts.append(f"\nTags: {', '.join(n['tags'])}")
            parts.append(f"\nCreated: {n.get('created', '?')}  |  Updated: {n.get('updated', '?')}")
            return "".join(parts)
    if not notes:
        return "No notes saved"
    return f"Note '{title}' not found. Titles:\n" + "\n".join(f"- {t}" for t in notes)


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
    },
)
def note_update(title: str, content: str = "", tags: str = "") -> str:
    notes = _load_notes()
    notes = _migrate(notes)
    if title not in notes:
        for k in notes:
            if title.lower() in k.lower():
                title = k
                break
        else:
            return f"Note '{title}' not found"
    n = notes[title]
    if content:
        n["content"] = content
    if tags:
        n["tags"] = [t.strip() for t in tags.split(",") if t.strip()]
    n["updated"] = _now()
    _save_notes(notes)
    return f"Updated note '{title}'"


@tool(
    name="note_delete",
    description="Delete a note by exact or partial title match",
    examples=[
        "delete my note about project ideas",
        "remove note about meeting",
        "delete note python tips",
    ],
)
def note_delete(title: str) -> str:
    notes = _load_notes()
    notes = _migrate(notes)
    if title in notes:
        del notes[title]
        _save_notes(notes)
        return f"Deleted note '{title}'"
    for k in list(notes):
        if title.lower() in k.lower():
            del notes[k]
            _save_notes(notes)
            return f"Deleted note '{k}'"
    if not notes:
        return "No notes saved"
    return f"Note '{title}' not found. Titles:\n" + "\n".join(f"- {t}" for t in notes)


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
    },
)
def note_list(tag: str = "") -> str:
    notes = _load_notes()
    notes = _migrate(notes)
    if not notes:
        return "No notes saved"
    items = []
    for title, n in notes.items():
        if tag:
            ntags = [t.lower() for t in n.get("tags", [])]
            if tag.lower() not in ntags:
                continue
        preview = _format_preview(n["content"], 60)
        line = f"- {title}"
        if tag:
            line += f"  → {preview}"
        else:
            ts = n.get("updated", "")
            line += f"  ({ts})" if ts else ""
        items.append(line)
    if tag and not items:
        return f"No notes with tag '{tag}'"
    return "\n".join(items)


@tool(
    name="note_search",
    description="Search notes by keyword in title or content. Shows matching content previews",
    examples=[
        "find notes about python",
        "search my notes for wifi password",
        "search for meeting",
    ],
)
def note_search(keyword: str) -> str:
    notes = _load_notes()
    notes = _migrate(notes)
    if not notes:
        return "No notes saved"
    matches = []
    for title, n in notes.items():
        kw = keyword.lower()
        if kw in title.lower() or kw in n["content"].lower():
            preview = _format_preview(n["content"], 100)
            ts = n.get("updated", "")
            line = f"- {title}: \"{preview}\""
            if ts:
                line += f"  ({ts})"
            matches.append(line)
    if not matches:
        return f"No notes matching '{keyword}'"
    return f"Results for '{keyword}' ({len(matches)}):\n" + "\n".join(matches)
