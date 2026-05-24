import time
from pathlib import Path

from tools.registry import get_tool_schemas, tool
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

_AUDIT_VERSION = "2.0.0"


def _resolve_root(root: str) -> Path:
    return Path(root or ".").expanduser().resolve()


def _python_tool_modules(tools_dir: Path, limit: int) -> list[str]:
    modules = []
    if not tools_dir.exists() or not tools_dir.is_dir():
        return modules
    for path in sorted(tools_dir.iterdir()):
        if len(modules) >= limit:
            break
        if not path.is_file() or path.suffix != ".py":
            continue
        if path.name.startswith("__"):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        if "@tool(" in text:
            modules.append(path.name)
    return modules


def _manifest_modules(manifest_path: Path, limit: int) -> tuple[list[str], str]:
    if not manifest_path.exists():
        return [], "missing"
    try:
        text = manifest_path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return [], "decode_error"
    except OSError:
        return [], "read_error"

    modules = []
    in_active_section = False
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if line == "## Active Executable Tools":
            in_active_section = True
            continue
        if in_active_section and line.startswith("## "):
            break
        if not in_active_section or not line.startswith("- `"):
            continue
        remainder = line[3:]
        end = remainder.find("`")
        if end <= 0:
            continue
        value = remainder[:end]
        if value.endswith(".py"):
            modules.append(value)
        if len(modules) >= limit:
            break
    return modules, "ok"


def _registered_tool_names(limit: int) -> list[str]:
    names = []
    for schema in get_tool_schemas():
        function = schema.get("function", {})
        name = function.get("name")
        if name:
            names.append(name)
        if len(names) >= limit:
            break
    return sorted(names)


def _audit_payload(repo_root: Path, max_items: int, include_schema: bool) -> tuple[dict, str]:
    tools_dir = repo_root / "tools"
    manifest_path = tools_dir / "TOOL_MANIFEST.md"
    observed_modules = _python_tool_modules(tools_dir, max_items)
    manifest_modules, manifest_status = _manifest_modules(manifest_path, max_items)
    observed_set = set(observed_modules)
    manifest_set = set(manifest_modules)
    missing_from_manifest = sorted(observed_set - manifest_set)
    missing_from_files = sorted(manifest_set - observed_set)
    schema_names = _registered_tool_names(max_items) if include_schema else []
    status = "success"
    if manifest_status != "ok" or missing_from_manifest or missing_from_files:
        status = "partial"
    result = {
        "status": status,
        "scope": str(repo_root),
        "manifest_status": manifest_status,
        "observed_module_count": len(observed_modules),
        "observed_modules": observed_modules,
        "manifest_module_count": len(manifest_modules),
        "manifest_modules": manifest_modules,
        "missing_from_manifest": missing_from_manifest,
        "missing_from_files": missing_from_files,
        "registered_schema_count": len(schema_names),
        "registered_schemas": schema_names,
        "aligned": status == "success",
    }
    lines = [
        f"Status: {status}",
        f"Scope: {repo_root}",
        f"Manifest: {manifest_status}",
        f"Observed tool modules: {len(observed_modules)}",
        f"Observed module names: {', '.join(observed_modules) if observed_modules else 'none'}",
        f"Manifest tool modules: {len(manifest_modules)}",
        f"Manifest module names: {', '.join(manifest_modules) if manifest_modules else 'none'}",
        f"Missing from manifest: {', '.join(missing_from_manifest) if missing_from_manifest else 'none'}",
        f"Missing from files: {', '.join(missing_from_files) if missing_from_files else 'none'}",
    ]
    if include_schema:
        lines.append(f"Registered callable schemas: {len(schema_names)}")
        lines.append(f"Callable names: {', '.join(schema_names) if schema_names else 'none'}")
    lines.append("Evidence: read-only local inspection; no files changed")
    return result, "\n".join(lines)


@tool(
    name="tool_manifest_audit",
    description=(
        "Audit KING's markdown tool manifest against observed tool modules and "
        "registered callable schemas. Read-only; use before or after toolchain changes."
    ),
    examples=[
        "audit the tool manifest",
        "check whether tools/TOOL_MANIFEST.md matches registered tools",
        "verify tool registration evidence",
    ],
    param_descriptions={
        "root": "Repository root to audit. Defaults to current working directory.",
        "max_items": "Maximum modules or schemas to list, from 1 to 500.",
        "include_schema": "Whether to include registered callable names in the report.",
        "response_format": "legacy or structured",
        "trace_enabled": "Emit machine-readable trace when true",
    },
)
def tool_manifest_audit(
    root: str = ".",
    max_items: int = 200,
    include_schema: bool = True,
    response_format: str = "legacy",
    trace_enabled: bool = False,
):
    started = time.perf_counter()
    started_at = utc_now_iso()
    inputs_received = 5
    response_format = normalize_response_format(response_format)
    trace_enabled = coerce_bool(trace_enabled)
    include_schema = coerce_bool(include_schema)
    max_items, max_error = normalize_int(max_items, "max_items", 200, 1, 500, "Use max_items between 1 and 500.", "INVALID_MAX_ITEMS")
    if max_error is not None:
        trace = make_trace("tool_manifest_audit", _AUDIT_VERSION, started_at, started, inputs_received, False, "validate", "FAILED", 1, error_code=max_error["code"])
        emit_trace(trace, trace_enabled)
        if response_format == "structured":
            return structured_error("tool_manifest_audit", _AUDIT_VERSION, max_error, started, trace)
        return "Status: blocked\nReason: max_items must be an integer between 1 and 500"

    repo_root = _resolve_root(root)
    if not repo_root.exists():
        error = error_payload("ROOT_NOT_FOUND", "Audit root does not exist.", "root", str(repo_root), "existing directory", False, "Pass a valid repository root.")
        trace = make_trace("tool_manifest_audit", _AUDIT_VERSION, started_at, started, inputs_received, False, "validate", "FAILED", 1, error_code=error["code"])
        emit_trace(trace, trace_enabled)
        if response_format == "structured":
            return structured_error("tool_manifest_audit", _AUDIT_VERSION, error, started, trace)
        return f"Status: blocked\nReason: root not found\nScope: {repo_root}"
    if not repo_root.is_dir():
        error = error_payload("ROOT_NOT_DIRECTORY", "Audit root is not a directory.", "root", str(repo_root), "directory path", False, "Pass a directory root.")
        trace = make_trace("tool_manifest_audit", _AUDIT_VERSION, started_at, started, inputs_received, False, "validate", "FAILED", 1, error_code=error["code"])
        emit_trace(trace, trace_enabled)
        if response_format == "structured":
            return structured_error("tool_manifest_audit", _AUDIT_VERSION, error, started, trace)
        return f"Status: blocked\nReason: root is not a directory\nScope: {repo_root}"

    result, legacy = _audit_payload(repo_root, max_items, include_schema)
    status = "SUCCESS" if result["status"] == "success" else "PARTIAL"
    trace = make_trace("tool_manifest_audit", _AUDIT_VERSION, started_at, started, inputs_received, True, "audit", status, len(result))
    emit_trace(trace, trace_enabled)
    if response_format == "structured":
        return structured_success("tool_manifest_audit", _AUDIT_VERSION, result, started, trace)
    return legacy
