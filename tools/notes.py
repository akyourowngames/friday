import json
from datetime import datetime
from pathlib import Path

from tools.registry import tool

NOTES_FILE = Path(__file__).resolve().parent.parent / "storage" / "notes.json"


def _load_notes():
    if NOTES_FILE.exists():
        return json.loads(NOTES_FILE.read_text(encoding="utf-8"))
    return {}


def _save_notes(notes):
    NOTES_FILE.parent.mkdir(parents=True, exist_ok=True)
    NOTES_FILE.write_text(json.dumps(notes, indent=2, ensure_ascii=False), encoding="utf-8")


def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M")


@tool(
    name="note_save",
    description="Save a task or project note with a title and body (auto-timestamped)",
    examples=[
        "save a note about project ideas",
        "take a note: meeting at 3pm",
        "note down the api endpoints",
    ],
)
def note_save(title: str, content: str, tags: str = "") -> str:
    notes = _load_notes()
    now = _now()
    entry = f"{content}\n\n_Saved: {now}"
    if tags:
        tag_list = [t.strip() for t in tags.split(",") if t.strip()]
        entry += f"\n_Tags: {', '.join(tag_list)}"
    notes[title] = entry
    _save_notes(notes)
    return f"Saved note '{title}'"


@tool(
    name="note_read",
    description="Read a note by title (exact or partial match)",
    examples=[
        "show me my note about project ideas",
        "what did I write about python tips",
        "read my note about weekend plans",
    ],
)
def note_read(title: str) -> str:
    notes = _load_notes()
    if title in notes:
        return notes[title]
    for k in notes:
        if title.lower() in k.lower():
            return f"[{k}]\n{notes[k]}"
    if not notes:
        return "No notes saved"
    return "Not found. Notes:\n" + "\n".join(f"- {t}" for t in notes)


@tool(
    name="note_list",
    description="List all saved notes with timestamps",
    examples=[
        "list my notes",
        "what notes do I have",
        "show all notes",
    ],
)
def note_list() -> str:
    notes = _load_notes()
    if not notes:
        return "No notes saved"
    lines = []
    for title, content in notes.items():
        ts = content.rsplit("_Saved: ", 1)[-1].split("\n")[0].rstrip("_") if "_Saved: " in content else ""
        lines.append(f"- {title}" + (f"  ({ts})" if ts else ""))
    return "\n".join(lines)


@tool(
    name="note_search",
    description="Search notes by keyword in title or content",
    examples=[
        "find notes about python",
        "search my notes for wifi password",
        "search for meeting",
    ],
)
def note_search(keyword: str) -> str:
    notes = _load_notes()
    if not notes:
        return "No notes saved"
    matches = []
    for title, content in notes.items():
        if keyword.lower() in title.lower() or keyword.lower() in content.lower():
            ts = content.rsplit("_Saved: ", 1)[-1].split("\n")[0].rstrip("_") if "_Saved: " in content else ""
            line = f"- {title}" + (f"  ({ts})" if ts else "")
            matches.append(line)
    if not matches:
        return f"No notes matching '{keyword}'"
    return f"Results for '{keyword}':\n" + "\n".join(matches)
