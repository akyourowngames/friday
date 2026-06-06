from __future__ import annotations

import json
from typing import Any

from assistant_cli.project_store import ProjectStore, project_db_path

from .args import relative_path, str_arg
from .core import JsonObject, ToolContext, ToolResult, ToolSpec, fail, ok, schema


ACTIONS = (
    "project_create",
    "project_list",
    "project_update",
    "project_archive",
    "task_create",
    "task_create_many",
    "task_list",
    "task_update",
    "task_bulk_update",
    "task_complete",
    "task_complete_all",
    "task_pending",
    "task_pending_all",
    "task_delete",
    "task_delete_many",
    "subtask_add",
    "note_add",
    "activity",
    "summary",
)


SPEC = ToolSpec(
    name="project_manage",
    description=(
        "Manage local projects and tasks in SQLite: create projects, create/list/update/complete/pending/delete tasks, "
        "create many tasks in one call, bulk-update priorities/dates/statuses, bulk-complete or reopen tasks, "
        "add subtasks and notes, inspect activity history, and summarize multiple active projects."
    ),
    parameters=schema(
        {
            "action": {
                "type": "string",
                "enum": list(ACTIONS),
                "description": "Project/task action to run.",
            },
            "project": {
                "type": "string",
                "description": "Project name or project_id. Required for task_create and project-specific lists.",
            },
            "project_id": {"type": "string", "description": "Project id. Optional alternative to project."},
            "name": {"type": "string", "description": "Project name for project_create or project_update."},
            "description": {"type": "string", "description": "Project or task description."},
            "task_id": {"type": "string", "description": "Task id for update/status/delete/note/subtask actions."},
            "task": {"type": "string", "description": "Task title or partial title when task_id is not known."},
            "task_ids": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Task ids or exact titles for batch update/delete operations.",
            },
            "all": {"type": "boolean", "description": "Set true when the user asks to update all matching tasks."},
            "title": {"type": "string", "description": "Task or subtask title."},
            "tasks": {
                "type": "array",
                "description": "Tasks to create together. Use for action task_create_many.",
                "items": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string"},
                        "description": {"type": "string"},
                        "status": {
                            "type": "string",
                            "enum": ["pending", "in_progress", "blocked", "done", "archived"],
                        },
                        "priority": {"type": "string", "enum": ["low", "normal", "high", "urgent"]},
                        "due": {"type": "string"},
                        "tags": {"type": "array", "items": {"type": "string"}},
                        "recurrence": {"type": "string"},
                    },
                    "required": ["title"],
                    "additionalProperties": False,
                },
            },
            "status": {
                "type": "string",
                "enum": ["pending", "in_progress", "blocked", "done", "archived"],
                "description": "Project or task status.",
            },
            "match_status": {
                "type": "string",
                "enum": ["pending", "in_progress", "blocked", "done", "archived"],
                "description": "Optional current-status filter for task_bulk_update.",
            },
            "priority": {
                "type": "string",
                "enum": ["low", "normal", "high", "urgent"],
                "description": "Task priority.",
            },
            "due": {
                "type": "string",
                "description": "Due date/time such as tomorrow 5pm, next Friday, or an ISO date/time.",
            },
            "recurrence": {
                "type": "string",
                "description": "Optional recurrence text such as daily, weekdays, weekly, or every Friday.",
            },
            "tags": {"type": "array", "items": {"type": "string"}, "description": "Task tags."},
            "parent_task_id": {"type": "string", "description": "Parent task id for subtask_add."},
            "parent_task": {"type": "string", "description": "Parent task title or id for subtask_add."},
            "note": {"type": "string", "description": "Task note text for note_add."},
            "include_done": {"type": "boolean", "description": "Include completed tasks in task_list."},
            "include_archived": {"type": "boolean", "description": "Include archived projects or tasks."},
            "limit": {"type": "integer", "description": "Maximum rows to return."},
        },
        required=("action",),
    ),
    examples=(
        'project_manage action=project_create name=Friday',
        'project_manage action=task_complete_all project=Friday',
        'project_manage action=task_create project=Friday title="fix voice latency" priority=high due=tomorrow',
        'project_manage action=task_create_many project=Friday tasks=[{"title":"Tool sanity"},{"title":"JSONL audit"}]',
        'project_manage action=task_bulk_update project=Friday match_status=pending priority=high due="tomorrow 5pm"',
        'project_manage action=task_complete task_id=task_abc123',
        'project_manage action=task_list project=Friday status=pending',
        'project_manage action=summary',
        "list me my projects",
        "list pending Friday tasks",
        "add a task in Friday to test weather tool",
        "add a Friday task called realtime voice smoke test",
        "mark all tasks done in Friday",
        "mark them done",
        "mark it done",
        "mark that task done",
        "double check the Friday tasks",
        "yeah double check pls",
        "check those tasks again",
        "what tasks are pending for Friday?",
    ),
    auto_route=False,
)


def run(ctx: ToolContext, args: JsonObject) -> ToolResult:
    action = _normalize_action(str_arg(args, "action"))
    if action not in ACTIONS:
        return fail(
            "project_manage",
            f"action must be one of: {', '.join(ACTIONS)}",
            {"actions": list(ACTIONS), "received_action": action},
        )

    path = project_db_path(ctx.settings, ctx.workspace_root)
    store = ProjectStore(path)
    try:
        data = _run_action(store, action, args)
    except Exception as exc:
        return fail("project_manage", str(exc), {"action": action, "db_path": relative_path(ctx.workspace_root, path)})

    data["action"] = action
    data["db_path"] = relative_path(ctx.workspace_root, path)
    return ok("project_manage", _render_text(action, data), data)


def _run_action(store: ProjectStore, action: str, args: JsonObject) -> JsonObject:
    project = _project_ref(args)
    task = _task_ref(args)

    if action == "project_create":
        return store.create_project(
            name=str_arg(args, "name") or project,
            description=str_arg(args, "description"),
            status=str_arg(args, "status", "in_progress"),
        )
    if action == "project_list":
        return {
            "projects": store.list_projects(
                status=str_arg(args, "status"),
                include_archived=_bool_arg(args, "include_archived"),
                limit=_limit(args),
            )
        }
    if action == "project_update":
        return {
            "project": store.update_project(
                project,
                name=str_arg(args, "name") or None,
                description=str_arg(args, "description") or None,
                status=str_arg(args, "status") or None,
            )
        }
    if action == "project_archive":
        return {"project": store.archive_project(project)}
    if action == "task_create":
        return store.create_task(
            project=project,
            title=str_arg(args, "title") or task,
            description=str_arg(args, "description"),
            status=str_arg(args, "status", "pending"),
            priority=str_arg(args, "priority", "normal"),
            due=args.get("due", ""),
            tags=args.get("tags", []),
            parent_task=str_arg(args, "parent_task_id"),
            recurrence=str_arg(args, "recurrence"),
        )
    if action == "task_create_many":
        return store.create_tasks(project=project, tasks=_task_items(args))
    if action == "task_list":
        return {
            "tasks": store.list_tasks(
                project=project,
                status=str_arg(args, "status"),
                include_archived=_bool_arg(args, "include_archived"),
                include_done=_bool_arg(args, "include_done", False),
                limit=_limit(args),
            )
        }
    if action == "task_update":
        return {
            "task": store.update_task(
                task,
                project=project,
                title=str_arg(args, "title") or None,
                description=str_arg(args, "description") or None,
                status=str_arg(args, "status") or None,
                priority=str_arg(args, "priority") or None,
                due_at=args.get("due") if args.get("due") else None,
                tags_json=args.get("tags") if args.get("tags") else None,
                recurrence=str_arg(args, "recurrence") or None,
            )
        }
    if action == "task_bulk_update":
        return store.bulk_update_tasks(
            project=project,
            task_refs=_string_list(args.get("task_ids")),
            match_status=str_arg(args, "match_status"),
            status=str_arg(args, "status") or None,
            priority=str_arg(args, "priority") or None,
            due=args.get("due") if args.get("due") else None,
            tags=args.get("tags") if args.get("tags") else None,
            recurrence=str_arg(args, "recurrence") or None,
            include_done=_bool_arg(args, "include_done"),
        )
    if action == "task_complete":
        if not task or _bool_arg(args, "all"):
            return store.set_tasks_status(project=project, status="done")
        return {"task": store.set_task_status(task, "done", project=project)}
    if action == "task_complete_all":
        return store.set_tasks_status(project=project, status="done")
    if action == "task_pending":
        if not task or _bool_arg(args, "all"):
            return store.set_tasks_status(project=project, status="pending")
        return {"task": store.set_task_status(task, "pending", project=project)}
    if action == "task_pending_all":
        return store.set_tasks_status(project=project, status="pending")
    if action == "task_delete":
        return {"task": store.delete_task(task, project=project), "deleted": True}
    if action == "task_delete_many":
        return store.delete_tasks(
            project=project,
            task_refs=_string_list(args.get("task_ids")),
            delete_all=_bool_arg(args, "all"),
            match_status=str_arg(args, "match_status"),
        )
    if action == "subtask_add":
        return store.add_subtask(
            parent_task=(
                str_arg(args, "parent_task_id")
                or str_arg(args, "task_id")
                or str_arg(args, "parent_task")
                or str_arg(args, "task")
            ),
            project=project,
            title=str_arg(args, "title"),
            description=str_arg(args, "description"),
            priority=str_arg(args, "priority", "normal"),
            due=args.get("due", ""),
            tags=args.get("tags", []),
            recurrence=str_arg(args, "recurrence"),
        )
    if action == "note_add":
        return store.add_note(task, str_arg(args, "note"), project=project)
    if action == "activity":
        return {"events": store.activity(project=project, task=task, limit=_limit(args))}
    if action == "summary":
        return store.summary(project=project, limit_projects=_limit(args, 5), limit_tasks=_limit(args, 12))
    raise ValueError(f"Unsupported action: {action}")


def _project_ref(args: JsonObject) -> str:
    return str_arg(args, "project_id") or str_arg(args, "project")


def _normalize_action(action: str) -> str:
    value = str(action or "").strip().lower().replace("-", "_").replace(" ", "_")
    aliases = {
        "list_projects": "project_list",
        "list": "project_list",
        "projects_list": "project_list",
        "list_project": "project_list",
        "get_projects": "project_list",
        "get_project": "project_list",
        "show_projects": "project_list",
        "show_project": "project_list",
        "projects": "project_list",
        "project": "project_list",
        "project_summary": "summary",
        "project_status": "summary",
        "check_project": "summary",
        "double_check": "summary",
        "verify_project": "summary",
        "task_summary": "summary",
        "tasks_summary": "summary",
        "list_tasks": "task_list",
        "tasks_list": "task_list",
        "pending_tasks": "task_list",
        "list_pending": "task_list",
        "list_pending_tasks": "task_list",
        "pending_task_list": "task_list",
        "show_pending_tasks": "task_list",
        "check_tasks": "task_list",
        "verify_tasks": "task_list",
        "create_tasks": "task_create_many",
        "add_tasks": "task_create_many",
        "bulk_create_tasks": "task_create_many",
        "update_tasks": "task_bulk_update",
        "bulk_update_tasks": "task_bulk_update",
        "delete_tasks": "task_delete_many",
        "bulk_delete_tasks": "task_delete_many",
        "complete_task": "task_complete",
        "complete_tasks": "task_complete_all",
        "complete_all": "task_complete_all",
        "complete_all_tasks": "task_complete_all",
        "mark_all_done": "task_complete_all",
        "mark_all_tasks_done": "task_complete_all",
        "reopen_tasks": "task_pending_all",
    }
    return aliases.get(value, value)


def _task_ref(args: JsonObject) -> str:
    return str_arg(args, "task_id") or str_arg(args, "task") or str_arg(args, "title")


def _task_items(args: JsonObject) -> list[JsonObject]:
    value = args.get("tasks")
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError("tasks must be a JSON array") from exc
    if not isinstance(value, list):
        raise ValueError("tasks must be an array")
    tasks = [item for item in value if isinstance(item, dict)]
    if not tasks:
        raise ValueError("tasks must contain at least one task object")
    return tasks


def _string_list(value: object) -> list[str]:
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            parsed = [value]
        value = parsed
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _bool_arg(args: JsonObject, key: str, default: bool = False) -> bool:
    value = args.get(key, default)
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _limit(args: JsonObject, default: int = 30) -> int:
    try:
        value = int(args.get("limit", default))
    except (TypeError, ValueError):
        value = default
    return max(1, min(200, value))


def _render_text(action: str, data: JsonObject) -> str:
    if "created_tasks" in data:
        created = data.get("created_tasks", [])
        existing = data.get("existing_tasks", [])
        project = data.get("project", {})
        lines = [
            f"Created {len(created)} task(s) in {project.get('name', project.get('id', 'project'))}; "
            f"{len(existing)} already existed."
        ]
        lines.extend(_task_line(task) for task in created)
        if existing:
            lines.append("Existing tasks:")
            lines.extend(_task_line(task) for task in existing)
        return "\n".join(lines)
    if "updated_tasks" in data:
        tasks = data["updated_tasks"]
        projects = data.get("projects", [])
        project_names = ", ".join(project["name"] for project in projects) or "active projects"
        if not tasks:
            return f"No tasks were changed to {data.get('status', 'updated')} in {project_names}."
        lines = [
            f"Updated {data.get('updated_count', len(tasks))} task(s) to {data.get('status', 'updated')} in {project_names}."
        ]
        if tasks:
            lines.extend(_task_line(task) for task in tasks)
        return "\n".join(lines)
    if "deleted_tasks" in data:
        tasks = data.get("deleted_tasks", [])
        if not tasks:
            return "No tasks were deleted."
        return "Deleted tasks:\n" + "\n".join(_task_line(task) for task in tasks)
    if "note" in data:
        note = data["note"]
        task = data["task"]
        return f"Added note {note['id']} to task {task['title']} ({task['id']})"
    if "task" in data:
        task = data["task"]
        created = data.get("created")
        deleted = data.get("deleted")
        verb = "Created" if created is True else "Task"
        if action in {"task_update", "task_complete", "task_pending"}:
            verb = "Updated"
        if created is False:
            verb = "Existing"
        if deleted:
            verb = "Deleted"
        due = f" due={task['due_at']}" if task.get("due_at") else ""
        return f"{verb} task {task['title']} ({task['id']}) project={task.get('project_name', task.get('project_id'))} status={task['status']} priority={task['priority']}{due}"
    if "project" in data:
        project = data["project"]
        created = data.get("created")
        verb = "Created" if created is True else "Project"
        if action == "project_archive":
            verb = "Archived"
        return f"{verb} project {project['name']} ({project['id']}) status={project['status']} open={project.get('open_tasks', 0)} done={project.get('done_tasks', 0)}"
    if "open_tasks" in data:
        project_lines = [_project_line(project) for project in data.get("projects", [])]
        task_lines = [_task_line(task) for task in data.get("open_tasks", [])]
        parts = ["Project summary"]
        if project_lines:
            parts.append("Projects:\n" + "\n".join(project_lines))
        if task_lines:
            parts.append("Open tasks:\n" + "\n".join(task_lines))
        return "\n".join(parts)
    if "projects" in data:
        return _render_list("Projects", data["projects"], _project_line)
    if "tasks" in data:
        return _render_list("Tasks", data["tasks"], _task_line)
    if "events" in data:
        return _render_list("Activity", data["events"], _event_line)
    return json.dumps(data, ensure_ascii=False, indent=2)


def _render_list(title: str, rows: list[dict[str, Any]], formatter) -> str:
    if not rows:
        return f"{title}: none"
    return f"{title}:\n" + "\n".join(formatter(row) for row in rows)


def _project_line(project: JsonObject) -> str:
    return (
        "- "
        f"{project['name']} ({project['id']}) "
        f"status={project['status']} open={project.get('open_tasks', 0)} done={project.get('done_tasks', 0)}"
    )


def _task_line(task: JsonObject) -> str:
    due = f" due={task['due_at']}" if task.get("due_at") else ""
    recurrence = f" recurrence={task['recurrence']}" if task.get("recurrence") else ""
    return (
        "- "
        f"{task['title']} ({task['id']}) "
        f"project={task.get('project_name', task.get('project_id'))} "
        f"status={task['status']} priority={task['priority']}{due}{recurrence}"
    )


def _event_line(event: JsonObject) -> str:
    return f"- {event['created_at']} {event['event_type']} project={event.get('project_id', '')} task={event.get('task_id', '')}"
