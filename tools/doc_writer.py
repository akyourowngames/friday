"""Document Writer — plan, generate, write, and deliver documents intelligently.

A single tool that takes a natural-language request like "draft a database schema
for the budget tracker" and:

1. Plans the document structure via LLM
2. Generates the full content via LLM
3. Writes to storage/docs/ with a descriptive filename
4. Delivers it: opens locally, shows in terminal, or sends via telegram

All document types, format rules, and delivery logic live in
tools/DOC_WRITER_POLICY.md. No hardcoded document types or delivery
decisions in code.
"""

import json
import os
import re
import tempfile
import time
from pathlib import Path

from config import settings
from tools.registry import tool
from tools.runtime import (
    coerce_bool,
    emit_trace,
    error_payload,
    make_trace,
    normalize_response_format,
    structured_error,
    structured_success,
    utc_now_iso,
)

_VERSION = "2.0.0"

_POLICY_PATH = Path(__file__).resolve().parent / "DOC_WRITER_POLICY.md"


def _docs_dir() -> Path:
    path = Path(settings.docs_dir)
    if not path.is_absolute():
        path = Path(__file__).resolve().parent.parent / path
    return path.resolve()


def _load_policy() -> str:
    if _POLICY_PATH.exists():
        return _POLICY_PATH.read_text(encoding="utf-8")
    return ""


def _llm_call(system: str, user_content: str, max_tokens: int = 2000, model: str = "") -> str | None:
    """One-shot LLM call. Returns None on failure."""
    if not settings.nim_api_key or not settings.nim_api_key.strip():
        return None
    use_model = model or settings.generation_model or settings.model_name
    try:
        from openai import OpenAI

        client = OpenAI(
            base_url=settings.nim_base_url,
            api_key=settings.nim_api_key,
            timeout=90,
            max_retries=2,
        )
        resp = client.chat.completions.create(
            model=use_model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user_content},
            ],
            temperature=0.2,
            max_tokens=max_tokens,
        )
        text = resp.choices[0].message.content or ""
        # Strip markdown fences if present: ```lang ... ``` or bare ``` ... ```
        lines = text.strip().splitlines()
        if len(lines) >= 2:
            first = lines[0].strip()
            last = lines[-1].strip()
            is_fence_open = first.startswith("```") and len(first) <= 20
            is_fence_close = last == "```"
            if is_fence_open and is_fence_close:
                return "\n".join(lines[1:-1]).strip()
        return text.strip()
    except Exception as e:
        import sys
        print(f"  [doc_write LLM error: {type(e).__name__}: {e}]", file=sys.stderr)
        return None


def _sanitize_filename(text: str) -> str:
    """Convert text to a safe kebab-case filename stem."""
    text = text.lower().strip()
    text = re.sub(r"[^a-z0-9\s-]", "", text)
    text = re.sub(r"[\s_]+", "-", text)
    text = re.sub(r"-+", "-", text)
    text = text.strip("-")
    return text[:60] or "document"


def _unique_path(directory: Path, filename: str, extension: str) -> Path:
    """Return a path that doesn't overwrite existing files."""
    stem = _sanitize_filename(filename)
    candidate = directory / f"{stem}{extension}"
    if not candidate.exists():
        return candidate
    for i in range(2, 100):
        candidate = directory / f"{stem}-{i}{extension}"
        if not candidate.exists():
            return candidate
    return directory / f"{stem}-99{extension}"


def _atomic_write(path: Path, content: str) -> None:
    """Write content atomically via temp file + replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


_PLAN_SYSTEM_PROMPT = """You are a document planner for a personal AI assistant called KING.
Given a user request and optional project context, decide what document to create.

Return ONLY a valid JSON object with these fields:
- "doc_type": one of: database_schema, api_spec, project_outline, technical_spec, report, code_snippet, config_file, readme, other
- "format": the file extension (e.g. ".sql", ".md", ".json", ".py", ".js", ".yaml")
- "filename": a short kebab-case filename without extension (e.g. "budget-tracker-schema")
- "title": a human-readable title for the document
- "sections": a list of section titles/headings to include
- "summary": a one-sentence description of what this document will contain

Rules:
- Choose the format that best fits the content, not what the user might have said
- For code snippets, use the appropriate language extension
- Keep the filename under 60 characters, lowercase, kebab-case
- Be specific about sections — they should form a complete outline
- If the request is vague, pick the most likely document type from context"""

_GENERATE_SYSTEM_PROMPT = """Write a complete document based on this plan.

Plan: {plan}

Rules:
- Write for a reader who will use this document
- Be specific, no generic filler
- For code: include syntax, imports, types
- For schemas: snake_case, types, constraints, comments
- For specs: concrete examples
- Output ONLY the document content
- Do NOT wrap in markdown code fences"""


def _plan_document(request: str, project_context: str = "") -> dict | None:
    """Use LLM to plan the document structure."""
    user_content = f"User request: {request}"
    if project_context:
        user_content += f"\n\nProject context:\n{project_context}"
    result = _llm_call(_PLAN_SYSTEM_PROMPT, user_content, max_tokens=400)
    if not result:
        return None
    try:
        parsed = json.loads(result)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(parsed, dict):
        return None
    if not parsed.get("doc_type") or not parsed.get("format"):
        return None
    return parsed


def _strip_markdown_fences(text: str) -> str:
    """Strip wrapping markdown code fences from LLM output.
    
    Handles: ```lang ... ```, bare ``` ... ```, and nested fences.
    """
    text = text.strip()
    lines = text.splitlines()
    if len(lines) >= 2:
        first = lines[0].strip()
        last = lines[-1].strip()
        # Match ``` or ```lang (e.g. ```sql, ```markdown, ```yaml)
        # but only if the opening line is short (just the fence + optional lang tag)
        is_fence_open = first.startswith("```") and len(first) <= 20
        is_fence_close = last == "```"
        if is_fence_open and is_fence_close:
            return "\n".join(lines[1:-1]).strip()
    return text


def _generate_content(plan: dict, request: str, project_context: str = "") -> str | None:
    """Use LLM to generate the full document content."""
    plan_text = json.dumps(plan, indent=2, ensure_ascii=False)
    user_content = f"Request: {request}\n\nPlan:\n{plan_text}"
    if project_context:
        user_content += f"\n\nProject context:\n{project_context}"
    system = _GENERATE_SYSTEM_PROMPT.format(plan=plan_text)
    result = _llm_call(system, user_content, max_tokens=settings.doc_writer_max_tokens)
    if not result:
        return None
    # Double-strip: LLM may nest fences (```markdown ... ```)
    cleaned = _strip_markdown_fences(result)
    if cleaned != result:
        cleaned = _strip_markdown_fences(cleaned)
    return cleaned


def _get_project_context(project_name: str) -> str:
    """Pull context from the project manager if a project name is given."""
    if not project_name:
        return ""
    try:
        from project_manager.store import ProjectStore

        store = ProjectStore()
        projects = store.all_projects()
        for p in projects:
            name = str(p.get("name", "")).lower()
            slug = str(p.get("id", "")).lower()
            query = project_name.lower().strip()
            if query in name or query in slug:
                tasks = p.get("tasks", [])
                task_list = []
                for t in tasks[:15]:
                    status = t.get("status", "open")
                    task_list.append(f"  - {t.get('title', '?')} [{status}]")
                blockers = [b.get("text", "") for b in p.get("blockers", []) if not b.get("resolved")]
                lines = [
                    f"Project: {p.get('name')}",
                    f"Goal: {p.get('goal', 'n/a')}",
                    f"Status: {p.get('status', 'n/a')}",
                    f"Health: {p.get('health', 'n/a')}",
                ]
                if task_list:
                    lines.append("Tasks:")
                    lines.extend(task_list)
                if blockers:
                    lines.append("Blockers:")
                    for b in blockers:
                        lines.append(f"  - {b}")
                return "\n".join(lines)
    except Exception:
        pass
    return ""


def _deliver(path: Path, content: str, delivery: str, doc_type: str) -> str:
    """Deliver the document to the user. Returns a description of what was done."""
    ext = path.suffix.lower()
    content_len = len(content)

    # Respect explicit override
    if delivery == "open":
        return _open_locally(path)
    if delivery == "terminal":
        return _show_in_terminal(path, content)

    # Auto-decide based on file type and content length
    code_exts = {".sql", ".py", ".js", ".ts", ".jsx", ".tsx", ".json", ".yaml", ".yml", ".toml", ".sh", ".ps1"}
    if ext in code_exts:
        return _open_locally(path)

    if ext == ".md" and content_len <= 3000:
        return _show_in_terminal(path, content)

    return _open_locally(path)


def _open_locally(path: Path) -> str:
    """Open the file with the OS default handler."""
    try:
        import sys

        if sys.platform == "win32":
            os.startfile(str(path))
        elif sys.platform == "darwin":
            import subprocess

            subprocess.run(["open", str(path)], capture_output=True, timeout=10)
        else:
            import subprocess

            subprocess.run(["xdg-open", str(path)], capture_output=True, timeout=10)
        return f"Opened {path.name} in your default editor."
    except Exception as e:
        return f"Wrote {path.name} but could not open it automatically: {e}. File is at: {path}"


def _show_in_terminal(path: Path, content: str) -> str:
    """Show the content in the terminal with a file path note."""
    truncated = False
    display = content
    if len(content) > 3000:
        display = content[:3000]
        truncated = True
    # Strip non-ASCII characters for Windows console compatibility
    display = display.encode("ascii", errors="replace").decode("ascii")
    ext = path.suffix.lower()
    lang_tag = ext.lstrip(".") if ext else ""
    header = f"=== {path.name} ({len(content)} chars) ===\n"
    footer = f"\n=== Saved to: {path} ==="
    if truncated:
        footer += "\n=== (truncated in terminal, full file on disk) ==="
    return header + display + footer


def _build_trace(name, started_at, started, schema_valid, status, output_fields, error_code=None):
    return make_trace(
        name, _VERSION, started_at, started, 1, schema_valid, name,
        status, output_fields, {"count": 1, "systems": ["doc_writer"]}, error_code,
    )


@tool(
    name="doc_write",
    description=(
        "Draft a document, write it to disk, and deliver it intelligently. "
        "Plans the structure via LLM, generates full content, saves to storage/docs/, "
        "and auto-decides whether to open locally, show in terminal, or send via telegram."
    ),
    examples=[
        "draft a database schema for the budget tracker",
        "write an API spec for the auth endpoints",
        "create a project outline for the new workshop",
        "draft a report on project progress",
        "write a readme for the budget tracker project",
        "create a config file for the deployment",
    ],
    param_descriptions={
        "request": "What to draft — be specific about topic, scope, and any constraints",
        "project": "Optional project name to pull context from the project manager",
        "delivery": "'open' to open locally, 'terminal' to show in text, 'telegram' to send, or 'auto' (default)",
        "response_format": "legacy or structured",
        "trace_enabled": "When true, emit a machine-readable trace entry",
    },
)
def doc_write(
    request: str,
    project: str = "",
    delivery: str = "auto",
    response_format: str = "legacy",
    trace_enabled: bool = False,
):
    started = time.perf_counter()
    started_at = utc_now_iso()
    response_format = normalize_response_format(response_format)
    trace_enabled = coerce_bool(trace_enabled)

    text = str(request or "").strip()
    if not text:
        err = error_payload(
            "EMPTY_REQUEST",
            "request must not be empty.",
            "request", text, "a sentence describing what to draft",
            False, "Describe what you want drafted.",
        )
        trace = _build_trace("doc_write", started_at, time.perf_counter() - started, False, "FAILED", 0, "EMPTY_REQUEST")
        emit_trace(trace, trace_enabled)
        return _emit("doc_write", started, started_at, trace_enabled, error=err, response_format=response_format, legacy="Error: nothing to draft", status="FAILED")

    # 1. Pull project context if specified
    project_context = _get_project_context(str(project or ""))

    # 2. Plan the document
    plan = _plan_document(text, project_context)
    if not plan:
        err = error_payload(
            "PLAN_FAILED",
            "Could not plan the document. Try a more specific request.",
            "request", text, "a clear document request",
            True, "Try rephrasing with more detail about what you want.",
        )
        trace = _build_trace("doc_write", started_at, time.perf_counter() - started, False, "FAILED", 0, "PLAN_FAILED")
        emit_trace(trace, trace_enabled)
        return _emit("doc_write", started, started_at, trace_enabled, error=err, response_format=response_format, legacy="Error: could not plan the document", status="FAILED")

    # 3. Generate the content
    content = _generate_content(plan, text, project_context)
    if not content:
        err = error_payload(
            "GENERATION_FAILED",
            "Could not generate the document content. Try a simpler request.",
            "request", text, "a simpler document request",
            True, "Try a shorter or more specific request.",
        )
        trace = _build_trace("doc_write", started_at, time.perf_counter() - started, False, "FAILED", 0, "GENERATION_FAILED")
        emit_trace(trace, trace_enabled)
        return _emit("doc_write", started, started_at, trace_enabled, error=err, response_format=response_format, legacy="Error: could not generate content", status="FAILED")

    # 4. Write to disk
    docs_dir = _docs_dir()
    doc_format = str(plan.get("format", ".md")).strip()
    if not doc_format.startswith("."):
        doc_format = "." + doc_format
    filename = str(plan.get("filename", "document"))
    file_path = _unique_path(docs_dir, filename, doc_format)

    try:
        _atomic_write(file_path, content)
    except Exception as e:
        err = error_payload(
            "WRITE_FAILED",
            f"Could not write file: {e}",
            "path", str(file_path), "a writable location",
            True, "Check storage/docs/ permissions.",
        )
        trace = _build_trace("doc_write", started_at, time.perf_counter() - started, False, "FAILED", 0, "WRITE_FAILED")
        emit_trace(trace, trace_enabled)
        return _emit("doc_write", started, started_at, trace_enabled, error=err, response_format=response_format, legacy=f"Error: could not write {file_path.name}", status="FAILED")

    # 5. Deliver
    delivery_result = _deliver(file_path, content, str(delivery or "auto"), str(plan.get("doc_type", "other")))

    # 6. Build response
    result = {
        "file": str(file_path),
        "filename": file_path.name,
        "doc_type": plan.get("doc_type"),
        "format": doc_format,
        "title": plan.get("title"),
        "size_chars": len(content),
        "delivery": delivery_result,
        "sections": plan.get("sections", []),
    }

    legacy = (
        f"Drafted '{plan.get('title', file_path.name)}' ({doc_format}, {len(content)} chars).\n"
        f"{delivery_result}"
    )

    trace = _build_trace("doc_write", started_at, time.perf_counter() - started, True, "SUCCESS", len(result))
    emit_trace(trace, trace_enabled)
    return _emit("doc_write", started, started_at, trace_enabled, result=result, response_format=response_format, legacy=legacy)


def _emit(name, started, started_at, trace_enabled, result=None, error=None, response_format="legacy", legacy="", status="SUCCESS"):
    valid = error is None
    trace = _build_trace(name, started_at, time.perf_counter() - started, valid, status if valid else "FAILED", len(result) if isinstance(result, dict) else 1, None if valid else error.get("code") if isinstance(error, dict) else None)
    emit_trace(trace, trace_enabled)
    if response_format == "structured":
        if valid:
            return structured_success(name, _VERSION, result, started, trace)
        return structured_error(name, _VERSION, error, started, trace)
    return legacy
