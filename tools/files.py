import mimetypes
import tempfile
from pathlib import Path

from tools.registry import tool


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
    },
)
def file_read(path: str, max_chars: int = 3000) -> str:
    try:
        max_chars = max(500, min(max_chars, 20000))
        p = _resolve(path)
        if not p.exists():
            return f"Not found: {path}"
        if not p.is_file():
            return f"Not a file: {path}"
        if _is_binary(p):
            return f"{_metadata(p)}\nCannot read binary file content"
        text = p.read_text(encoding="utf-8")
        if len(text) > max_chars:
            text = text[:max_chars] + "\n...[truncated]"
        return f"{_metadata(p)}\n\n{text}"
    except UnicodeDecodeError:
        return f"Cannot decode file as UTF-8: {path}"
    except Exception as e:
        return f"Error reading file: {e}"


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
    },
)
def file_write(path: str, content: str, mode: str = "overwrite") -> str:
    try:
        mode = mode.strip().lower()
        if mode not in ("overwrite", "append", "create_new"):
            return "Invalid mode. Use overwrite, append, or create_new"
        p = _resolve(path)
        existed_before = p.exists()
        if mode == "create_new" and p.exists():
            return f"File already exists: {p}"
        p.parent.mkdir(parents=True, exist_ok=True)
        if not p.parent.is_dir():
            return f"Parent is not a directory: {p.parent}"
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
        return f"{action}: {p}\nMode: {mode}\nExisted before: {existed}\nSize: {_fmt(p.stat().st_size)}"
    except Exception as e:
        return f"Error writing file: {e}"


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
    },
)
def file_list(directory: str = ".", include_hidden: bool = False, limit: int = 100) -> str:
    try:
        limit = max(1, min(limit, 200))
        p = _resolve(directory) if directory else Path(".").resolve()
        if not p.exists():
            return f"Directory not found: {directory}"
        if not p.is_dir():
            return f"Not a directory: {directory}"
        items = sorted(p.iterdir())
        if not include_hidden:
            items = [item for item in items if not item.name.startswith(".")]
        if not items:
            return "Empty"
        result = []
        for item in items[:limit]:
            size = item.stat().st_size if item.is_file() else 0
            name = f"{item.name}/" if item.is_dir() else item.name
            if item.is_file():
                result.append(f"{name}  ({_fmt(size)})")
            else:
                result.append(f"{name}")
        if len(items) > limit:
            result.append(f"...[{len(items) - limit} more]")
        return "\n".join(result)
    except Exception as e:
        return f"Error listing directory: {e}"


def _fmt(n):
    for unit in ("B", "KB", "MB"):
        if n < 1024:
            return f"{n:.0f}{unit}"
        n /= 1024
    return f"{n:.1f}GB"
