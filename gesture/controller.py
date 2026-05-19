import os
import shutil
from pathlib import Path

from .config import GESTURES

_CWD = Path.cwd().resolve()
_FILE_LIST = []
_LIST_INDEX = 0


def _list_dir():
    global _FILE_LIST, _LIST_INDEX
    items = sorted(_CWD.iterdir())
    _FILE_LIST = items
    _LIST_INDEX = 0
    return _format_list(items)


def _format_list(items):
    lines = [f"[DIR] {_CWD}"]
    lines.append("-" * 50)
    lines.append("  [point=down] [thumbs_down=up] [pinch=enter] [peace=open]")
    lines.append("  [fist=delete] [thumbs_up=parent] [open_palm=refresh]")
    lines.append("")
    for i, item in enumerate(items):
        marker = ">" if i == _LIST_INDEX else " "
        icon = "[DIR]" if item.is_dir() else "     "
        name = f"{item.name}/" if item.is_dir() else item.name
        lines.append(f" {marker} {icon} {name}")
    return "\n".join(lines)


def execute(gesture_name: str) -> str | None:
    global _CWD, _FILE_LIST, _LIST_INDEX

    if gesture_name == "none":
        return None

    mapping = {
        "open_palm": _on_open_palm,
        "fist": _on_fist,
        "point": _on_point,
        "peace": _on_peace,
        "pinch": _on_pinch,
        "thumbs_up": _on_thumbs_up,
        "thumbs_down": _on_thumbs_down,
    }

    handler = mapping.get(gesture_name)
    if handler:
        return handler()
    return None


def _ensure_list():
    if not _FILE_LIST:
        _list_dir()


def _on_open_palm():
    return _list_dir()


def _on_fist():
    _ensure_list()
    if not _FILE_LIST:
        return "Directory is empty"
    item = _FILE_LIST[_LIST_INDEX]
    if item.is_dir():
        return "Cannot delete a directory"
    try:
        os.remove(item)
        msg = f"Deleted: {item.name}"
        _list_dir()
        return msg
    except Exception as e:
        return f"Error deleting {item.name}: {e}"


def _on_point():
    global _LIST_INDEX
    _ensure_list()
    if not _FILE_LIST:
        return None
    _LIST_INDEX = (_LIST_INDEX + 1) % len(_FILE_LIST)
    item = _FILE_LIST[_LIST_INDEX]
    return _format_list(_FILE_LIST)


def _on_peace():
    _ensure_list()
    if not _FILE_LIST:
        return None
    item = _FILE_LIST[_LIST_INDEX]
    try:
        os.startfile(item)
        return f"Opened: {item.name}"
    except Exception as e:
        return f"Error opening {item.name}: {e}"


def _on_pinch():
    _ensure_list()
    if not _FILE_LIST:
        return None
    item = _FILE_LIST[_LIST_INDEX]
    if item.is_dir():
        global _CWD
        _CWD = item.resolve()
        return _list_dir()
    else:
        try:
            os.startfile(item)
            return f"Opened: {item.name}"
        except Exception as e:
            return f"Error: {e}"


def _on_thumbs_up():
    global _CWD, _FILE_LIST, _LIST_INDEX
    parent = _CWD.parent
    if parent != _CWD:
        _CWD = parent
    return _list_dir()


def _on_thumbs_down():
    global _LIST_INDEX
    _ensure_list()
    if not _FILE_LIST:
        return None
    _LIST_INDEX = (_LIST_INDEX - 1) % len(_FILE_LIST)
    item = _FILE_LIST[_LIST_INDEX]
    return _format_list(_FILE_LIST)
