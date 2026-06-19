"""Tool definitions (for LLM) and implementations (local execution)."""

from __future__ import annotations

from ares.conversations import ConversationStore
from ares.exporter import export_data
from ares.filesystem import list_directory, read_file, search_files
from ares.memory import MemoryStore
from ares.models import AppConfig
from ares.tasks import TaskStore
from ares.web import fetch_url_tool, payload_to_json, web_search_payload


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
    ]


class ToolExecutor:
    """Executes tool calls locally."""

    def __init__(
        self,
        memory_store: MemoryStore,
        task_store: TaskStore,
        conversation_store: ConversationStore | None = None,
        config: AppConfig | None = None,
    ):
        self.memory = memory_store
        self.tasks = task_store
        self.conversations = conversation_store
        self.config = config

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
            "export_data": self._export_data,
            "web_search": self._web_search,
            "fetch_url": self._fetch_url,
            "read_file": self._read_file,
            "search_files": self._search_files,
            "list_directory": self._list_directory,
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
        task_id = self.tasks.create(
            args["title"],
            description=args.get("description"),
            due=args.get("due"),
            priority=args.get("priority", "medium"),
            reminder_at=args.get("reminder_at"),
        )
        task = self.tasks.get(task_id)
        due_str = f" (due: {task['due']})" if task and task.get("due") else ""
        return f"Created task #{task_id}: {args['title']}{due_str}"

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
