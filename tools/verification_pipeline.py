import subprocess
import sys
import time
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
    normalize_timeout_ms,
    structured_error,
    structured_success,
    utc_now_iso,
)


_PIPELINE_VERSION = "1.0.0"


def _resolve_root(root: str) -> Path:
    return Path(root or ".").expanduser().resolve()


def _is_inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _resolve_pipeline_path(root: Path, pipeline_path: str) -> Path:
    configured = str(pipeline_path or "").strip() or settings.verification_pipeline_file
    path = Path(configured).expanduser()
    if not path.is_absolute():
        path = root / path
    return path.resolve()


def _strip_code_ticks(value: str) -> str:
    text = value.strip()
    if len(text) >= 2 and text[0] == "`" and text[-1] == "`":
        return text[1:-1].strip()
    return text


def _parse_bool(value: str, default: bool) -> bool:
    text = value.strip().lower()
    if text in ("true", "1", "yes", "required"):
        return True
    if text in ("false", "0", "no", "optional"):
        return False
    return default


def _parse_pipeline(text: str, max_steps: int) -> list[dict]:
    steps = []
    current = None
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("- command:"):
            command = _strip_code_ticks(line.partition(":")[2])
            if not command:
                current = None
                continue
            current = {
                "command": command,
                "required": True,
                "reason": "",
            }
            steps.append(current)
            if len(steps) >= max_steps:
                break
            continue
        if current is None:
            continue
        if line.startswith("required:"):
            current["required"] = _parse_bool(line.partition(":")[2], True)
        elif line.startswith("reason:"):
            current["reason"] = line.partition(":")[2].strip()
    return steps


def _shell_command(command: str) -> list[str]:
    if sys.platform == "win32":
        return ["powershell", "-NoProfile", "-Command", command]
    return ["bash", "-c", command]


def _clip(text: str, limit: int) -> tuple[str, bool]:
    value = text or ""
    if len(value) <= limit:
        return value, False
    return value[:limit] + "\n...[truncated]", True


def _run_step(command: str, root: Path, timeout_ms: int, output_limit: int, required: bool, reason: str) -> dict:
    started = time.perf_counter()
    try:
        completed = subprocess.run(
            _shell_command(command),
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=timeout_ms / 1000,
        )
        stdout, stdout_truncated = _clip(completed.stdout, output_limit)
        stderr, stderr_truncated = _clip(completed.stderr, output_limit)
        return {
            "command": command,
            "required": required,
            "reason": reason,
            "status": "PASS" if completed.returncode == 0 else "FAIL",
            "exit_code": completed.returncode,
            "duration_ms": int((time.perf_counter() - started) * 1000),
            "stdout": stdout,
            "stderr": stderr,
            "truncated": stdout_truncated or stderr_truncated,
        }
    except subprocess.TimeoutExpired as exc:
        stdout, stdout_truncated = _clip(exc.stdout or "", output_limit)
        stderr, stderr_truncated = _clip(exc.stderr or "", output_limit)
        return {
            "command": command,
            "required": required,
            "reason": reason,
            "status": "TIMEOUT",
            "exit_code": -1,
            "duration_ms": int((time.perf_counter() - started) * 1000),
            "stdout": stdout,
            "stderr": stderr or f"Command timed out after {timeout_ms}ms",
            "truncated": stdout_truncated or stderr_truncated,
        }
    except FileNotFoundError as exc:
        return {
            "command": command,
            "required": required,
            "reason": reason,
            "status": "UNAVAILABLE",
            "exit_code": -1,
            "duration_ms": int((time.perf_counter() - started) * 1000),
            "stdout": "",
            "stderr": f"Shell unavailable: {exc}",
            "truncated": False,
        }


def _pipeline_trace(started_at: str, started: float, inputs_received: int, schema_valid: bool, execution_path: str, status: str, output_fields: int, command_count: int, error_code: str | None = None) -> dict:
    external_calls = {"count": command_count, "systems": ["shell"] if command_count else []}
    return make_trace(
        "tool_verification_pipeline",
        _PIPELINE_VERSION,
        started_at,
        started,
        inputs_received,
        schema_valid,
        execution_path,
        status,
        output_fields,
        external_calls,
        error_code,
    )


def _pipeline_error(error: dict, response_format: str, trace_enabled: bool, started: float, started_at: str, legacy: str):
    trace = _pipeline_trace(started_at, started, 7, False, "input_validation", "FAILED", 1, 0, error["code"])
    emit_trace(trace, trace_enabled)
    if response_format == "structured":
        return structured_error("tool_verification_pipeline", _PIPELINE_VERSION, error, started, trace)
    return legacy


def _format_legacy(result: dict) -> str:
    lines = [
        f"Status: {result['status']}",
        f"Ship decision: {result['ship_decision']}",
        f"Scope: {result['root']}",
        f"Pipeline: {result['pipeline']}",
        f"Checks considered: {result['check_count']}",
        f"Checks run: {result['run_count']}",
        f"Passed: {result['passed']}",
        f"Failed: {result['failed']}",
        f"Timed out: {result['timed_out']}",
    ]
    for index, item in enumerate(result["checks"], start=1):
        lines.append(f"[{index}] {item['status']} exit={item['exit_code']} required={item['required']} command={item['command']}")
        if item.get("reason"):
            lines.append(f"    reason: {item['reason']}")
        if item.get("stdout", "").strip():
            lines.append(f"    stdout: {item['stdout'].strip()}")
        if item.get("stderr", "").strip():
            lines.append(f"    stderr: {item['stderr'].strip()}")
    lines.append("Evidence: commands came from the markdown pipeline file; no hidden checks were added")
    return "\n".join(lines)


@tool(
    name="tool_verification_pipeline",
    description=(
        "Run KING's markdown-defined verification pipeline with bounded command "
        "timeouts and structured evidence. Use after tool, prompt, manifest, "
        "runtime, or frontend changes."
    ),
    examples=[
        "run the tool verification pipeline",
        "verify the current tool upgrade",
        "dry run the markdown verification checks",
    ],
    param_descriptions={
        "root": "Repository root for the verification scope. Defaults to current working directory.",
        "pipeline_path": "Markdown pipeline file. Defaults to KING_VERIFICATION_PIPELINE_FILE.",
        "max_steps": "Maximum markdown checks to run. Uses configured default when 0.",
        "timeout_ms": "Per-command timeout in milliseconds. Uses configured default when 0.",
        "dry_run": "When true, report planned checks without executing commands.",
        "response_format": "legacy or structured. Default legacy preserves chat-facing output.",
        "trace_enabled": "When true, emit a machine-readable trace entry.",
    },
)
def tool_verification_pipeline(
    root: str = ".",
    pipeline_path: str = "",
    max_steps: int = 0,
    timeout_ms: int = 0,
    dry_run: bool = False,
    response_format: str = "legacy",
    trace_enabled: bool = False,
):
    started = time.perf_counter()
    started_at = utc_now_iso()
    response_format = normalize_response_format(response_format)
    trace_enabled = coerce_bool(trace_enabled)
    dry_run = coerce_bool(dry_run)

    repo_root = _resolve_root(root)
    if not repo_root.exists():
        error = error_payload(
            "ROOT_NOT_FOUND",
            "The requested repository root does not exist.",
            "root",
            str(repo_root),
            "existing directory",
            False,
            "Pass an existing repository root.",
        )
        return _pipeline_error(error, response_format, trace_enabled, started, started_at, f"Status: blocked\nReason: root not found\nScope: {repo_root}")
    if not repo_root.is_dir():
        error = error_payload(
            "ROOT_NOT_DIRECTORY",
            "The requested repository root is not a directory.",
            "root",
            str(repo_root),
            "directory",
            False,
            "Pass a directory as the repository root.",
        )
        return _pipeline_error(error, response_format, trace_enabled, started, started_at, f"Status: blocked\nReason: root is not a directory\nScope: {repo_root}")

    configured_max = max(1, int(settings.verification_pipeline_max_steps))
    requested_max = max_steps
    if not isinstance(max_steps, bool):
        try:
            if int(max_steps) <= 0:
                requested_max = configured_max
        except (TypeError, ValueError):
            requested_max = max_steps
    step_limit, step_error = normalize_int(
        requested_max,
        "max_steps",
        configured_max,
        1,
        configured_max,
        "Use a max_steps value within the configured verification step limit.",
        "INVALID_STEP_LIMIT",
    )
    if step_error is not None:
        return _pipeline_error(step_error, response_format, trace_enabled, started, started_at, "Status: blocked\nReason: invalid max_steps")

    timeout_value, timeout_error = normalize_timeout_ms(
        timeout_ms,
        max(1, int(settings.verification_pipeline_timeout_ms)),
    )
    if timeout_error is not None:
        return _pipeline_error(timeout_error, response_format, trace_enabled, started, started_at, "Status: blocked\nReason: invalid timeout_ms")

    output_limit, output_error = normalize_int(
        settings.verification_pipeline_output_chars,
        "verification_pipeline_output_chars",
        4000,
        500,
        20000,
        "Set KING_VERIFICATION_PIPELINE_OUTPUT_CHARS between 500 and 20000.",
        "INVALID_OUTPUT_LIMIT",
    )
    if output_error is not None:
        return _pipeline_error(output_error, response_format, trace_enabled, started, started_at, "Status: blocked\nReason: invalid output limit")

    pipeline_file = _resolve_pipeline_path(repo_root, pipeline_path)
    if not _is_inside(pipeline_file, repo_root):
        error = error_payload(
            "PIPELINE_OUT_OF_SCOPE",
            "The pipeline file must stay inside the requested repository root.",
            "pipeline_path",
            str(pipeline_file),
            "path inside repository root",
            False,
            "Move the pipeline file inside the repository or pass the correct root.",
        )
        return _pipeline_error(error, response_format, trace_enabled, started, started_at, f"Status: blocked\nReason: pipeline out of scope\nPipeline: {pipeline_file}")
    if not pipeline_file.exists():
        error = error_payload(
            "PIPELINE_NOT_FOUND",
            "The markdown verification pipeline file does not exist.",
            "pipeline_path",
            str(pipeline_file),
            "existing markdown file",
            False,
            "Create the pipeline file or pass a valid pipeline_path.",
        )
        return _pipeline_error(error, response_format, trace_enabled, started, started_at, f"Status: blocked\nReason: pipeline not found\nPipeline: {pipeline_file}")

    try:
        text = pipeline_file.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        error = error_payload(
            "PIPELINE_DECODE_FAILED",
            "The pipeline file could not be decoded as UTF-8.",
            "pipeline_path",
            str(pipeline_file),
            "UTF-8 markdown file",
            False,
            "Save the pipeline file as UTF-8 markdown.",
        )
        return _pipeline_error(error, response_format, trace_enabled, started, started_at, f"Status: blocked\nReason: pipeline decode failed\nPipeline: {pipeline_file}")

    steps = _parse_pipeline(text, step_limit)
    if not steps:
        error = error_payload(
            "NO_PIPELINE_COMMANDS",
            "No markdown command entries were found in the pipeline file.",
            "pipeline_path",
            str(pipeline_file),
            "lines beginning with '- command:'",
            False,
            "Add '- command:' entries to the markdown pipeline.",
        )
        return _pipeline_error(error, response_format, trace_enabled, started, started_at, f"Status: blocked\nReason: no commands\nPipeline: {pipeline_file}")

    checks = []
    if dry_run:
        for item in steps:
            checks.append({
                "command": item["command"],
                "required": item["required"],
                "reason": item["reason"],
                "status": "DRY_RUN",
                "exit_code": None,
                "duration_ms": 0,
                "stdout": "",
                "stderr": "",
                "truncated": False,
            })
    else:
        for item in steps:
            checks.append(
                _run_step(
                    item["command"],
                    repo_root,
                    timeout_value,
                    output_limit,
                    item["required"],
                    item["reason"],
                )
            )

    passed = len([item for item in checks if item["status"] == "PASS"])
    failed_required = len([item for item in checks if item["required"] and item["status"] not in ("PASS", "DRY_RUN")])
    failed_optional = len([item for item in checks if not item["required"] and item["status"] not in ("PASS", "DRY_RUN")])
    timed_out = len([item for item in checks if item["status"] == "TIMEOUT"])
    if dry_run:
        status = "dry_run"
        ship_decision = "hold"
    elif failed_required:
        status = "failed"
        ship_decision = "hold"
    elif failed_optional:
        status = "partial"
        ship_decision = "hold"
    else:
        status = "success"
        ship_decision = "ship"

    result = {
        "status": status,
        "ship_decision": ship_decision,
        "root": str(repo_root),
        "pipeline": str(pipeline_file),
        "check_count": len(steps),
        "run_count": 0 if dry_run else len(checks),
        "passed": passed,
        "failed": failed_required + failed_optional,
        "timed_out": timed_out,
        "timeout_ms": timeout_value,
        "checks": checks,
    }
    trace_status = "SUCCESS" if status in ("success", "dry_run") else "FAILED"
    command_count = 0 if dry_run else len(checks)
    trace = _pipeline_trace(started_at, started, 7, True, "dry_run" if dry_run else "shell_pipeline", trace_status, len(result), command_count)
    emit_trace(trace, trace_enabled)
    if response_format == "structured":
        return structured_success("tool_verification_pipeline", _PIPELINE_VERSION, result, started, trace)
    return _format_legacy(result)
