"""Tool definitions (for LLM) and implementations (local execution)."""

from __future__ import annotations

from ares.conversations import ConversationStore
from ares.exporter import export_data
from ares.filesystem import (
    list_directory, read_file, search_files, get_file_info as _get_file_info_impl,
    glob_pattern as _glob_pattern_impl, disk_usage as _disk_usage_impl,
    checksum as _checksum_impl, copy_file as _copy_file_impl,
    find_duplicates as _find_duplicates_impl, tail_file as _tail_file_impl,
    head_file as _head_file_impl, count_lines as _count_lines_impl,
    file_tree as _file_tree_impl,
)
from ares.memory import MemoryStore
from ares.models import AppConfig
from ares.tasks import TaskStore
from ares.web import fetch_url_tool, payload_to_json, web_search_payload
from ares.filesystem_write import write_file as _write_file_impl
from ares.filesystem_write import edit_file as _edit_file_impl
from ares.filesystem_write import create_directory as _create_directory_impl
from ares.filesystem_write import delete_file as _delete_file_impl
from ares.filesystem_write import move_file as _move_file_impl
from ares.code_execution import run_code
from ares.shell_execution import run_command
from ares.image_generate import generate_image
from ares.image_edit import image_info as _image_info
from ares.image_edit import resize_image as _resize_image
from ares.image_edit import convert_image as _convert_image
from ares.image_edit import crop_image as _crop_image

def _tool(name: str, description: str, properties: dict, required: list[str] | None = None) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required or [],
            },
        },
    }


def get_tool_definitions() -> list[dict]:
    """Return tool definitions in OpenAI function calling format."""
    return [
        _tool(
            "store_memory",
            "Save a fact, preference, or personal detail the user wants remembered.",
            {
                "content": {"type": "string", "description": "What to remember."},
                "category": {
                    "type": "string",
                    "description": "preference, fact, belief, habit, relationship, or note",
                    "default": "note",
                },
                "confidence": {"type": "number", "default": 1.0},
                "importance": {"type": "number", "default": 0.5},
            },
            ["content"],
        ),
        _tool(
            "search_memory",
            "Search stored user memories.",
            {
                "query": {"type": "string", "description": "What to search for."},
                "limit": {"type": "integer", "default": 5},
            },
            ["query"],
        ),
        _tool(
            "update_memory",
            "Correct or enrich an existing stored memory.",
            {
                "fact_id": {"type": "integer", "description": "Memory ID."},
                "content": {"type": "string", "description": "Replacement memory text."},
                "category": {"type": "string"},
                "confidence": {"type": "number"},
                "importance": {"type": "number"},
            },
            ["fact_id"],
        ),
        _tool(
            "delete_memory",
            "Forget a stored memory by ID.",
            {"fact_id": {"type": "integer", "description": "Memory ID to delete."}},
            ["fact_id"],
        ),
        _tool(
            "create_task",
            "Create a reminder, to-do, or task.",
            {
                "title": {"type": "string", "description": "The task title."},
                "description": {"type": "string"},
                "due": {"type": "string", "description": "ISO or natural-language due date."},
                "reminder_at": {"type": "string", "description": "ISO or natural-language reminder time."},
                "priority": {
                    "type": "string",
                    "enum": ["low", "medium", "high"],
                    "default": "medium",
                },
                "auto_executable": {
                    "type": "boolean",
                    "default": False,
                    "description": "If true, Ares will try to auto-complete this task in the background.",
                },
                "max_turns": {
                    "type": "integer",
                    "default": 10,
                    "description": "Max tool-use turns for auto-execution (default 10).",
                },
            },
            ["title"],
        ),
        _tool(
            "list_tasks",
            "Show pending tasks and reminders.",
            {"limit": {"type": "integer", "default": 50}},
        ),
        _tool(
            "search_tasks",
            "Search tasks by title or description.",
            {
                "query": {"type": "string"},
                "limit": {"type": "integer", "default": 10},
                "include_done": {"type": "boolean", "default": False},
            },
            ["query"],
        ),
        _tool(
            "complete_task",
            "Mark a pending task as done.",
            {"task_id": {"type": "integer"}},
            ["task_id"],
        ),
        _tool(
            "cancel_task",
            "Cancel a task.",
            {"task_id": {"type": "integer"}},
            ["task_id"],
        ),
        _tool(
            "get_due_soon",
            "Show pending tasks due within the next N hours.",
            {"hours": {"type": "integer", "default": 24}},
        ),
        _tool(
            "get_execution_status",
            "Show recently auto-completed tasks with execution notes.",
            {"limit": {"type": "integer", "default": 10}},
        ),
        _tool(
            "get_executor_status",
            "Show the background task executor's current state: idle, running, stopped, disabled. Returns which task is being executed, error state, and completion stats.",
            {},
        ),
        _tool(
            "export_data",
            "Export local Ares memories, tasks, conversations, and config to JSON.",
            {"path": {"type": "string", "description": "Optional output JSON path."}},
        ),
        _tool(
            "web_search",
            "Search the web AND automatically read the top results. Returns search results plus the full content of the top 3 pages. One call does everything — no need to fetch URLs separately.",
            {
                "query": {"type": "string", "description": "The web search query."},
                "max_results": {"type": "integer", "default": 5},
                "fetch_top": {
                    "type": "integer",
                    "default": 3,
                    "description": "How many top results to automatically fetch full content for (0 to skip fetching).",
                },
                "provider": {
                    "type": "string",
                    "enum": ["auto", "tavily", "ddgs"],
                    "default": "auto",
                    "description": "Search provider. auto uses Tavily when configured, otherwise ddgs.",
                },
            },
            ["query"],
        ),
        _tool(
            "fetch_url",
            "Fetch a web page and extract its readable text content. Use after web_search to read the full content of a specific page.",
            {
                "url": {"type": "string", "description": "The URL to fetch (must start with http:// or https://)."},
                "max_chars": {
                    "type": "integer",
                    "default": 15000,
                    "description": "Maximum characters to return (default 15000).",
                },
                "extract_text": {
                    "type": "boolean",
                    "default": True,
                    "description": "If true, strips HTML tags and returns plain text. If false, returns raw HTML.",
                },
            },
            ["url"],
        ),
        _tool(
            "read_file",
            "Read a local text file with line numbers. Use for a specific file path.",
            {
                "path": {"type": "string", "description": "Absolute, home-relative, or workspace-relative path."},
                "start_line": {"type": "integer", "default": 1},
                "num_lines": {"type": "integer", "default": 200},
            },
            ["path"],
        ),
        _tool(
            "search_files",
            "Search local files by content and/or file name glob pattern.",
            {
                "query": {"type": "string", "description": "Content regex/text to search for.", "default": ""},
                "path": {"type": "string", "description": "Directory to search.", "default": "."},
                "name_pattern": {"type": "string", "description": "Glob pattern such as *.py.", "default": ""},
                "max_results": {"type": "integer", "default": 20},
            },
        ),
        _tool(
            "list_directory",
            "List a local directory with file sizes and subdirectories.",
            {
                "path": {"type": "string", "description": "Directory path.", "default": "."},
                "max_items": {"type": "integer", "default": 30},
            },
        ),
        _tool(
            "get_file_info",
            "Get metadata about a file or directory: type, size, timestamps, binary status.",
            {
                "path": {"type": "string", "description": "File or directory path."},
            },
            ["path"],
        ),
        _tool(
            "glob_pattern",
            "Find files matching a glob pattern (e.g. **/*.py, src/**/*.ts).",
            {
                "pattern": {"type": "string", "description": "Glob pattern."},
                "path": {"type": "string", "default": ".", "description": "Directory to search from."},
                "max_results": {"type": "integer", "default": 50, "description": "Max files to return."},
            },
            ["pattern"],
        ),
        _tool(
            "write_file",
            "Create a new file or overwrite an existing one. If overwriting, confirm=true is required.",
            {
                "path": {"type": "string", "description": "File path to write."},
                "content": {"type": "string", "description": "File content to write."},
                "dry_run": {"type": "boolean", "default": False, "description": "Preview without writing."},
                "confirm": {"type": "boolean", "default": False, "description": "Confirm destructive overwrite."},
            },
            ["path", "content"],
        ),
        _tool(
            "edit_file",
            "Edit a file by searching and replacing text. old_text must match uniquely. If no match, returns the closest content as a suggestion.",
            {
                "path": {"type": "string", "description": "File path."},
                "old_text": {"type": "string", "description": "Text to find (must match uniquely in the file)."},
                "new_text": {"type": "string", "description": "Replacement text."},
                "dry_run": {"type": "boolean", "default": False, "description": "Preview without editing."},
            },
            ["path", "old_text", "new_text"],
        ),
        _tool(
            "create_directory",
            "Create a directory and any missing parent directories (mkdir -p).",
            {
                "path": {"type": "string", "description": "Directory path to create."},
                "dry_run": {"type": "boolean", "default": False, "description": "Preview without creating."},
            },
            ["path"],
        ),
        _tool(
            "delete_file",
            "Delete a file or empty directory. Always requires confirm=true.",
            {
                "path": {"type": "string", "description": "File or directory path to delete."},
                "confirm": {"type": "boolean", "default": False, "description": "Confirm deletion."},
                "dry_run": {"type": "boolean", "default": False, "description": "Preview without deleting."},
            },
            ["path"],
        ),
        _tool(
            "move_file",
            "Move or rename a file or directory. Creates parent directories of destination as needed.",
            {
                "source": {"type": "string", "description": "Current file path."},
                "destination": {"type": "string", "description": "New file path."},
                "confirm": {"type": "boolean", "default": False, "description": "Confirm if destination exists."},
                "dry_run": {"type": "boolean", "default": False, "description": "Preview without moving."},
            },
            ["source", "destination"],
        ),
        _tool(
            "disk_usage",
            "Show disk usage for a directory tree with sizes and file counts (like du -sh).",
            {
                "path": {"type": "string", "description": "Directory to analyze.", "default": "."},
                "max_depth": {"type": "integer", "default": 2, "description": "How deep to traverse (1-5)."},
            },
        ),
        _tool(
            "checksum",
            "Compute file checksum hash (md5, sha1, sha256, sha512). Useful for verifying file integrity.",
            {
                "path": {"type": "string", "description": "File path."},
                "algorithm": {"type": "string", "default": "sha256", "description": "Hash algorithm: md5, sha1, sha256, or sha512."},
            },
            ["path"],
        ),
        _tool(
            "copy_file",
            "Copy a file to a new location.",
            {
                "source": {"type": "string", "description": "Source file path."},
                "destination": {"type": "string", "description": "Destination file path."},
                "overwrite": {"type": "boolean", "default": False, "description": "Overwrite if destination exists."},
                "dry_run": {"type": "boolean", "default": False, "description": "Preview without copying."},
            },
            ["source", "destination"],
        ),
        _tool(
            "find_duplicates",
            "Find duplicate files by size and content hash. Useful for cleaning up redundant files.",
            {
                "path": {"type": "string", "description": "Directory to scan.", "default": "."},
                "min_size": {"type": "integer", "default": 1024, "description": "Minimum file size to check (bytes)."},
                "max_results": {"type": "integer", "default": 50, "description": "Max duplicate groups to show."},
            },
        ),
        _tool(
            "tail_file",
            "Read the last N lines of a file (like Unix tail).",
            {
                "path": {"type": "string", "description": "File path."},
                "num_lines": {"type": "integer", "default": 20, "description": "Number of lines to read from end."},
            },
            ["path"],
        ),
        _tool(
            "head_file",
            "Read the first N lines of a file (like Unix head).",
            {
                "path": {"type": "string", "description": "File path."},
                "num_lines": {"type": "integer", "default": 20, "description": "Number of lines to read."},
            },
            ["path"],
        ),
        _tool(
            "count_lines",
            "Count lines in files with optional content and name filtering (like wc -l).",
            {
                "path": {"type": "string", "description": "Directory to scan.", "default": "."},
                "pattern": {"type": "string", "default": "", "description": "Content regex pattern to match lines."},
                "name_pattern": {"type": "string", "default": "", "description": "File name glob pattern (e.g. *.py)."},
            },
        ),
        _tool(
            "file_tree",
            "Display directory tree structure (like the tree command).",
            {
                "path": {"type": "string", "description": "Directory path.", "default": "."},
                "max_depth": {"type": "integer", "default": 3, "description": "Max depth to show (1-10)."},
                "show_files": {"type": "boolean", "default": True, "description": "Show files (false = dirs only)."},
            },
        ),
        _tool(
            "run_code",
            "Execute Python code in an isolated subprocess. Full access to stdlib, pip, filesystem, network. Returns exit code + output.",
            {
                "code": {"type": "string", "description": "Python code to execute"},
                "timeout": {"type": "integer", "description": "Max seconds before kill (1-300, default 30)"},
                "cwd": {"type": "string", "description": "Working directory (default: home)"},
            },
            required=["code"],
        ),
        _tool(
            "run_command",
            "Execute a shell command (bash, git, npm, python, docker, etc.). Full system access. Supports pipes, redirects, && chaining.",
            {
                "command": {"type": "string", "description": "Shell command to execute"},
                "timeout": {"type": "integer", "description": "Max seconds before kill (1-300, default 30)"},
                "cwd": {"type": "string", "description": "Working directory"},
            },
            required=["command"],
        ),
        _tool(
            "generate_image",
            "Generate an image from a text prompt using Pollinations.ai (free, no API key). Returns saved file path.",
            {
                "prompt": {"type": "string", "description": "Text description of the image to generate"},
                "width": {"type": "integer", "description": "Output width in pixels (default 1024)"},
                "height": {"type": "integer", "description": "Output height in pixels (default 1024)"},
                "model": {"type": "string", "enum": ["flux", "turbo", "stable-diffusion"], "description": "Model to use (default flux)"},
                "seed": {"type": "integer", "description": "Deterministic seed for reproducibility"},
            },
            required=["prompt"],
        ),
        _tool(
            "image_info",
            "Get metadata about an image: dimensions, format, mode, file size, EXIF data.",
            {
                "path": {"type": "string", "description": "Path to the image file"},
            },
            required=["path"],
        ),
        _tool(
            "resize_image",
            "Resize an image preserving aspect ratio. Uses LANCZOS resampling (highest quality).",
            {
                "path": {"type": "string", "description": "Source image path"},
                "width": {"type": "integer", "description": "Target width (scales proportionally)"},
                "height": {"type": "integer", "description": "Target height (scales proportionally)"},
                "percent": {"type": "integer", "description": "Scale by percentage (50 = half)"},
                "output": {"type": "string", "description": "Output path (default: overwrite source)"},
            },
            required=["path"],
        ),
        _tool(
            "convert_image",
            "Convert image between formats (PNG, JPEG, WebP, BMP, GIF). Handles RGBA to JPEG transparency.",
            {
                "path": {"type": "string", "description": "Source image path"},
                "format": {"type": "string", "enum": ["png", "jpeg", "webp", "bmp", "gif"], "description": "Target format"},
                "output": {"type": "string", "description": "Output path (default: same name, new extension)"},
                "quality": {"type": "integer", "description": "JPEG/WebP quality 1-100 (default 85)"},
            },
            required=["path", "format"],
        ),
        _tool(
            "crop_image",
            "Crop a rectangular region from an image. Coordinates in pixels, right/bottom exclusive.",
            {
                "path": {"type": "string", "description": "Source image path"},
                "left": {"type": "integer", "description": "Left edge in pixels (default 0)"},
                "top": {"type": "integer", "description": "Top edge in pixels (default 0)"},
                "right": {"type": "integer", "description": "Right edge in pixels (exclusive)"},
                "bottom": {"type": "integer", "description": "Bottom edge in pixels (exclusive)"},
                "output": {"type": "string", "description": "Output path (default: overwrite source)"},
            },
            required=["path", "right", "bottom"],
        ),
        _tool(
            "terminal_exec",
            "Send a command to the interactive terminal panel and wait for it to complete. Use when you need visible output the user can see, or when running commands that need interactive shell features. For simple one-shot commands that don't need visibility, prefer run_command.",
            {
                "command": {"type": "string", "description": "Shell command to execute in the terminal"},
                "wait": {"type": "boolean", "description": "Wait for command to complete (default true)"},
                "timeout": {"type": "integer", "description": "Max seconds to wait for completion (default 30)"},
            },
            required=["command"],
        ),
    ]


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
        }
        try:
            handler = handlers[tool_name]
        except KeyError as exc:
            raise ValueError(f"Unknown tool: {tool_name}") from exc
        return handler(arguments)

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

    def _create_task(self, args: dict) -> str:
        auto_exec = "yes" if args.get("auto_executable", False) else "no"
        max_turns = int(args.get("max_turns", 10))
        task_id = self.tasks.create(
            args["title"],
            description=args.get("description"),
            due=args.get("due"),
            priority=args.get("priority", "medium"),
            reminder_at=args.get("reminder_at"),
            auto_executable=auto_exec,
            max_turns=max_turns,
        )
        task = self.tasks.get(task_id)
        due_str = f" (due: {task['due']})" if task and task.get("due") else ""
        auto_str = " [auto]" if auto_exec == "yes" else ""
        return f"Created task #{task_id}: {args['title']}{due_str}{auto_str}"

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
        if self.tasks.complete(task_id):
            return f"Completed task #{task_id}."
        return f"Task #{task_id} was not found or is not pending."

    def _cancel_task(self, args: dict) -> str:
        task_id = int(args["task_id"])
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

    def _export_data(self, args: dict) -> str:
        path = export_data(
            memory_store=self.memory,
            task_store=self.tasks,
            conversation_store=self.conversations,
            config=self.config,
            path=args.get("path"),
        )
        return f"Exported Ares data to {path}"

    def _web_search(self, args: dict) -> str:
        payload = web_search_payload(
            args["query"],
            max_results=int(args.get("max_results", 5)),
            provider=args.get("provider"),
        )
        return payload_to_json(payload)

    def _fetch_url(self, args: dict) -> str:
        return fetch_url_tool(args)

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

    def _write_file(self, args: dict) -> str:
        path = args["path"]
        content = args["content"]
        dry_run = bool(args.get("dry_run", False))
        confirm = bool(args.get("confirm", False))

        # Check if file exists and needs confirmation
        from ares.filesystem import resolve_path as read_resolve
        try:
            resolved = read_resolve(path)
            is_overwrite = resolved.exists()
        except ValueError:
            is_overwrite = False

        if is_overwrite and not confirm and not dry_run:
            from ares.filesystem import _format_size
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
        from ares.filesystem import resolve_path as read_resolve
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

    def _run_code(self, args: dict) -> str:
        code = args["code"]
        timeout = int(args.get("timeout", 30))
        cwd = args.get("cwd")
        return run_code(code, timeout=timeout, cwd=cwd)

    def _run_command(self, args: dict) -> str:
        command = args["command"]
        timeout = int(args.get("timeout", 30))
        cwd = args.get("cwd")
        return run_command(command, timeout=timeout, cwd=cwd)

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

    def _terminal_exec(self, args: dict) -> str:
        """Send a command to the interactive terminal panel."""
        command = args["command"]
        wait = bool(args.get("wait", True))
        timeout = int(args.get("timeout", 30))

        if not hasattr(self, '_terminal_exec_callback') or self._terminal_exec_callback is None:
            return "Error: No terminal connected. Open the terminal panel in the desktop app first."

        try:
            result = self._terminal_exec_callback(command, wait=wait, timeout=timeout)
            return result
        except Exception as e:
            return f"Error executing in terminal: {e}"
