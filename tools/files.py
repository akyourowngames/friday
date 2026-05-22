import mimetypes
import tempfile
import time
from pathlib import Path

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


_FILE_READ_VERSION = "2.0.0"
_FILE_WRITE_VERSION = "2.0.0"
_FILE_LIST_VERSION = "2.0.0"


def _is_binary(path: Path) -> bool:
    mime, _ = mimetypes.guess_type(str(path))
    if mime and not mime.startswith("text/"):
        return True
    EXT_BINARY = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico", ".pdf", ".zip", ".exe", ".dll", ".so", ".bin"}
    return path.suffix.lower() in EXT_BINARY


def _resolve(path: str) -> Path:
    return Path(path).expanduser().resolve()


def _metadata(path: Path) -> str:
    stat = path.stat()
    kind = "directory" if path.is_dir() else "file"
    return f"Path: {path}\nType: {kind}\nSize: {_fmt(stat.st_size)}\nModified: {stat.st_mtime:.0f}"


def _file_trace(tool_name: str, version: str, started_at: str, started: float, inputs_received: int, schema_valid: bool, execution_path: str, status: str, output_fields: int, error_code: str | None = None) -> dict:
    return make_trace(
        tool_name,
        version,
        started_at,
        started,
        inputs_received,
        schema_valid,
        execution_path,
        status,
        output_fields,
        {"count": 1, "systems": ["filesystem"]},
        error_code,
    )


def _file_error(tool_name: str, version: str, error: dict, response_format: str, trace_enabled: bool, started: float, started_at: str, inputs_received: int, legacy: str, schema_valid: bool = False, execution_path: str = "input_validation"):
    trace = _file_trace(tool_name, version, started_at, started, inputs_received, schema_valid, execution_path, "FAILED", 1, error["code"])
    emit_trace(trace, trace_enabled)
    if response_format == "structured":
        return structured_error(tool_name, version, error, started, trace)
    return legacy


def _write_text_atomic(path: Path, content: str):
    tmp_name = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as tmp:
            tmp.write(content)
            tmp_name = tmp.name
        Path(tmp_name).replace(path)
    finally:
        if tmp_name:
            tmp_path = Path(tmp_name)
            if tmp_path.exists():
                tmp_path.unlink(missing_ok=True)


def _write_trace(started_at: str, started: float, inputs_received: int, schema_valid: bool, execution_path: str, status: str, output_fields: int, error_code: str | None = None) -> dict:
    return make_trace(
        "file_write",
        _FILE_WRITE_VERSION,
        started_at,
        started,
        inputs_received,
        schema_valid,
        execution_path,
        status,
        output_fields,
        {"count": 1, "systems": ["filesystem"]},
        error_code,
    )


def _file_write_error(error: dict, response_format: str, trace_enabled: bool, started: float, started_at: str, inputs_received: int, legacy: str, schema_valid: bool = False):
    trace = _write_trace(started_at, started, inputs_received, schema_valid, "input_validation" if not schema_valid else "write", "FAILED", 1, error["code"])
    emit_trace(trace, trace_enabled)
    if response_format == "structured":
        return structured_error("file_write", _FILE_WRITE_VERSION, error, started, trace)
    return legacy


@tool(
    name="file_read",
    description="Read the contents of any text file",
    examples=[
        "read config.py",
        "show me the contents of main.py",
        "open requirements.txt",
    ],
    param_descriptions={
        "path": "Text file path to read",
        "max_chars": "Maximum characters to return, from 500 to 20000",
        "response_format": "legacy or structured. Default legacy preserves existing output",
        "trace_enabled": "When true, emit a machine-readable trace entry",
    },
)
def file_read(path: str, max_chars: int = 3000, response_format: str = "legacy", trace_enabled: bool = False):
    started = time.perf_counter()
    started_at = utc_now_iso()
    inputs_received = 4
    response_format = normalize_response_format(response_format)
    trace_enabled = coerce_bool(trace_enabled)
    try:
        if not str(path or "").strip():
            error = error_payload(
                "EMPTY_PATH",
                "path must not be empty.",
                "path",
                path,
                "non-empty text file path",
                False,
                "Pass the exact file path to read.",
            )
            return _file_error("file_read", _FILE_READ_VERSION, error, response_format, trace_enabled, started, started_at, inputs_received, "Not found: empty path")
        max_chars, max_error = normalize_int(
            max_chars,
            "max_chars",
            3000,
            500,
            20000,
            "Use a max_chars value between 500 and 20000.",
            "INVALID_MAX_CHARS",
        )
        if max_error is not None:
            return _file_error("file_read", _FILE_READ_VERSION, max_error, response_format, trace_enabled, started, started_at, inputs_received, "Error reading file: invalid max_chars")
        p = _resolve(path)
        if not p.exists():
            error = error_payload(
                "FILE_NOT_FOUND",
                "The requested file does not exist.",
                "path",
                str(p),
                "existing text file",
                False,
                "Pass an existing file path.",
            )
            return _file_error("file_read", _FILE_READ_VERSION, error, response_format, trace_enabled, started, started_at, inputs_received, f"Not found: {path}")
        if not p.is_file():
            error = error_payload(
                "NOT_A_FILE",
                "The requested path is not a file.",
                "path",
                str(p),
                "file path",
                False,
                "Pass a path to a file, not a directory.",
            )
            return _file_error("file_read", _FILE_READ_VERSION, error, response_format, trace_enabled, started, started_at, inputs_received, f"Not a file: {path}")
        if _is_binary(p):
            result = {
                "path": str(p),
                "kind": "file",
                "size_bytes": p.stat().st_size,
                "binary": True,
                "text": "",
                "truncated": False,
                "readable": False,
            }
            trace = _file_trace("file_read", _FILE_READ_VERSION, started_at, started, inputs_received, True, "read", "PARTIAL", len(result), "BINARY_FILE")
            emit_trace(trace, trace_enabled)
            if response_format == "structured":
                return structured_success("file_read", _FILE_READ_VERSION, result, started, trace)
            return f"{_metadata(p)}\nCannot read binary file content"
        text = p.read_text(encoding="utf-8")
        truncated = len(text) > max_chars
        if len(text) > max_chars:
            text = text[:max_chars] + "\n...[truncated]"
        result = {
            "path": str(p),
            "kind": "file",
            "size_bytes": p.stat().st_size,
            "binary": False,
            "text": text,
            "truncated": truncated,
            "readable": True,
        }
        trace = _file_trace("file_read", _FILE_READ_VERSION, started_at, started, inputs_received, True, "read", "SUCCESS", len(result))
        emit_trace(trace, trace_enabled)
        if response_format == "structured":
            return structured_success("file_read", _FILE_READ_VERSION, result, started, trace)
        return f"{_metadata(p)}\n\n{text}"
    except UnicodeDecodeError:
        error = error_payload(
            "DECODE_FAILED",
            "The file could not be decoded as UTF-8.",
            "path",
            path,
            "UTF-8 text file",
            False,
            "Use a text file encoded as UTF-8 or another tool for binary content.",
        )
        return _file_error("file_read", _FILE_READ_VERSION, error, response_format, trace_enabled, started, started_at, inputs_received, f"Cannot decode file as UTF-8: {path}", True, "read")
    except Exception as e:
        error = error_payload(
            "READ_FAILED",
            "The file read failed before completion.",
            "path",
            path,
            "successful file read",
            True,
            "Verify the path and retry if still needed.",
        )
        return _file_error("file_read", _FILE_READ_VERSION, error, response_format, trace_enabled, started, started_at, inputs_received, f"Error reading file: {e.__class__.__name__}", True, "read")


@tool(
    name="file_write",
    description="Create or overwrite a file with content",
    examples=[
        "create a file called hello.py with print('hello')",
        "save this code to script.py",
        "append this line to notes.txt",
    ],
    param_descriptions={
        "path": "File path to write",
        "content": "Text content to write",
        "mode": "overwrite (default), append, or create_new",
        "dry_run": "When true, report the planned write without changing the filesystem",
        "create_parent_dirs": "When true, create missing parent directories. Default true preserves existing behavior",
        "response_format": "legacy or structured. Default legacy preserves existing output",
        "trace_enabled": "When true, emit a machine-readable trace entry",
    },
)
def file_write(
    path: str,
    content: str,
    mode: str = "overwrite",
    dry_run: bool = False,
    create_parent_dirs: bool = True,
    response_format: str = "legacy",
    trace_enabled: bool = False,
):
    started = time.perf_counter()
    started_at = utc_now_iso()
    inputs_received = 7
    response_format = normalize_response_format(response_format)
    trace_enabled = coerce_bool(trace_enabled)
    dry_run = coerce_bool(dry_run)
    create_parent_dirs = coerce_bool(create_parent_dirs)
    try:
        if not str(path or "").strip():
            error = error_payload(
                "EMPTY_PATH",
                "path must not be empty.",
                "path",
                path,
                "non-empty file path",
                False,
                "Pass the exact file path to write.",
            )
            return _file_write_error(
                error,
                response_format,
                trace_enabled,
                started,
                started_at,
                inputs_received,
                "Error writing file: path is required",
            )
        mode = mode.strip().lower()
        if mode not in ("overwrite", "append", "create_new"):
            error = error_payload(
                "INVALID_WRITE_MODE",
                "mode must be overwrite, append, or create_new.",
                "mode",
                mode,
                "overwrite, append, or create_new",
                False,
                "Use overwrite, append, or create_new.",
            )
            return _file_write_error(
                error,
                response_format,
                trace_enabled,
                started,
                started_at,
                inputs_received,
                "Invalid mode. Use overwrite, append, or create_new",
            )
        p = _resolve(path)
        existed_before = p.exists()
        if mode == "create_new" and p.exists():
            error = error_payload(
                "FILE_ALREADY_EXISTS",
                "create_new mode refuses to overwrite an existing file.",
                "path",
                str(p),
                "path that does not already exist",
                False,
                "Use overwrite mode only when replacing the file is intended.",
            )
            return _file_write_error(
                error,
                response_format,
                trace_enabled,
                started,
                started_at,
                inputs_received,
                f"File already exists: {p}",
            )
        parent_existed_before = p.parent.exists()
        if not parent_existed_before and not create_parent_dirs:
            error = error_payload(
                "PARENT_DIRECTORY_NOT_FOUND",
                "The parent directory does not exist and create_parent_dirs is false.",
                "path",
                str(p.parent),
                "existing parent directory",
                False,
                "Create the parent directory first or set create_parent_dirs to true.",
            )
            return _file_write_error(
                error,
                response_format,
                trace_enabled,
                started,
                started_at,
                inputs_received,
                f"Parent directory not found: {p.parent}",
            )
        content_bytes = len(content.encode("utf-8"))
        planned = {
            "path": str(p),
            "mode": mode,
            "existed_before": existed_before,
            "parent_created": False,
            "bytes_requested": content_bytes,
            "bytes_written": 0,
            "dry_run": dry_run,
            "changed": False,
        }
        if dry_run:
            trace = _write_trace(started_at, started, inputs_received, True, "dry_run", "SUCCESS", len(planned))
            emit_trace(trace, trace_enabled)
            if response_format == "structured":
                return structured_success("file_write", _FILE_WRITE_VERSION, planned, started, trace)
            return f"Dry run: would write to {p}\nMode: {mode}\nExisted before: {'yes' if existed_before else 'no'}\nSize: {_fmt(content_bytes)}"
        if create_parent_dirs:
            p.parent.mkdir(parents=True, exist_ok=True)
        if not p.parent.is_dir():
            error = error_payload(
                "PARENT_NOT_DIRECTORY",
                "The resolved parent path is not a directory.",
                "path",
                str(p.parent),
                "directory parent",
                False,
                "Choose a file path with a directory parent.",
            )
            return _file_write_error(
                error,
                response_format,
                trace_enabled,
                started,
                started_at,
                inputs_received,
                f"Parent is not a directory: {p.parent}",
            )
        if mode == "append":
            with p.open("a", encoding="utf-8") as f:
                f.write(content)
        elif mode == "create_new":
            with p.open("x", encoding="utf-8") as f:
                f.write(content)
        else:
            _write_text_atomic(p, content)
        action = "Appended to" if mode == "append" else "Written to"
        existed = "yes" if existed_before else "no"
        final_size = p.stat().st_size
        result = {
            "path": str(p),
            "mode": mode,
            "existed_before": existed_before,
            "parent_created": not parent_existed_before and p.parent.exists(),
            "bytes_requested": content_bytes,
            "bytes_written": content_bytes,
            "dry_run": False,
            "changed": True,
            "size_bytes": final_size,
        }
        trace = _write_trace(started_at, started, inputs_received, True, mode, "SUCCESS", len(result))
        emit_trace(trace, trace_enabled)
        if response_format == "structured":
            return structured_success("file_write", _FILE_WRITE_VERSION, result, started, trace)
        return f"{action}: {p}\nMode: {mode}\nExisted before: {existed}\nSize: {_fmt(p.stat().st_size)}"
    except PermissionError as e:
        error = error_payload(
            "PERMISSION_DENIED",
            "The file write was blocked by filesystem permissions.",
            "path",
            path,
            "writable target path",
            False,
            "Choose a writable path or adjust permissions before retrying.",
        )
        return _file_write_error(
            error,
            response_format,
            trace_enabled,
            started,
            started_at,
            inputs_received,
            f"Error writing file: {e}",
            True,
        )
    except Exception as e:
        error = error_payload(
            "WRITE_FAILED",
            "The file write failed before completion.",
            "path",
            path,
            "successful UTF-8 file write",
            True,
            "Verify the target path and retry only if another write is safe.",
        )
        return _file_write_error(
            error,
            response_format,
            trace_enabled,
            started,
            started_at,
            inputs_received,
            f"Error writing file: {e.__class__.__name__}",
            True,
        )


@tool(
    name="file_list",
    description="List files and folders in a directory (defaults to current folder)",
    examples=[
        "what files are here",
        "list the current folder",
        "what is in the tools directory",
    ],
    param_descriptions={
        "directory": "Directory to list",
        "include_hidden": "Whether to include hidden files",
        "limit": "Maximum number of entries, from 1 to 200",
        "response_format": "legacy or structured. Default legacy preserves existing output",
        "trace_enabled": "When true, emit a machine-readable trace entry",
    },
)
def file_list(
    directory: str = ".",
    include_hidden: bool = False,
    limit: int = 100,
    response_format: str = "legacy",
    trace_enabled: bool = False,
):
    started = time.perf_counter()
    started_at = utc_now_iso()
    inputs_received = 5
    response_format = normalize_response_format(response_format)
    trace_enabled = coerce_bool(trace_enabled)
    include_hidden = coerce_bool(include_hidden)
    try:
        limit, limit_error = normalize_int(
            limit,
            "limit",
            100,
            1,
            200,
            "Use a limit value between 1 and 200.",
            "INVALID_LIMIT",
        )
        if limit_error is not None:
            return _file_error("file_list", _FILE_LIST_VERSION, limit_error, response_format, trace_enabled, started, started_at, inputs_received, "Error listing directory: invalid limit")
        p = _resolve(directory) if directory else Path(".").resolve()
        if not p.exists():
            error = error_payload(
                "DIRECTORY_NOT_FOUND",
                "The requested directory does not exist.",
                "directory",
                str(p),
                "existing directory",
                False,
                "Pass an existing directory path.",
            )
            return _file_error("file_list", _FILE_LIST_VERSION, error, response_format, trace_enabled, started, started_at, inputs_received, f"Directory not found: {directory}")
        if not p.is_dir():
            error = error_payload(
                "NOT_A_DIRECTORY",
                "The requested path is not a directory.",
                "directory",
                str(p),
                "directory path",
                False,
                "Pass a path to a directory.",
            )
            return _file_error("file_list", _FILE_LIST_VERSION, error, response_format, trace_enabled, started, started_at, inputs_received, f"Not a directory: {directory}")
        items = sorted(p.iterdir())
        if not include_hidden:
            items = [item for item in items if not item.name.startswith(".")]
        if not items:
            result = {
                "directory": str(p),
                "include_hidden": include_hidden,
                "limit": limit,
                "items": [],
                "count": 0,
                "truncated": False,
            }
            trace = _file_trace("file_list", _FILE_LIST_VERSION, started_at, started, inputs_received, True, "list", "SUCCESS", len(result))
            emit_trace(trace, trace_enabled)
            if response_format == "structured":
                return structured_success("file_list", _FILE_LIST_VERSION, result, started, trace)
            return "Empty"
        result = []
        structured_items = []
        for item in items[:limit]:
            size = item.stat().st_size if item.is_file() else 0
            name = f"{item.name}/" if item.is_dir() else item.name
            structured_items.append(
                {
                    "name": item.name,
                    "path": str(item),
                    "kind": "directory" if item.is_dir() else "file",
                    "size_bytes": size,
                }
            )
            if item.is_file():
                result.append(f"{name}  ({_fmt(size)})")
            else:
                result.append(f"{name}")
        if len(items) > limit:
            result.append(f"...[{len(items) - limit} more]")
        payload = {
            "directory": str(p),
            "include_hidden": include_hidden,
            "limit": limit,
            "items": structured_items,
            "count": len(structured_items),
            "total_count": len(items),
            "truncated": len(items) > limit,
        }
        trace = _file_trace("file_list", _FILE_LIST_VERSION, started_at, started, inputs_received, True, "list", "SUCCESS", len(payload))
        emit_trace(trace, trace_enabled)
        if response_format == "structured":
            return structured_success("file_list", _FILE_LIST_VERSION, payload, started, trace)
        return "\n".join(result)
    except Exception as e:
        error = error_payload(
            "LIST_FAILED",
            "The directory listing failed before completion.",
            "directory",
            directory,
            "successful directory listing",
            True,
            "Verify the path and retry if still needed.",
        )
        return _file_error("file_list", _FILE_LIST_VERSION, error, response_format, trace_enabled, started, started_at, inputs_received, f"Error listing directory: {e.__class__.__name__}", True, "list")


def _fmt(n):
    for unit in ("B", "KB", "MB"):
        if n < 1024:
            return f"{n:.0f}{unit}"
        n /= 1024
    return f"{n:.1f}GB"
