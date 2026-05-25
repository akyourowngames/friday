from __future__ import annotations

from pathlib import Path


def load_status(repo_root: str | Path = ".", status_path: str | Path = "tools/FOLDER_WATCHER_STATUS.md") -> dict:
    root = Path(repo_root).expanduser().resolve()
    path = Path(status_path).expanduser()
    if not path.is_absolute():
        path = root / path
    path = path.resolve()
    result = {
        "path": str(path),
        "implemented": [],
        "partial": [],
        "planned": [],
        "how_to_see_it": [],
    }
    if not path.exists():
        result["status"] = "missing"
        return result
    section = ""
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if line.startswith("## "):
            section = line[3:].strip().lower().replace(" ", "_")
            continue
        if not line.startswith("- "):
            continue
        item = line[2:].strip()
        if section in result and isinstance(result[section], list):
            result[section].append(item)
    result["status"] = "ok"
    result["summary"] = {
        "implemented": len(result["implemented"]),
        "partial": len(result["partial"]),
        "planned": len(result["planned"]),
    }
    return result
