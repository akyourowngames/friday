"""ToolExecutor — dispatches tool calls to local implementations."""

from __future__ import annotations

from typing import Any

from ares.conversations import ConversationStore
from ares.tools.exporter import export_data
from ares.tools.filesystem import (
    list_directory, read_file, search_files, get_file_info as _get_file_info_impl,
    glob_pattern as _glob_pattern_impl, disk_usage as _disk_usage_impl,
    checksum as _checksum_impl, copy_file as _copy_file_impl,
    find_duplicates as _find_duplicates_impl, tail_file as _tail_file_impl,
    head_file as _head_file_impl, count_lines as _count_lines_impl,
    file_tree as _file_tree_impl,
)
from ares.memory import MemoryStore
from ares.models import AppConfig
from ares.skills import SkillManager
from ares.tools.tasks import TaskStore
from ares.tools.web import fetch_url_tool, payload_to_json, web_search_payload
from ares.tools.filesystem_write import write_file as _write_file_impl
from ares.tools.filesystem_write import edit_file as _edit_file_impl
from ares.tools.filesystem_write import create_directory as _create_directory_impl
from ares.tools.filesystem_write import delete_file as _delete_file_impl
from ares.tools.filesystem_write import move_file as _move_file_impl
from ares.tools.repl import PersistentREPL
from ares.tools.image_generate import generate_image
from ares.tools.image_edit import image_info as _image_info
from ares.tools.image_edit import resize_image as _resize_image
from ares.tools.image_edit import convert_image as _convert_image
from ares.tools.image_edit import crop_image as _crop_image


class ToolExecutor:
    """Executes tool calls locally."""

    def __init__(
        self,
        memory_store: MemoryStore,
        task_store: TaskStore,
        conversation_store: ConversationStore | None = None,
        config: AppConfig | None = None,
        task_executor: Any | None = None,
    ):
        self.memory = memory_store
        self.tasks = task_store
        self.conversations = conversation_store
        self.config = config
        self.task_executor = task_executor
        self.task_executor_ref = None  # wired by server for resume support
        self.repl = PersistentREPL()

    def close(self) -> None:
        """Clean up persistent sessions."""
        self.repl.close()

    def execute(self, tool_name: str, arguments: dict) -> str:
        """Execute a tool by name with the given arguments. Returns a result string."""
        handlers = {
            "store_memory": self._store_memory,
            "search_memory": self._search_memory,
            "update_memory": self._update_memory,
            "delete_memory": self._delete_memory,
            "create_task": self._create_task,
            "list_tasks": self._list_tasks,
            "search_tasks": self._search_tasks,
            "complete_task": self._complete_task,
            "cancel_task": self._cancel_task,
            "get_due_soon": self._get_due_soon,
            "get_execution_status": self._get_execution_status,
            "get_executor_status": self._get_executor_status,
            "list_skills": self._list_skills_tool,
            "load_skill": self._load_skill_tool,
            "create_skill": self._create_skill_tool,
            "export_data": self._export_data,
            "web_search": self._web_search,
            "fetch_url": self._fetch_url,
            "read_file": self._read_file,
            "search_files": self._search_files,
            "list_directory": self._list_directory,
            "get_file_info": self._get_file_info,
            "glob_pattern": self._glob_pattern,
            "write_file": self._write_file,
            "edit_file": self._edit_file,
            "create_directory": self._create_directory,
            "delete_file": self._delete_file,
            "move_file": self._move_file,
            "disk_usage": self._disk_usage,
            "checksum": self._checksum,
            "copy_file": self._copy_file,
            "find_duplicates": self._find_duplicates,
            "tail_file": self._tail_file,
            "head_file": self._head_file,
            "count_lines": self._count_lines,
            "file_tree": self._file_tree,
            "run_code": self._run_code,
            "run_command": self._run_command,
            "generate_image": self._generate_image,
            "image_info": self._image_info,
            "resize_image": self._resize_image,
            "convert_image": self._convert_image,
            "crop_image": self._crop_image,
            "terminal_exec": self._terminal_exec,
            "resume_task": self._resume_task,
            "get_task_events": self._get_task_events,
            "get_task_artifacts": self._get_task_artifacts,
        }
        try:
            handler = handlers[tool_name]
        except KeyError as exc:
            raise ValueError(f"Unknown tool: {tool_name}") from exc
        return handler(arguments)

    # ── Memory tools ──────────────────────────────────────────────

    def _store_memory(self, args: dict) -> str:
        content = args["content"]
        fact_id = self.memory.store(
            content,
            category=args.get("category", "note"),
            confidence=float(args.get("confidence", 1.0)),
            importance=float(args.get("importance", 0.5)),
        )
        return f"Stored memory #{fact_id}: {content}"

    def _search_memory(self, args: dict) -> str:
        query = args["query"]
        limit = int(args.get("limit", 5))
        results = self.memory.search(query, limit=limit)
        if not results:
            return f"No matching memories found for '{query}'."
        lines = [f"Found {len(results)} memories:"]
        for r in results:
            lines.append(
                f"- #{r['fact_id']} [{r.get('category', 'note')}, importance={r.get('importance', 0.5)}] "
                f"{r['fact_text']}"
            )
        return "\n".join(lines)

    def _update_memory(self, args: dict) -> str:
        fact_id = int(args["fact_id"])
        updated = self.memory.update(
            fact_id,
            fact_text=args.get("content"),
            category=args.get("category"),
            confidence=float(args["confidence"]) if args.get("confidence") is not None else None,
            importance=float(args["importance"]) if args.get("importance") is not None else None,
        )
        if not updated:
            return f"Memory #{fact_id} was not found."
        memory = self.memory.get(fact_id)
        return f"Updated memory #{fact_id}: {memory['fact_text']}"

    def _delete_memory(self, args: dict) -> str:
        fact_id = int(args["fact_id"])
        if self.memory.delete(fact_id):
            return f"Forgot memory #{fact_id}."
        return f"Memory #{fact_id} was not found."

    # ── Task tools ────────────────────────────────────────────────

    def _create_task(self, args: dict) -> str:
        auto_exec = "yes" if args.get("auto_executable", False) else "no"
        max_turns = int(args.get("max_turns", 10))
        max_attempts = int(args.get("max_attempts", 3))
        task_id = self.tasks.create(
            args["title"],
            description=args.get("description"),
            due=args.get("due"),
            priority=args.get("priority", "medium"),
            reminder_at=args.get("reminder_at"),
            auto_executable=auto_exec,
            max_turns=max_turns,
        )
        # Set v2 state
        self.tasks.update(task_id, state="queued", max_attempts=max_attempts)
        task = self.tasks.get(task_id)
        due_str = f" (due: {task['due']})" if task and task.get("due") else ""
        auto_str = " [auto]" if auto_exec == "yes" else ""
        if auto_exec == "yes" and self.task_executor_ref is not None:
            wake = getattr(self.task_executor_ref, "wake", None)
            if callable(wake):
                wake()
        result = f"Created task #{task_id}: {args['title']}{due_str}{auto_str}"
        if auto_exec == "yes":
            result += (
                "\nAuto-executable task queued for the background executor. "
                "Do not execute it inline; the executor will plan, run, verify, and track artifacts."
            )
        return result

    def _format_task(self, task: dict) -> str:
        due = f" | due: {task['due']}" if task.get("due") else ""
        status = f" | {task['status']}" if task.get("status") != "pending" else ""
        return f"- #{task['id']} [{task['priority']}] {task['title']}{due}{status}"

    def _list_tasks(self, args: dict | None = None) -> str:
        limit = int((args or {}).get("limit", 50))
        pending = self.tasks.list_pending(limit=limit)
        if not pending:
            return "No pending tasks."
        return "\n".join([f"You have {len(pending)} pending task(s):"] + [
            self._format_task(t) for t in pending
        ])

    def _search_tasks(self, args: dict) -> str:
        results = self.tasks.search(
            args["query"],
            limit=int(args.get("limit", 10)),
            include_done=bool(args.get("include_done", False)),
        )
        if not results:
            return f"No matching tasks found for '{args['query']}'."
        return "\n".join([f"Found {len(results)} task(s):"] + [
            self._format_task(t) for t in results
        ])

    def _complete_task(self, args: dict) -> str:
        task_id = int(args["task_id"])
        # Set v2 state alongside old status
        self.tasks.update(task_id, state="completed")
        if self.tasks.complete(task_id):
            return f"Completed task #{task_id}."
        return f"Task #{task_id} was not found or is not pending."

    def _cancel_task(self, args: dict) -> str:
        task_id = int(args["task_id"])
        # Set v2 state alongside old status
        self.tasks.update(task_id, state="cancelled")
        if self.tasks.cancel(task_id):
            return f"Cancelled task #{task_id}."
        return f"Task #{task_id} was not found."

    def _get_due_soon(self, args: dict) -> str:
        hours = int(args.get("hours", 24))
        tasks = self.tasks.get_due_soon(hours=hours)
        if not tasks:
            return f"No tasks due in the next {hours} hour(s)."
        return "\n".join([f"Due in the next {hours} hour(s):"] + [
            self._format_task(t) for t in tasks
        ])

    def _get_execution_status(self, args: dict) -> str:
        limit = int(args.get("limit", 10))
        tasks = self.tasks.get_recently_executed(limit=limit)
        if not tasks:
            return "No tasks have been auto-executed yet."
        lines = [f"Recently executed ({len(tasks)} task(s)):"]
        for t in tasks:
            status_icon = "✅" if t["status"] == "done" else "⚠️"
            lines.append(
                f"  {status_icon} #{t['id']} {t['title']} [{t['status']}]"
            )
            if t.get("execution_notes"):
                lines.append(f"     Notes: {t['execution_notes']}")
            if t.get("executed_at"):
                lines.append(f"     At: {t['executed_at']}")
        return "\n".join(lines)

    def _get_executor_status(self, args: dict) -> str:
        if self.task_executor is None:
            return "Executor not available (no reference configured)."
        stats = self.task_executor.stats if hasattr(self.task_executor, "stats") else {}
        lines = [f"Executor state: {stats.get('state', 'unknown')}"]
        if stats.get("enabled") is not None:
            lines.append(f"Enabled: {'yes' if stats['enabled'] else 'no'}")
        if stats.get("current_task_title"):
            lines.append(f"Currently executing: #{stats['current_task_id']} \"{stats['current_task_title']}\"")
        if stats.get("last_error"):
            lines.append(f"Last error: {stats['last_error']}")
        if stats.get("tasks_completed") is not None:
            lines.append(f"Tasks completed: {stats['tasks_completed']}")
        if stats.get("tasks_failed") is not None:
            lines.append(f"Tasks failed: {stats['tasks_failed']}")
        if stats.get("started_at"):
            lines.append(f"Started at: {stats['started_at']}")
        return "\n".join(lines)

    # ── Skills ─────────────────────────────────────────────────────

    def _skill_manager(self) -> SkillManager:
        manager = getattr(self, "skill_manager", None)
        if manager is None:
            skill_dirs = list((self.config.skill_dirs if self.config else []) or [])
            manager = SkillManager(skill_dirs=skill_dirs or None)
            self.skill_manager = manager
        return manager

    def _list_skills_tool(self, args: dict | None = None) -> str:
        args = args or {}
        manager = self._skill_manager()
        skills = manager.search(
            query=args.get("query", ""),
            category=args.get("category", ""),
        )
        if not skills:
            return "No matching skills found."
        lines = [f"Available skills ({len(skills)}):"]
        lines.extend(skill.summary_line() for skill in skills)
        cats = manager.list_categories()
        if cats:
            lines.append("Categories: " + ", ".join(f"{name} ({count})" for name, count in cats.items()))
        return "\n".join(lines)

    def _load_skill_tool(self, args: dict) -> str:
        skill = self._skill_manager().get_skill(args["name"])
        if skill is None:
            return f"Skill '{args['name']}' was not found."
        files = ""
        if skill.files:
            rel_files = [str(path.relative_to(skill.root)) for path in skill.files[:20]]
            files = "\n\nSupporting files:\n" + "\n".join(f"- {file}" for file in rel_files)
        return (
            f"# Skill: {skill.name}\n"
            f"Category: {skill.category}\n"
            f"Description: {skill.description}\n"
            f"Version: {skill.version}\n\n"
            f"{skill.content}{files}"
        )

    def _create_skill_tool(self, args: dict) -> str:
        skill = self._skill_manager().create_skill(
            name=args["name"],
            content=args["content"],
            category=args.get("category", "general"),
        )
        return f"Created skill '{skill.name}' in category '{skill.category}' at {skill.path}."

    # ── Export ─────────────────────────────────────────────────────

    def _export_data(self, args: dict) -> str:
        path = export_data(
            memory_store=self.memory,
            task_store=self.tasks,
            conversation_store=self.conversations,
            config=self.config,
            path=args.get("path"),
        )
        return f"Exported Ares data to {path}"

    # ── Web tools ──────────────────────────────────────────────────

    def _web_search(self, args: dict) -> str:
        payload = web_search_payload(
            args["query"],
            max_results=int(args.get("max_results", 5)),
            provider=args.get("provider"),
        )
        return payload_to_json(payload)

    def _fetch_url(self, args: dict) -> str:
        return fetch_url_tool(args)

    # ── Filesystem (read) tools ────────────────────────────────────

    def _read_file(self, args: dict) -> str:
        return read_file(
            args["path"],
            start_line=int(args.get("start_line", 1)),
            num_lines=int(args.get("num_lines", 200)),
        )

    def _search_files(self, args: dict) -> str:
        return search_files(
            query=args.get("query", ""),
            path=args.get("path", "."),
            name_pattern=args.get("name_pattern", ""),
            max_results=int(args.get("max_results", 20)),
        )

    def _list_directory(self, args: dict) -> str:
        return list_directory(
            path=args.get("path", "."),
            max_items=int(args.get("max_items", 30)),
        )

    def _get_file_info(self, args: dict) -> str:
        return _get_file_info_impl(args["path"])

    def _glob_pattern(self, args: dict) -> str:
        return _glob_pattern_impl(
            args["pattern"],
            path=args.get("path", "."),
            max_results=int(args.get("max_results", 50)),
        )

    # ── Filesystem (write) tools ───────────────────────────────────

    def _write_file(self, args: dict) -> str:
        path = args["path"]
        content = args["content"]
        dry_run = bool(args.get("dry_run", False))
        confirm = bool(args.get("confirm", False))

        # Check if file exists and needs confirmation
        from ares.tools.filesystem import resolve_path as read_resolve
        try:
            resolved = read_resolve(path)
            is_overwrite = resolved.exists()
        except ValueError:
            is_overwrite = False

        if is_overwrite and not confirm and not dry_run:
            from ares.tools.filesystem import _format_size
            try:
                size = _format_size(resolved.stat().st_size)
            except OSError:
                size = "unknown"
            return (
                f"⚠ CONFIRM REQUIRED: This will overwrite {path} ({size}). "
                f"Re-call with confirm=true to proceed."
            )

        return _write_file_impl(path, content, dry_run=dry_run)

    def _edit_file(self, args: dict) -> str:
        return _edit_file_impl(
            args["path"],
            args["old_text"],
            args["new_text"],
            dry_run=bool(args.get("dry_run", False)),
        )

    def _create_directory(self, args: dict) -> str:
        return _create_directory_impl(
            args["path"],
            dry_run=bool(args.get("dry_run", False)),
        )

    def _delete_file(self, args: dict) -> str:
        path = args["path"]
        dry_run = bool(args.get("dry_run", False))
        confirm = bool(args.get("confirm", False))

        if not confirm and not dry_run:
            return (
                f"⚠ CONFIRM REQUIRED: This will delete {path}. "
                f"Re-call with confirm=true to proceed."
            )

        return _delete_file_impl(path, dry_run=dry_run)

    def _move_file(self, args: dict) -> str:
        source = args["source"]
        destination = args["destination"]
        dry_run = bool(args.get("dry_run", False))
        confirm = bool(args.get("confirm", False))

        # Check if destination exists and needs confirmation
        from ares.tools.filesystem import resolve_path as read_resolve
        try:
            dst_resolved = read_resolve(destination)
            dst_exists = dst_resolved.exists()
        except ValueError:
            dst_exists = False

        if dst_exists and not confirm and not dry_run:
            return (
                f"⚠ CONFIRM REQUIRED: Destination {destination} already exists. "
                f"Re-call with confirm=true to proceed (will overwrite)."
            )

        return _move_file_impl(source, destination, dry_run=dry_run)

    def _disk_usage(self, args: dict) -> str:
        return _disk_usage_impl(
            path=args.get("path", "."),
            max_depth=int(args.get("max_depth", 2)),
        )

    def _checksum(self, args: dict) -> str:
        return _checksum_impl(
            path=args["path"],
            algorithm=args.get("algorithm", "sha256"),
        )

    def _copy_file(self, args: dict) -> str:
        return _copy_file_impl(
            source=args["source"],
            destination=args["destination"],
            overwrite=bool(args.get("overwrite", False)),
            dry_run=bool(args.get("dry_run", False)),
        )

    def _find_duplicates(self, args: dict) -> str:
        return _find_duplicates_impl(
            path=args.get("path", "."),
            min_size=int(args.get("min_size", 1024)),
            max_results=int(args.get("max_results", 50)),
        )

    def _tail_file(self, args: dict) -> str:
        return _tail_file_impl(
            path=args["path"],
            num_lines=int(args.get("num_lines", 20)),
        )

    def _head_file(self, args: dict) -> str:
        return _head_file_impl(
            path=args["path"],
            num_lines=int(args.get("num_lines", 20)),
        )

    def _count_lines(self, args: dict) -> str:
        return _count_lines_impl(
            path=args.get("path", "."),
            pattern=args.get("pattern", ""),
            name_pattern=args.get("name_pattern", ""),
        )

    def _file_tree(self, args: dict) -> str:
        return _file_tree_impl(
            path=args.get("path", "."),
            max_depth=int(args.get("max_depth", 3)),
            show_files=bool(args.get("show_files", True)),
        )

    # ── Code execution tools ───────────────────────────────────────

    def _run_code(self, args: dict) -> str:
        code = args["code"]
        timeout = int(args.get("timeout", 30))
        cwd = args.get("cwd")
        return self.repl.execute_python(code, timeout=timeout, cwd=cwd)

    def _run_command(self, args: dict) -> str:
        command = args["command"]
        timeout = int(args.get("timeout", 30))
        cwd = args.get("cwd")
        return self.repl.execute_shell(command, timeout=timeout, cwd=cwd)

    # ── Image tools ────────────────────────────────────────────────

    def _generate_image(self, args: dict) -> str:
        prompt = args["prompt"]
        width = int(args.get("width", 1024))
        height = int(args.get("height", 1024))
        model = args.get("model", "flux")
        seed = args.get("seed")
        if seed is not None:
            seed = int(seed)
        return generate_image(prompt, width=width, height=height, model=model, seed=seed)

    def _image_info(self, args: dict) -> str:
        return _image_info(args["path"])

    def _resize_image(self, args: dict) -> str:
        return _resize_image(
            args["path"],
            width=args.get("width"),
            height=args.get("height"),
            percent=args.get("percent"),
            output=args.get("output"),
        )

    def _convert_image(self, args: dict) -> str:
        return _convert_image(
            args["path"],
            format=args["format"],
            output=args.get("output"),
            quality=int(args.get("quality", 85)),
        )

    def _crop_image(self, args: dict) -> str:
        return _crop_image(
            args["path"],
            left=int(args.get("left", 0)),
            top=int(args.get("top", 0)),
            right=args["right"],
            bottom=args["bottom"],
            output=args.get("output"),
        )

    # ── Terminal ───────────────────────────────────────────────────

    def _terminal_exec(self, args: dict) -> str:
        """Execute a shell command via persistent REPL for reliable output capture.

        Optionally displays the command in the visual terminal panel if connected.
        """
        command = args["command"]
        timeout = int(args.get("timeout", 30))
        cwd = args.get("cwd")

        # Execute via REPL for reliable output capture
        result = self.repl.execute_shell(command, timeout=timeout, cwd=cwd)

        # Also send to visual terminal if connected (best-effort display)
        if hasattr(self, '_terminal_display_callback') and self._terminal_display_callback:
            try:
                self._terminal_display_callback(command)
            except Exception:
                pass  # display is optional

        return result

    # ── v2 Task Tools ──────────────────────────────────────────

    def _resume_task(self, args: dict) -> str:
        """Resume a failed task from where it left off."""
        task_id = int(args["task_id"])
        task = self.tasks.get(task_id)

        if not task:
            return f"Task #{task_id} not found."

        state = task.get("state") or task.get("status", "pending")
        if state not in ("failed", "cancelled"):
            return f"Task #{task_id} cannot be resumed (state: {state})."

        if not task.get("plan"):
            return f"Task #{task_id} has no execution plan. Cannot resume."

        if self.task_executor_ref:
            self.task_executor_ref.enqueue_resume(task_id)
            return f"Task #{task_id} queued for resume."
        else:
            return "Task executor not available."

    def _get_task_events(self, args: dict) -> str:
        """Get the execution log for a task."""
        task_id = int(args["task_id"])
        limit = int(args.get("limit", 50))
        events = self.tasks.get_events(task_id, limit=limit)

        if not events:
            return f"No events found for task #{task_id}."

        lines = [f"Execution Log — Task #{task_id}:"]
        for event in events:
            ts = event.get("timestamp", "?")
            level = event.get("level", "info")
            step = event.get("step")
            msg = event.get("message", "")

            icon = {"info": "→", "success": "✓", "warning": "⚠", "error": "✗"}.get(level, "·")
            step_prefix = f"Step {step}: " if step else ""

            lines.append(f"  {icon} {ts}  {step_prefix}{msg}")

        return "\n".join(lines)

    def _get_task_artifacts(self, args: dict) -> str:
        """Get all files created or modified by a task."""
        task_id = int(args["task_id"])
        artifacts = self.tasks.get_artifacts(task_id)

        if not artifacts:
            return f"No artifacts found for task #{task_id}."

        lines = [f"Artifacts — Task #{task_id}:"]
        for a in artifacts:
            icon = "📄" if a["artifact_type"] == "write_file" else "📝" if a["artifact_type"] == "edit_file" else "📁"
            step = a.get("step", "?")
            size = a.get("size_human", "?")
            lines.append(f"  {icon} {a['path']}")
            lines.append(f"     {size}" + (f" · {a['line_count']} lines" if a.get('line_count') else ""))
            lines.append(f"     Step {step}")

        return "\n".join(lines)