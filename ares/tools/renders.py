"""Rich renderers for tool results."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from typing import Any

from rich import box
from rich.console import Group
from rich.panel import Panel
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text
from rich.tree import Tree


def _clip(text: str, limit: int = 180) -> str:
    """Collapse whitespace and keep long cells readable."""
    clean = re.sub(r"\s+", " ", str(text or "")).strip()
    if len(clean) <= limit:
        return clean
    return clean[: limit - 3].rstrip() + "..."


def _status_style(value: str | int | None) -> str:
    text = str(value or "").lower()
    if text in {"0", "ok", "pass", "passed", "true", "success", "enabled"}:
        return "green"
    if text in {"fail", "failed", "false", "error", "disabled"} or text.startswith("error"):
        return "red"
    return "yellow" if "dry" in text or "warn" in text else "default"


def _simple_table(title: str, *columns: str, border_style: str = "dim") -> Table:
    table = Table(
        title=title,
        box=box.SIMPLE_HEAVY,
        show_lines=False,
        header_style="bold",
        border_style=border_style,
        expand=False,
    )
    for column in columns:
        table.add_column(column)
    return table


def _key_value_table(title: str, rows: list[tuple[str, Any]], border_style: str = "dim") -> Any:
    table = _simple_table(title, "Field", "Value", border_style=border_style)
    table.columns[0].style = "cyan"
    table.columns[0].no_wrap = True
    table.columns[1].overflow = "fold"
    for key, value in rows:
        table.add_row(str(key), str(value))
    return Panel(table, title=title, border_style=border_style, padding=(0, 1))


def _parse_key_value_lines(content: str) -> list[tuple[str, str]]:
    rows: list[tuple[str, str]] = []
    for raw in content.splitlines():
        line = raw.strip()
        if not line or line.startswith("---"):
            continue
        match = re.match(r"^([A-Za-z][A-Za-z0-9 _/-]{1,40}):\s*(.+)$", line)
        if match:
            rows.append((match.group(1).strip(), match.group(2).strip()))
    return rows


def render_web_search(content: str) -> Any:
    """Render structured web search results as summary plus a result table."""
    try:
        payload = json.loads(content)
    except (TypeError, json.JSONDecodeError):
        return Panel(Text(content), title="Web Search", border_style="bright_green")

    if isinstance(payload, list):
        payload = {"query": "", "provider": "web", "summary": "", "results": payload, "errors": []}

    results = payload.get("results", [])
    query = payload.get("query", "")
    provider = payload.get("provider", "")
    summary = payload.get("summary", "")
    errors = payload.get("errors", [])

    title_parts = ["Web Search"]
    if provider:
        title_parts.append(provider)
    if query:
        title_parts.append(f'"{query}"')

    renderables: list[Any] = []
    if summary and summary != "No summary available.":
        renderables.append(Text("Summary", style="bold bright_green"))
        renderables.append(Text(summary, style="default"))
        renderables.append(Text())

    if not results:
        renderables.append(Text("No results found.", style="dim"))
    else:
        table = _simple_table("Results", "#", "Title", "Source", "Snippet", border_style="bright_green")
        table.columns[0].justify = "right"
        table.columns[0].style = "bold cyan"
        table.columns[0].width = 3
        table.columns[1].ratio = 3
        table.columns[2].ratio = 3
        table.columns[3].ratio = 5
        for index, result in enumerate(results, 1):
            title = str(result.get("title") or "Untitled")
            url = str(result.get("url") or "")
            snippet = _clip(result.get("snippet") or "", 220)
            table.add_row(str(index), Text(_clip(title, 120), style="bold"), Text(_clip(url, 100), style="dim"), snippet)
        renderables.append(table)

    if errors:
        renderables.append(Text())
        renderables.append(Text("Warnings: " + "; ".join(str(e) for e in errors[:3]), style="yellow"))

    return Panel(
        Group(*renderables),
        title=" | ".join(title_parts),
        border_style="bright_green",
        padding=(0, 1),
    )


def _language_for_path(path: str) -> str:
    suffix = path.lower().rsplit(".", 1)[-1] if "." in path else ""
    return {
        "py": "python",
        "js": "javascript",
        "ts": "typescript",
        "tsx": "tsx",
        "jsx": "jsx",
        "rs": "rust",
        "go": "go",
        "java": "java",
        "c": "c",
        "h": "c",
        "cpp": "cpp",
        "hpp": "cpp",
        "rb": "ruby",
        "sh": "bash",
        "bash": "bash",
        "json": "json",
        "yaml": "yaml",
        "yml": "yaml",
        "toml": "toml",
        "md": "markdown",
        "html": "html",
        "css": "css",
        "sql": "sql",
        "xml": "xml",
    }.get(suffix, "text")


def render_file_content(content: str) -> Any:
    """Render read_file output as syntax highlighted content."""
    if "binary file" in content.lower() or "cannot display" in content.lower():
        return Panel(Text(content, style="yellow"), title="File", border_style="yellow")

    header_match = re.search(r"\[File: (.+?) \((\d+) lines total\)\]", content)
    title = "File Content"
    lang = "text"
    if header_match:
        title = f"{header_match.group(1)} ({header_match.group(2)} lines)"
        lang = _language_for_path(header_match.group(1))

    code_lines = []
    notes = []
    for line in content.splitlines():
        if line.startswith("[File:"):
            continue
        if line.startswith("(") and line.endswith(")"):
            notes.append(line)
            continue
        match = re.match(r"^\s*\d+\t(.*)$", line)
        if match:
            code_lines.append(match.group(1))
        elif line.strip():
            code_lines.append(line)

    if not code_lines:
        return Panel(Text(content), title=title, border_style="bright_blue")

    shown_code = "\n".join(code_lines)
    syntax = Syntax(shown_code, lang, theme="monokai", line_numbers=True, word_wrap=True)
    renderables: list[Any] = [syntax]
    if notes:
        renderables.append(Text("  " + " | ".join(notes), style="dim"))

    return Panel(Group(*renderables), title=title, border_style="bright_blue", padding=(0, 1))


def render_search_results(content: str) -> Any:
    """Render search_files output with match type badges."""
    if "no results" in content.lower() or "found 0" in content.lower():
        return Panel(Text("No matching files found.", style="dim"), title="File Search", border_style="bright_yellow")

    table = Table(show_header=True, header_style="bold", border_style="bright_yellow")
    table.add_column("Type", width=10)
    table.add_column("Location", ratio=3)
    table.add_column("Excerpt", ratio=5)

    lines = content.splitlines()
    pending: tuple[str, str] | None = None
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("[content match]"):
            pending = ("content", stripped.removeprefix("[content match]").strip())
        elif stripped.startswith("[name match]"):
            pending = ("name", stripped.removeprefix("[name match]").strip())
            table.add_row(Text("name", style="bold magenta"), Text(pending[1], style="bold"), "")
            pending = None
        elif pending and stripped and not stripped.startswith("Found "):
            match_type, location = pending
            style = "cyan" if match_type == "content" else "magenta"
            table.add_row(Text(match_type, style=f"bold {style}"), Text(location, style="bold"), Text(stripped, style="dim"))
            pending = None

    if table.row_count == 0:
        return Panel(Text(content), title="File Search", border_style="bright_yellow")
    return Panel(table, title="File Search Results", border_style="bright_yellow", padding=(0, 1))


def render_directory(content: str) -> Any:
    """Render list_directory output as a tree."""
    lines = [line.strip() for line in content.splitlines() if line.strip()]
    title = "Directory"
    if lines and lines[0].startswith("[Directory:"):
        title = lines[0].removeprefix("[Directory:").removesuffix("]").strip()
        lines = lines[1:]

    tree = Tree(title, guide_style="dim")
    added = 0
    footer = ""
    for line in lines:
        if line.startswith("..."):
            footer = line
            continue
        if line.startswith("[dir]"):
            tree.add(Text(line.removeprefix("[dir]").strip(), style="bold cyan"))
            added += 1
        elif line.startswith("[file]"):
            tree.add(Text(line.removeprefix("[file]").strip(), style="default"))
            added += 1

    if footer:
        tree.add(Text(footer, style="dim"))
    if added == 0 and not footer:
        tree.add(Text("Empty directory.", style="dim"))
    return Panel(tree, title=title, border_style="bright_magenta", padding=(0, 1))


def render_memory_result(content: str) -> Any:
    """Render memory tool output as a small action or search table."""
    clean = content.strip()
    if not clean:
        return Panel(Text("No memory result.", style="dim"), title="Memory", border_style="green")

    try:
        payload = json.loads(clean)
    except (TypeError, json.JSONDecodeError):
        payload = None
    if isinstance(payload, dict) and "results" in payload:
        if not payload.get("ok", False):
            return render_json_status(clean, title="Memory", border_style="green")
        records = payload.get("results") or []
        if not records:
            return Panel(Text("No local recall records found.", style="dim"), title="Memory Search", border_style="green")
        table = _simple_table("Local Recall", "Source", "Source ID", "Role / Details", "Stored content", border_style="green")
        table.columns[0].style = "cyan"
        table.columns[1].style = "dim"
        table.columns[3].ratio = 5
        for record in records:
            if not isinstance(record, dict):
                continue
            source = str(record.get("source") or "memory")
            details = str(record.get("role") or record.get("category") or "")
            stored = str(record.get("content") or "")
            if source == "person" and isinstance(record.get("person"), dict):
                person = record["person"]
                details = "; ".join(part for part in [
                    str(person.get("relation") or ""),
                    f"phone={person.get('phone')}" if person.get("phone") else "",
                    f"email={person.get('email')}" if person.get("email") else "",
                    f"dates={person.get('important_dates')}" if person.get("important_dates") else "",
                    f"notes={person.get('notes')}" if person.get("notes") else "",
                ] if part)
            table.add_row(
                source,
                str(record.get("source_id") or "-"),
                _clip(details or "-", 320),
                _clip(stored or "-", 600),
            )
        if table.row_count == 0:
            return Panel(Text("No local recall records found.", style="dim"), title="Memory Search", border_style="green")
        return Panel(table, title="Memory Search", border_style="green", padding=(0, 1))

    rows = []
    for line in clean.splitlines():
        match = re.match(r"^-\s+#(\d+)\s+\[([^,\]]+)(?:,\s*importance=([^\]]+))?\]\s+(.+)$", line.strip())
        if match:
            rows.append(match.groups(default=""))

    if rows:
        table = _simple_table("Memories", "ID", "Category", "Importance", "Memory", border_style="green")
        table.columns[0].style = "dim"
        table.columns[1].style = "cyan"
        table.columns[3].ratio = 4
        for fact_id, category, importance, fact in rows:
            table.add_row(fact_id, category, importance or "-", fact)
        return Panel(table, title="Memory Search", border_style="green", padding=(0, 1))

    action_match = re.match(r"^(Stored|Updated) memory #(\d+):\s*(.+)$", clean)
    if action_match:
        action, fact_id, fact = action_match.groups()
        table = _simple_table("Memory", "Action", "ID", "Fact", border_style="green")
        table.columns[0].style = "green"
        table.columns[1].style = "dim"
        table.columns[2].ratio = 4
        table.add_row(action, fact_id, fact)
        return Panel(table, title="Memory", border_style="green", padding=(0, 1))

    forgotten = re.match(r"^Forgot memory #(\d+)\.", clean)
    if forgotten:
        return _key_value_table("Memory", [("Action", "Forgot"), ("ID", forgotten.group(1))], border_style="yellow")

    return Panel(Text(clean), title="Memory", border_style="green", padding=(0, 1))


def render_people_result(content: str) -> Any:
    """Render complete local people records."""
    try:
        payload = json.loads(content)
    except (TypeError, json.JSONDecodeError):
        return Panel(Text(content), title="People", border_style="bright_magenta", padding=(0, 1))
    if not isinstance(payload, dict):
        return render_json_status(content, title="People", border_style="bright_magenta")
    if payload.get("confirm_required") or not payload.get("ok", False):
        return render_json_status(content, title="People", border_style="bright_magenta")
    people = payload.get("people")
    if people is None and isinstance(payload.get("person"), dict):
        people = [payload["person"]]
    if not isinstance(people, list):
        return render_json_status(content, title="People", border_style="bright_magenta")
    table = _simple_table("People", "ID", "Name", "Relation", "Aliases", "Phone", "Email", "Dates", "Notes", border_style="bright_magenta")
    table.columns[0].style = "dim"
    table.columns[1].style = "cyan"
    table.columns[4].ratio = 2
    table.columns[7].ratio = 3
    for person in people:
        if not isinstance(person, dict):
            continue
        table.add_row(
            str(person.get("person_id", "?")),
            _clip(person.get("canonical_name", ""), 80),
            _clip(person.get("relation", "") or "-", 60),
            _clip(", ".join(str(alias) for alias in person.get("aliases", [])[:4]) or "-", 100),
            _clip(person.get("phone", "") or "-", 80),
            _clip(person.get("email", "") or "-", 120),
            _clip(json.dumps(person.get("important_dates") or {}, ensure_ascii=False) if person.get("important_dates") else "-", 160),
            _clip(person.get("notes", "") or "-", 300),
        )
    if table.row_count == 0:
        return Panel(Text("No matching people found.", style="dim"), title="People", border_style="bright_magenta")
    return Panel(table, title="People", border_style="bright_magenta", padding=(0, 1))


def render_actions_result(content: str) -> Any:
    """Render the privacy-minimized Action Ledger as a concise provenance table."""
    try:
        payload = json.loads(content)
    except (TypeError, json.JSONDecodeError):
        return Panel(Text(content), title="Action History", border_style="bright_cyan", padding=(0, 1))
    if not isinstance(payload, dict) or not payload.get("ok", False):
        return render_json_status(content, title="Action History", border_style="bright_cyan")
    actions = payload.get("actions", [])
    table = _simple_table("Action History", "When", "Action", "Summary", "Target", border_style="bright_cyan")
    table.columns[0].style = "dim"
    table.columns[1].style = "cyan"
    table.columns[2].ratio = 3
    table.columns[3].ratio = 2
    for action in actions if isinstance(actions, list) else []:
        if not isinstance(action, dict):
            continue
        table.add_row(
            _clip(action.get("created_at", ""), 32),
            _clip(action.get("action_type", ""), 40),
            _clip(action.get("summary", ""), 180),
            _clip(action.get("target", "") or "-", 120),
        )
    if table.row_count == 0:
        return Panel(Text("No matching action records found.", style="dim"), title="Action History", border_style="bright_cyan")
    return Panel(table, title="Action History", border_style="bright_cyan", padding=(0, 1))


def render_skills_result(content: str) -> Any:
    """Render list_skills output as skill and category tables."""
    clean = content.strip()
    if not clean or clean.lower().startswith("no matching"):
        return Panel(Text(clean or "No matching skills found.", style="dim"), title="Skills", border_style="magenta")

    skills: list[tuple[str, str, str]] = []
    categories = ""
    for raw in clean.splitlines():
        line = raw.strip()
        match = re.match(r"^-\s+(.+?)(?:\s+\[([^\]]+)\])?:\s*(.+)$", line)
        if match:
            name, category, description = match.groups(default="")
            skills.append((name, category or "general", description))
        elif line.startswith("Categories:"):
            categories = line.removeprefix("Categories:").strip()

    if not skills:
        return Panel(Text(clean), title="Skills", border_style="magenta", padding=(0, 1))

    table = _simple_table("Available Skills", "Name", "Category", "Description", border_style="magenta")
    table.columns[0].style = "cyan"
    table.columns[0].no_wrap = True
    table.columns[1].style = "magenta"
    table.columns[2].ratio = 4
    for name, category, description in skills:
        table.add_row(name, category, description)

    renderables: list[Any] = [table]
    if categories:
        cat_table = _simple_table("Categories", "Category", "Count", border_style="magenta")
        cat_table.columns[0].style = "cyan"
        for item in categories.split(","):
            match = re.match(r"\s*(.+?)\s+\((\d+)\)\s*$", item)
            if match:
                cat_table.add_row(match.group(1), match.group(2))
        if cat_table.row_count:
            renderables.append(cat_table)

    return Panel(Group(*renderables), title="Skills", border_style="magenta", padding=(0, 1))


def render_command_result(content: str) -> Any:
    """Render shell and code execution output with status and output sections."""
    clean = content.strip()
    if not clean:
        return Panel(Text("(No output)", style="dim"), title="Command", border_style="cyan")
    if clean.lower().startswith("error:"):
        return Panel(Text(clean, style="red"), title="Command Failed", border_style="red")

    exit_code = None
    summary: dict[str, str] = {}
    remaining_lines: list[str] = []
    for line in clean.splitlines():
        if line.startswith("Exit code:"):
            exit_code = line.split(":", 1)[1].strip()
        elif line.startswith("Summary:"):
            for item in line.removeprefix("Summary:").split(";"):
                if "=" in item:
                    key, value = item.split("=", 1)
                    summary[key.strip()] = value.strip()
        else:
            remaining_lines.append(line)

    meta = _simple_table("Status", "Field", "Value", border_style="cyan")
    meta.columns[0].style = "cyan"
    meta.columns[0].no_wrap = True
    if exit_code is not None:
        meta.add_row("Exit code", Text(exit_code, style=_status_style(exit_code)))
    for key, value in summary.items():
        meta.add_row(key.replace("_", " ").title(), Text(value, style=_status_style(value)))

    sections: list[Any] = []
    text = "\n".join(remaining_lines).strip()
    if text and text != "(No output)":
        parts = re.split(r"^--- (stdout|stderr) ---$", text, flags=re.MULTILINE)
        if len(parts) > 1:
            for index in range(1, len(parts), 2):
                name = parts[index]
                body = parts[index + 1].strip()
                style = "red" if name == "stderr" else "default"
                sections.append(Text(name.upper(), style=f"bold {style}"))
                sections.append(Syntax(body, "text", theme="monokai", word_wrap=True))
        else:
            sections.append(Syntax(text, "text", theme="monokai", word_wrap=True))
    else:
        sections.append(Text("(No output)", style="dim"))

    return Panel(Group(meta, *sections), title="Command Result", border_style="cyan", padding=(0, 1))


def render_json_status(content: str, title: str = "Status", border_style: str = "bright_cyan") -> Any:
    """Render compact JSON objects as key/value tables."""
    clean = content.strip()
    try:
        payload = json.loads(clean)
    except (TypeError, json.JSONDecodeError):
        rows = _parse_key_value_lines(clean)
        return _key_value_table(title, rows, border_style=border_style) if rows else Panel(Text(clean), title=title, border_style=border_style)

    if isinstance(payload, dict) and ("kdeconnect" in payload or "adb" in payload):
        table = _simple_table("Phone Bridge", "Bridge", "Status", "Details", border_style=border_style)
        table.columns[0].style = "cyan"
        table.columns[2].style = "dim"
        for name, data in (("KDE Connect", payload.get("kdeconnect", {})), ("ADB", payload.get("adb", {}))):
            if not isinstance(data, dict):
                continue
            ok = bool(data.get("ok"))
            details = data.get("device_id") or data.get("error") or ", ".join(data.get("devices") or []) or "ready"
            battery = data.get("battery") or {}
            if isinstance(battery, dict) and battery.get("level"):
                details = f"{details}; battery {battery['level']}%"
            table.add_row(name, Text("PASS" if ok else "FAIL", style="green" if ok else "red"), str(details))
        return Panel(table, title=title, border_style=border_style, padding=(0, 1))

    rows: list[tuple[str, Any]] = []
    if isinstance(payload, dict):
        rows = [(key, json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else value) for key, value in payload.items()]
    elif isinstance(payload, list):
        rows = [(str(index), json.dumps(value, ensure_ascii=False)) for index, value in enumerate(payload, 1)]
    else:
        rows = [("Value", payload)]
    return _key_value_table(title, rows, border_style=border_style)


def render_file_operation(content: str) -> Any:
    """Render file operation status, diffs, and confirmations."""
    clean = content.strip()
    if not clean:
        return Panel(Text("No file operation output.", style="dim"), title="File Operation", border_style="bright_blue")

    diff_match = re.search(r"(?m)^(--- |\+\+\+ |@@ )", clean)
    if diff_match:
        header = clean[: diff_match.start()].strip()
        diff = clean[diff_match.start():].strip()
        renderables: list[Any] = []
        if header:
            renderables.append(Text(header, style="bold"))
        renderables.append(Syntax(diff, "diff", theme="monokai", word_wrap=True))
        return Panel(Group(*renderables), title="File Diff", border_style="bright_blue", padding=(0, 1))

    rows = _parse_key_value_lines(clean)
    if rows:
        return _key_value_table("File Operation", rows, border_style="bright_blue")
    return Panel(Text(clean), title="File Operation", border_style="bright_blue", padding=(0, 1))


def render_image_result(content: str) -> Any:
    """Render image generation and editing output."""
    clean = content.strip()
    if clean.lower().startswith("error"):
        return Panel(Text(clean, style="red"), title="Image", border_style="red")

    rows = _parse_key_value_lines(clean)
    saved = re.search(r"(?:saved to|Saved to)\s+(.+?)(?:\n|$)", clean)
    if saved and not any(key.lower() == "saved to" for key, _ in rows):
        rows.insert(0, ("Saved to", saved.group(1).strip()))
    manifest = re.search(r"^Manifest:\s*(.+)$", clean, re.MULTILINE)
    if manifest:
        rows.append(("Manifest", manifest.group(1).strip()))

    if rows:
        return _key_value_table("Image", rows, border_style="bright_magenta")
    return Panel(Text(clean), title="Image", border_style="bright_magenta", padding=(0, 1))


def render_inspection_result(content: str) -> Any:
    """Render filesystem inspection summaries as tables when possible."""
    clean = content.strip()
    if not clean:
        return Panel(Text("No result.", style="dim"), title="Inspection", border_style="bright_yellow")

    rows = _parse_key_value_lines(clean)
    if rows and len(rows) >= 2:
        return _key_value_table("Inspection", rows, border_style="bright_yellow")

    lines = [line for line in clean.splitlines() if line.strip()]
    table = _simple_table("Results", "#", "Item", border_style="bright_yellow")
    table.columns[0].style = "dim"
    table.columns[0].justify = "right"
    table.columns[1].ratio = 5
    for index, line in enumerate(lines[:80], 1):
        table.add_row(str(index), line)
    return Panel(table, title="Inspection", border_style="bright_yellow", padding=(0, 1))


def render_generic_tool(content: str) -> Any:
    """Render generic tool results compactly."""
    clean = content.strip()
    if not clean:
        return Text("  *  No result", style="dim")
    if "\n" not in clean and len(clean) < 120:
        return Text(f"  *  {clean}", style="dim")
    return Panel(Text(clean), title="Tool Result", border_style="dim", padding=(0, 1))


RENDERERS: dict[str, Callable[[str], Any]] = {
    "web_search": render_web_search,
    "read_file": render_file_content,
    "show_file_with_line_numbers": render_file_content,
    "head_file": render_file_content,
    "tail_file": render_file_content,
    "search_files": render_search_results,
    "find_text": render_search_results,
    "list_directory": render_directory,
    "file_tree": render_directory,
    "store_memory": render_memory_result,
    "search_memory": render_memory_result,
    "update_memory": render_memory_result,
    "delete_memory": render_memory_result,
    "remember_person": render_people_result,
    "search_person": render_people_result,
    "update_person": render_people_result,
    "forget_person": render_people_result,
    "search_actions": render_actions_result,
    "list_skills": render_skills_result,
    "run_code": render_command_result,
    "run_command": render_command_result,
    "terminal_exec": render_command_result,
    "phone_status": render_json_status,
    "phone_get_notifications": lambda content: render_json_status(content, title="Phone Notifications"),
    "phone_search_contact": lambda content: render_json_status(content, title="Phone Contacts"),
    "phone_send_sms": lambda content: render_json_status(content, title="Phone SMS"),
    "phone_call_number": lambda content: render_json_status(content, title="Phone Call"),
    "phone_launch_app": lambda content: render_json_status(content, title="Phone App"),
    "phone_open_url": lambda content: render_json_status(content, title="Phone URL"),
    "write_file": render_file_operation,
    "edit_file": render_file_operation,
    "create_directory": render_file_operation,
    "delete_file": render_file_operation,
    "move_file": render_file_operation,
    "batch_edit": render_file_operation,
    "glob_apply": render_file_operation,
    "insert_line": render_file_operation,
    "replace_lines": render_file_operation,
    "delete_lines": render_file_operation,
    "append_to_file": render_file_operation,
    "prepend_to_file": render_file_operation,
    "preview_diff": render_file_operation,
    "copy_file": render_file_operation,
    "backup_file": render_file_operation,
    "undo_last_edit": render_file_operation,
    "batch_file_ops": render_file_operation,
    "create_file_from_template": render_file_operation,
    "get_file_info": render_inspection_result,
    "disk_usage": render_inspection_result,
    "checksum": render_inspection_result,
    "find_duplicates": render_inspection_result,
    "count_lines": render_inspection_result,
    "safe_path_status": render_inspection_result,
    "generate_image": render_image_result,
    "image_info": render_image_result,
    "resize_image": render_image_result,
    "convert_image": render_image_result,
    "crop_image": render_image_result,
    "get_current_datetime": lambda content: render_json_status(content, title="Date And Time", border_style="cyan"),
    "update_config": lambda content: render_json_status(content, title="Config", border_style="cyan"),
    "export_data": render_file_operation,
    "create_task": lambda content: render_json_status(content, title="Workflow Task", border_style="bright_blue"),
    "list_tasks": lambda content: render_json_status(content, title="Workflow Tasks", border_style="bright_blue"),
    "get_task_status": lambda content: render_json_status(content, title="Workflow Task", border_style="bright_blue"),
    "update_task": lambda content: render_json_status(content, title="Workflow Task", border_style="bright_blue"),
    "cancel_task": lambda content: render_json_status(content, title="Workflow Task", border_style="bright_blue"),
    "run_task": lambda content: render_json_status(content, title="Workflow Run", border_style="bright_blue"),
}
DEFAULT_RENDERER = render_generic_tool


def get_renderer(tool_name: str) -> Callable[[str], Any]:
    """Return the renderer for a tool name."""
    return RENDERERS.get(tool_name, DEFAULT_RENDERER)
