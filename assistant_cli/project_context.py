from __future__ import annotations

from pathlib import Path

from .config import Settings
from .project_store import ProjectStore, project_db_path


def project_prompt_context(settings: Settings, workspace_root: Path | None = None) -> str:
    path = project_db_path(settings, workspace_root)
    if not path.exists():
        return ""
    try:
        snapshot = ProjectStore(path).summary(limit_projects=5, limit_tasks=12)
    except Exception:
        return ""
    projects = snapshot.get("projects", [])
    tasks = snapshot.get("open_tasks", [])
    if not projects and not tasks:
        return ""

    lines = [
        "Local project management context from Friday's SQLite project database.",
        "Use this for project and task questions. Do not dump every row unless the user asks for details.",
        "This is a read-only snapshot: never claim a project/task was created, updated, completed, deleted, or verified unless a project_manage tool result for the current turn proves it.",
        "For follow-up requests like marking them done or double-checking task state, use project_manage instead of answering from this snapshot alone.",
    ]
    if projects:
        lines.append("Active projects:")
        for project in projects[:5]:
            lines.append(
                "- "
                f"{project['name']} ({project['status']}): "
                f"{project.get('open_tasks', 0)} open, {project.get('done_tasks', 0)} done"
            )
    if tasks:
        lines.append("Important open tasks:")
        for task in tasks[:12]:
            due = f", due {task['due_at']}" if task.get("due_at") else ""
            lines.append(
                "- "
                f"[{task['priority']}/{task['status']}] "
                f"{task['project_name']}: {task['title']}{due}"
            )
    return "\n".join(lines)
