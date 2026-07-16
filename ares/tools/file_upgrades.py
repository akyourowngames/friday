"""Advanced modes layered onto Ares's existing file tools.

The public tool names stay unchanged.  These helpers only run when a caller
selects an advanced mode or asks for the structured response contract.
"""

from __future__ import annotations

import ast
import codecs
import difflib
import fnmatch
import json
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ares.tools.filesystem import _is_ignored, _iter_files, resolve_path
from ares.tools.filesystem_write import _create_backup, atomic_write, resolve_write_path


MAX_ADVANCED_FILE_BYTES = 2_000_000
LANGUAGES = {
    ".py": "Python", ".js": "JavaScript", ".jsx": "JavaScript", ".ts": "TypeScript",
    ".tsx": "TypeScript", ".rs": "Rust", ".go": "Go", ".java": "Java", ".kt": "Kotlin",
    ".c": "C", ".h": "C", ".cpp": "C++", ".cs": "C#", ".rb": "Ruby", ".php": "PHP",
    ".md": "Markdown", ".json": "JSON", ".yaml": "YAML", ".yml": "YAML", ".toml": "TOML",
}


def _decode(path: Path, requested: str = "auto") -> tuple[str, str, str]:
    data = path.read_bytes()
    if len(data) > MAX_ADVANCED_FILE_BYTES:
        raise ValueError(f"File exceeds the {MAX_ADVANCED_FILE_BYTES:,}-byte advanced-read limit.")
    if requested and requested != "auto":
        return data.decode(requested), requested, "explicit"
    for marker, encoding in (
        (codecs.BOM_UTF8, "utf-8-sig"),
        (codecs.BOM_UTF16_LE, "utf-16-le"),
        (codecs.BOM_UTF16_BE, "utf-16-be"),
    ):
        if data.startswith(marker):
            return data.decode(encoding), encoding, "bom"
    try:
        return data.decode("utf-8"), "utf-8", "validated"
    except UnicodeDecodeError:
        return data.decode("cp1252"), "cp1252", "fallback"


def _parse_cursor(cursor: str | int | None) -> int:
    if cursor in (None, ""):
        return 0
    try:
        value = int(cursor)
    except (TypeError, ValueError) as exc:
        raise ValueError("cursor must be a non-negative integer offset") from exc
    if value < 0:
        raise ValueError("cursor must be a non-negative integer offset")
    return value


def _node_name(node: ast.AST, parents: list[str]) -> str:
    name = getattr(node, "name", "")
    return ".".join([*parents, str(name)]) if parents else str(name)


def _python_symbols(text: str) -> list[dict[str, Any]]:
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return []
    output: list[dict[str, Any]] = []

    def walk(nodes: list[ast.stmt], parents: list[str]) -> None:
        for node in nodes:
            if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                qualified = _node_name(node, parents)
                output.append({
                    "symbol": qualified,
                    "kind": "class" if isinstance(node, ast.ClassDef) else "function",
                    "line": int(node.lineno),
                    "end_line": int(getattr(node, "end_lineno", node.lineno)),
                })
                walk(node.body, [*parents, node.name])
    walk(tree.body, [])
    return output


def _select_symbol(text: str, symbol: str) -> tuple[int, int, str]:
    matches = [item for item in _python_symbols(text) if item["symbol"] == symbol or item["symbol"].endswith(f".{symbol}")]
    if not matches:
        raise ValueError(f"Python symbol '{symbol}' was not found.")
    if len(matches) > 1:
        raise ValueError(f"Python symbol '{symbol}' is ambiguous: {', '.join(item['symbol'] for item in matches[:8])}")
    item = matches[0]
    lines = text.splitlines(keepends=True)
    return item["line"], item["end_line"], "".join(lines[item["line"] - 1:item["end_line"]])


def _select_heading(text: str, heading: str) -> tuple[int, int, str]:
    wanted = str(heading).strip().lstrip("#").strip().casefold()
    lines = text.splitlines(keepends=True)
    start = None
    level = 0
    for index, line in enumerate(lines):
        match = re.match(r"^(#{1,6})\s+(.+?)\s*$", line)
        if match and match.group(2).strip().casefold() == wanted:
            start, level = index, len(match.group(1))
            break
    if start is None:
        raise ValueError(f"Markdown heading '{heading}' was not found.")
    end = len(lines)
    for index in range(start + 1, len(lines)):
        match = re.match(r"^(#{1,6})\s+", lines[index])
        if match and len(match.group(1)) <= level:
            end = index
            break
    return start + 1, end, "".join(lines[start:end])


def advanced_read(args: dict[str, Any]) -> dict[str, Any]:
    path = resolve_path(str(args["path"]))
    if not path.exists() or not path.is_file():
        raise FileNotFoundError(f"File not found: {args['path']}")
    text, encoding, detection = _decode(path, str(args.get("encoding") or "auto"))
    mode = str(args.get("mode") or "lines").casefold()
    lines = text.splitlines(keepends=True)
    start, end, content = 1, len(lines), text
    metadata: dict[str, Any] = {}

    if mode == "lines":
        offset = _parse_cursor(args.get("cursor")) or max(0, int(args.get("start_line", 1)) - 1)
        count = max(1, min(int(args.get("num_lines", 200)), 2_000))
        start, end = offset + 1, min(offset + count, len(lines))
        content = "".join(lines[offset:end])
    elif mode == "symbol":
        start, end, content = _select_symbol(text, str(args.get("symbol") or ""))
    elif mode == "heading":
        start, end, content = _select_heading(text, str(args.get("heading") or ""))
    elif mode == "json":
        value: Any = json.loads(text)
        selector = str(args.get("selector") or "").strip()
        if selector:
            for part in selector.split("."):
                value = value[int(part)] if isinstance(value, list) else value[part]
        content = json.dumps(value, ensure_ascii=False, indent=2)
        metadata["selector"] = selector
    elif mode == "imports":
        if path.suffix.casefold() != ".py":
            raise ValueError("imports mode currently supports Python files")
        tree = ast.parse(text)
        imports = [node for node in ast.walk(tree) if isinstance(node, (ast.Import, ast.ImportFrom))]
        ranges = [(int(node.lineno), int(getattr(node, "end_lineno", node.lineno))) for node in imports]
        content = "".join("".join(lines[line - 1:end_line]) for line, end_line in ranges)
        metadata["imports"] = [ast.get_source_segment(text, node) or "" for node in imports]
    elif mode in {"references", "tests"}:
        query = str(args.get("symbol") or path.stem)
        search_args = {
            "mode": "references" if mode == "references" else "text",
            "path": str(path.parent.parent if path.parent.name in {"src", "ares"} else path.parent),
            "query": query,
            "name_pattern": "test_*.py" if mode == "tests" else "",
            "max_results": int(args.get("max_results", 50)),
        }
        metadata["matches"] = advanced_search(search_args)["results"]
        content = ""
    else:
        raise ValueError("mode must be lines, symbol, heading, json, imports, references, or tests")

    next_cursor = str(end) if mode == "lines" and end < len(lines) else None
    return {
        "path": str(path), "mode": mode, "encoding": encoding, "encoding_detection": detection,
        "start_line": start, "end_line": end, "total_lines": len(lines), "content": content,
        "cursor": next_cursor, **metadata,
    }


def _git_changed(root: Path) -> dict[str, str]:
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), "status", "--porcelain=v1", "--untracked-files=all"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=10, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return {}
    if proc.returncode != 0:
        return {}
    output: dict[str, str] = {}
    for line in proc.stdout.splitlines():
        if len(line) < 4:
            continue
        name = line[3:].split(" -> ")[-1].strip().strip('"')
        output[str((root / name).resolve())] = line[:2].strip() or "modified"
    return output


def _date(value: Any) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed


def _tokens(value: str) -> set[str]:
    return {token.casefold() for token in re.findall(r"[A-Za-z0-9_]{2,}", value)}


def advanced_search(args: dict[str, Any]) -> dict[str, Any]:
    root = resolve_path(str(args.get("path") or "."))
    if root.is_file():
        root = root.parent
    if not root.exists() or not root.is_dir():
        raise FileNotFoundError(f"Directory not found: {args.get('path', '.')}")
    mode = str(args.get("mode") or "text").casefold()
    allowed = {"text", "name", "semantic", "symbol", "references", "imports", "todo", "changed", "date"}
    if mode not in allowed:
        raise ValueError(f"mode must be one of: {', '.join(sorted(allowed))}")
    query = str(args.get("symbol") or args.get("query") or "").strip()
    name_pattern = str(args.get("name_pattern") or "").strip()
    changed = _git_changed(root)
    changed_only = bool(args.get("changed_only", False)) or mode == "changed"
    date_from, date_to = _date(args.get("date_from")), _date(args.get("date_to"))
    query_tokens = _tokens(query)
    results: list[dict[str, Any]] = []

    for path in _iter_files(root, name_pattern):
        resolved = str(path.resolve())
        if changed_only and resolved not in changed:
            continue
        try:
            stat = path.stat()
        except OSError:
            continue
        modified = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)
        if date_from and modified < date_from:
            continue
        if date_to and modified > date_to:
            continue
        base = {
            "path": resolved, "relative_path": str(path.resolve().relative_to(root)),
            "modified_at": modified.isoformat(), "size": stat.st_size,
            "git_status": changed.get(resolved), "language": LANGUAGES.get(path.suffix.casefold(), "Other"),
        }
        if mode in {"name", "changed", "date"}:
            if mode != "name" or not query or query.casefold() in path.name.casefold() or fnmatch.fnmatch(path.name, query):
                results.append({**base, "line": 0, "excerpt": "", "match_reason": f"{mode} file match", "score": 1.0})
            continue
        try:
            text, _encoding, _detection = _decode(path)
        except (OSError, UnicodeError, ValueError):
            continue
        lines = text.splitlines()
        if mode == "symbol" and path.suffix.casefold() == ".py":
            for item in _python_symbols(text):
                if not query or query.casefold() in item["symbol"].casefold():
                    results.append({**base, **item, "excerpt": lines[item["line"] - 1].strip(), "match_reason": "Python AST symbol definition", "score": 1.0})
        elif mode == "imports" and path.suffix.casefold() == ".py":
            try:
                tree = ast.parse(text)
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                if not isinstance(node, (ast.Import, ast.ImportFrom)):
                    continue
                source = ast.get_source_segment(text, node) or lines[node.lineno - 1].strip()
                if not query or query.casefold() in source.casefold():
                    results.append({**base, "line": node.lineno, "excerpt": source, "match_reason": "Python import", "score": 1.0})
        elif mode == "semantic":
            haystack = _tokens(path.name + " " + text[:100_000])
            overlap = len(query_tokens & haystack) / max(1, len(query_tokens))
            if overlap:
                results.append({**base, "line": 0, "excerpt": text[:240].replace("\n", " "), "match_reason": "token-overlap semantic fallback", "score": round(overlap, 3)})
        else:
            pattern_text = r"\b(?:TODO|FIXME|HACK|XXX)\b" if mode == "todo" else re.escape(query)
            try:
                pattern = re.compile(pattern_text, re.IGNORECASE)
            except re.error:
                pattern = re.compile(re.escape(pattern_text), re.IGNORECASE)
            for number, line in enumerate(lines, 1):
                if pattern.search(line):
                    results.append({
                        **base, "line": number, "excerpt": line.strip()[:500],
                        "match_reason": "reference occurrence" if mode == "references" else f"{mode} content match",
                        "score": 1.0,
                    })
                    if sum(1 for item in results if item["path"] == resolved) >= 20:
                        break

    results.sort(key=lambda item: (-float(item.get("score") or 0), item["relative_path"].casefold(), int(item.get("line") or 0)))
    offset = _parse_cursor(args.get("cursor"))
    limit = max(1, min(int(args.get("max_results", 20)), 200))
    page = results[offset:offset + limit]
    next_cursor = str(offset + limit) if offset + limit < len(results) else None
    related_tests: list[str] = []
    if bool(args.get("include_related_tests", False)):
        stems = {Path(item["path"]).stem.removeprefix("test_") for item in page}
        for path in _iter_files(root, "test_*.py"):
            if any(stem and stem in path.stem for stem in stems):
                related_tests.append(str(path.resolve()))
    groups: dict[str, list[dict[str, Any]]] = {}
    if str(args.get("group_by") or "").casefold() == "file":
        for item in page:
            groups.setdefault(item["path"], []).append(item)
    return {
        "mode": mode, "query": query, "root": str(root), "results": page,
        "total_results": len(results), "cursor": next_cursor, "groups": groups,
        "related_tests": sorted(set(related_tests)), "git_available": bool(changed) or (root / ".git").exists(),
    }


def project_scan(args: dict[str, Any], *, tree: bool = False) -> dict[str, Any]:
    root = resolve_path(str(args.get("path") or "."))
    if not root.exists() or not root.is_dir():
        raise FileNotFoundError(f"Directory not found: {args.get('path', '.')}")
    max_depth = max(1, min(int(args.get("max_depth", 3)), 12)) if tree else 1
    ignore_generated = bool(args.get("ignore_generated", True))
    changed = _git_changed(root) if bool(args.get("include_git", False)) else {}
    items: list[dict[str, Any]] = []
    languages: dict[str, int] = {}
    total_size = 0
    for path in root.rglob("*"):
        try:
            relative = path.relative_to(root)
        except ValueError:
            continue
        if len(relative.parts) > max_depth or (ignore_generated and _is_ignored(path, root)):
            continue
        try:
            stat = path.stat()
        except OSError:
            continue
        kind = "directory" if path.is_dir() else "file"
        language = LANGUAGES.get(path.suffix.casefold(), "Other") if kind == "file" else ""
        if kind == "file":
            total_size += stat.st_size
            languages[language] = languages.get(language, 0) + 1
        items.append({
            "path": str(path.resolve()), "relative_path": str(relative), "name": path.name,
            "kind": kind, "depth": len(relative.parts), "size": stat.st_size if kind == "file" else 0,
            "modified_at": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
            "language": language, "git_status": changed.get(str(path.resolve())),
        })
    sort = str(args.get("sort") or "name").casefold()
    if sort == "size":
        items.sort(key=lambda item: (item["kind"] != "directory", -item["size"], item["relative_path"].casefold()))
    elif sort == "activity":
        items.sort(key=lambda item: item["modified_at"], reverse=True)
    elif sort == "relevance":
        query = str(args.get("query") or "").casefold()
        items.sort(key=lambda item: (query not in item["relative_path"].casefold(), item["relative_path"].casefold()))
    else:
        items.sort(key=lambda item: (item["kind"] != "directory", item["relative_path"].casefold()))
    offset = _parse_cursor(args.get("cursor"))
    limit = max(1, min(int(args.get("max_items", 100)), 500))
    page = items[offset:offset + limit]
    return {
        "root": str(root), "mode": "project" if tree else "directory", "items": page,
        "total_items": len(items), "cursor": str(offset + limit) if offset + limit < len(items) else None,
        "summary": {"file_count": sum(item["kind"] == "file" for item in items), "directory_count": sum(item["kind"] == "directory" for item in items), "total_size": total_size, "languages": languages},
    }


def _apply_unified_patch(original: str, patch: str) -> str:
    source = original.splitlines(keepends=True)
    output: list[str] = []
    source_index = 0
    lines = patch.splitlines(keepends=True)
    index = 0
    while index < len(lines):
        match = re.match(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", lines[index])
        if not match:
            index += 1
            continue
        old_start = int(match.group(1)) - 1
        output.extend(source[source_index:old_start])
        source_index = old_start
        index += 1
        while index < len(lines) and not lines[index].startswith("@@"):
            line = lines[index]
            if line.startswith(" "):
                if source_index >= len(source) or source[source_index].rstrip("\r\n") != line[1:].rstrip("\r\n"):
                    raise ValueError("Patch context does not match the current file")
                output.append(source[source_index])
                source_index += 1
            elif line.startswith("-") and not line.startswith("---"):
                if source_index >= len(source) or source[source_index].rstrip("\r\n") != line[1:].rstrip("\r\n"):
                    raise ValueError("Patch deletion does not match the current file")
                source_index += 1
            elif line.startswith("+") and not line.startswith("+++"):
                output.append(line[1:])
            index += 1
    if not output and "@@" not in patch:
        raise ValueError("patch must contain at least one unified-diff hunk")
    output.extend(source[source_index:])
    return "".join(output)


def _validate_content(content: str, validation: str, path: Path) -> None:
    selected = str(validation or "").casefold()
    if selected == "json" or (selected == "auto" and path.suffix.casefold() == ".json"):
        json.loads(content)
    elif selected == "python" or (selected == "auto" and path.suffix.casefold() == ".py"):
        ast.parse(content)


def _format_content(content: str, formatter: str, path: Path) -> str:
    selected = str(formatter or "").casefold()
    if selected == "json" or (selected == "auto" and path.suffix.casefold() == ".json"):
        return json.dumps(json.loads(content), ensure_ascii=False, indent=2) + "\n"
    return content


def advanced_write(args: dict[str, Any]) -> dict[str, Any]:
    path = resolve_write_path(str(args["path"]))
    mode = str(args.get("mode") or "overwrite").casefold()
    encoding = str(args.get("encoding") or "utf-8")
    exists = path.exists()
    previous = path.read_text(encoding=encoding, errors="replace") if exists and path.is_file() else ""
    incoming = str(args.get("content") or "")
    if mode == "append":
        content = previous + incoming
    elif mode == "merge":
        if path.suffix.casefold() != ".json":
            raise ValueError("merge mode currently supports JSON objects")
        base = json.loads(previous or "{}")
        update = json.loads(incoming or "{}")
        if not isinstance(base, dict) or not isinstance(update, dict):
            raise ValueError("JSON merge requires object values")
        content = json.dumps({**base, **update}, ensure_ascii=False, indent=2) + "\n"
    elif mode == "patch":
        content = _apply_unified_patch(previous, str(args.get("patch") or incoming))
    elif mode == "template":
        template = str(args.get("template") or incoming)
        for key, value in dict(args.get("variables") or {}).items():
            template = template.replace("{{" + str(key) + "}}", str(value))
        content = template
    elif mode == "overwrite":
        content = incoming
    else:
        raise ValueError("mode must be overwrite, append, merge, patch, or template")
    newline = str(args.get("newline") or "preserve").casefold()
    if newline in {"lf", "crlf"}:
        content = content.replace("\r\n", "\n").replace("\r", "\n")
        content = content.replace("\n", "\r\n") if newline == "crlf" else content
    content = _format_content(content, str(args.get("formatter") or ""), path)
    _validate_content(content, str(args.get("validation") or ""), path)
    diff = "".join(difflib.unified_diff(previous.splitlines(True), content.splitlines(True), fromfile=str(path), tofile=str(path)))
    if bool(args.get("dry_run", False)):
        return {"path": str(path), "mode": mode, "changed": previous != content, "created": not exists, "preview": diff, "dry_run": True, "undo_id": None}
    pass
    if bool(args.get("create_parents", True)):
        path.parent.mkdir(parents=True, exist_ok=True)
    backup = _create_backup(path, "advanced-write") if exists else None
    atomic_write(path, content, encoding=encoding)
    return {"path": str(path), "mode": mode, "changed": previous != content, "created": not exists, "preview": diff, "dry_run": False, "undo_id": str(backup) if backup else None, "bytes": len(content.encode(encoding))}


def advanced_edit(args: dict[str, Any]) -> dict[str, Any]:
    path = resolve_write_path(str(args["path"]))
    if not path.exists() or not path.is_file():
        raise FileNotFoundError(f"File not found: {args['path']}")
    encoding = str(args.get("encoding") or "utf-8")
    original = path.read_text(encoding=encoding, errors="replace")
    mode = str(args.get("mode") or "replace").casefold()
    content = original
    if mode == "replace":
        old, new = str(args.get("old_text") or ""), str(args.get("new_text") or "")
        matches = [match.start() for match in re.finditer(re.escape(old), content)] if old else []
        selection = int(args.get("match_index", 1))
        if not matches or not 1 <= selection <= len(matches):
            raise ValueError(f"Requested match {selection} is unavailable; found {len(matches)} match(es)")
        position = matches[selection - 1]
        content = content[:position] + new + content[position + len(old):]
    elif mode == "regex":
        count = int(args.get("count", 0))
        content, replaced = re.subn(str(args.get("pattern") or ""), str(args.get("replacement") or ""), content, count=count)
        if replaced == 0:
            raise ValueError("regex pattern matched no content")
    elif mode == "line_range":
        lines = content.splitlines(keepends=True)
        start, end = int(args.get("start_line", 1)), int(args.get("end_line", args.get("start_line", 1)))
        if start < 1 or end < start or end > len(lines):
            raise ValueError("line range is outside the file")
        replacement = str(args.get("new_text") or "")
        if replacement and not replacement.endswith(("\n", "\r")) and end < len(lines):
            replacement += "\r\n" if "\r\n" in content else "\n"
        content = "".join([*lines[:start - 1], replacement, *lines[end:]])
    elif mode == "patch":
        content = _apply_unified_patch(content, str(args.get("patch") or ""))
    elif mode == "python_ast":
        start, end, _selected = _select_symbol(content, str(args.get("symbol") or ""))
        lines = content.splitlines(keepends=True)
        replacement = str(args.get("new_text") or "")
        if replacement and not replacement.endswith("\n") and end < len(lines):
            replacement += "\n"
        content = "".join([*lines[:start - 1], replacement, *lines[end:]])
    elif mode == "json_fields":
        value = json.loads(content)
        if not isinstance(value, dict):
            raise ValueError("json_fields mode requires a top-level JSON object")
        value.update(dict(args.get("fields") or {}))
        content = json.dumps(value, ensure_ascii=False, indent=2) + "\n"
    else:
        raise ValueError("mode must be replace, regex, line_range, patch, python_ast, or json_fields")
    content = _format_content(content, str(args.get("formatter") or ""), path)
    _validate_content(content, str(args.get("validation") or ""), path)
    diff = "".join(difflib.unified_diff(original.splitlines(True), content.splitlines(True), fromfile=str(path), tofile=str(path)))
    if bool(args.get("dry_run", False)):
        return {"path": str(path), "mode": mode, "changed": content != original, "preview": diff, "dry_run": True, "undo_id": None}
    backup = _create_backup(path, "advanced-edit")
    atomic_write(path, content, encoding=encoding)
    return {"path": str(path), "mode": mode, "changed": content != original, "preview": diff, "dry_run": False, "undo_id": str(backup)}


def plan_batch(operations: list[dict[str, Any]]) -> dict[str, Any]:
    by_id: dict[str, dict[str, Any]] = {}
    for index, operation in enumerate(operations):
        operation_id = str(operation.get("id") or f"op-{index + 1}")
        if operation_id in by_id:
            raise ValueError(f"Duplicate batch operation id: {operation_id}")
        by_id[operation_id] = {**operation, "id": operation_id}
    ordered: list[dict[str, Any]] = []
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(operation_id: str) -> None:
        if operation_id in visited:
            return
        if operation_id in visiting:
            raise ValueError("Batch operation dependencies contain a cycle")
        if operation_id not in by_id:
            raise ValueError(f"Unknown batch dependency: {operation_id}")
        visiting.add(operation_id)
        for dependency in by_id[operation_id].get("depends_on") or []:
            visit(str(dependency))
        visiting.remove(operation_id)
        visited.add(operation_id)
        ordered.append(by_id[operation_id])

    for key in by_id:
        visit(key)
    runnable: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for operation in ordered:
        condition = dict(operation.get("condition") or {})
        path_value = condition.get("path") or operation.get("path")
        path = resolve_write_path(str(path_value)) if path_value else None
        okay = True
        reason = ""
        if condition.get("exists") is True and path is not None and not path.exists():
            okay, reason = False, "required path does not exist"
        if condition.get("not_exists") is True and path is not None and path.exists():
            okay, reason = False, "path already exists"
        if "contains" in condition and path is not None:
            okay = path.exists() and str(condition["contains"]) in path.read_text(encoding="utf-8", errors="replace")
            reason = "required content is absent" if not okay else ""
        (runnable if okay else skipped).append(operation if okay else {**operation, "skip_reason": reason})
    return {"ordered": ordered, "runnable": runnable, "skipped": skipped}
