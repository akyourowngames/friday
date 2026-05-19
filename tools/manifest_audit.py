from pathlib import Path

from tools.registry import get_tool_schemas, tool


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
        except UnicodeDecodeError:
            continue
        except OSError:
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


def _format_list(label: str, values: list[str]) -> str:
    if not values:
        return f"{label}: none"
    return f"{label}: {', '.join(values)}"


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
    },
)
def tool_manifest_audit(root: str = ".", max_items: int = 200, include_schema: bool = True) -> str:
    try:
        max_items = int(max_items)
    except (TypeError, ValueError):
        return "Status: blocked\nReason: max_items must be an integer between 1 and 500"
    max_items = max(1, min(max_items, 500))

    repo_root = _resolve_root(root)
    if not repo_root.exists():
        return f"Status: blocked\nReason: root not found\nScope: {repo_root}"
    if not repo_root.is_dir():
        return f"Status: blocked\nReason: root is not a directory\nScope: {repo_root}"

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

    lines = [
        f"Status: {status}",
        f"Scope: {repo_root}",
        f"Manifest: {manifest_status}",
        f"Observed tool modules: {len(observed_modules)}",
        _format_list("Observed module names", observed_modules),
        f"Manifest tool modules: {len(manifest_modules)}",
        _format_list("Manifest module names", manifest_modules),
        _format_list("Missing from manifest", missing_from_manifest),
        _format_list("Missing from files", missing_from_files),
    ]
    if include_schema:
        lines.append(f"Registered callable schemas: {len(schema_names)}")
        lines.append(_format_list("Callable names", schema_names))
    lines.append("Evidence: read-only local inspection; no files changed")
    return "\n".join(lines)
