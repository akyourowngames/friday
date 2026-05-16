import mimetypes
import os
from pathlib import Path

from tools.registry import tool


def _is_binary(path: Path) -> bool:
    mime, _ = mimetypes.guess_type(str(path))
    if mime and not mime.startswith("text/"):
        return True
    EXT_BINARY = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico", ".pdf", ".zip", ".exe", ".dll", ".so", ".bin"}
    return path.suffix.lower() in EXT_BINARY


@tool(
    name="file_read",
    description="Read the contents of any text file",
    examples=[
        "read config.py",
        "show me the contents of main.py",
        "open requirements.txt",
    ],
)
def file_read(path: str) -> str:
    try:
        p = Path(path).resolve()
        if not p.exists():
            return f"Not found: {path}"
        if not p.is_file():
            return f"Not a file: {path}"
        if _is_binary(p):
            return f"Cannot read binary file: {path}"
        return p.read_text(encoding="utf-8")
    except Exception:
        return "Error reading file"


@tool(
    name="file_write",
    description="Create or overwrite a file with content",
    examples=[
        "create a file called hello.py with print('hello')",
        "save this code to script.py",
    ],
)
def file_write(path: str, content: str) -> str:
    try:
        p = Path(path).resolve()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return f"Written to {path}"
    except Exception:
        return "Error writing file"


@tool(
    name="file_list",
    description="List files and folders in a directory (defaults to current folder)",
    examples=[
        "what files are here",
        "list the current folder",
        "what is in the tools directory",
    ],
)
def file_list(directory: str = ".") -> str:
    try:
        p = Path(directory).resolve() if directory else Path(".").resolve()
        if not p.exists():
            return f"Directory not found: {directory}"
        items = sorted(p.iterdir())
        if not items:
            return "Empty"
        result = []
        for item in items:
            size = item.stat().st_size if item.is_file() else 0
            name = f"{item.name}/" if item.is_dir() else item.name
            if item.is_file():
                result.append(f"{name}  ({_fmt(size)})")
            else:
                result.append(f"{name}")
        return "\n".join(result)
    except Exception:
        return "Error listing directory"


def _fmt(n):
    for unit in ("B", "KB", "MB"):
        if n < 1024:
            return f"{n:.0f}{unit}"
        n /= 1024
    return f"{n:.1f}GB"
