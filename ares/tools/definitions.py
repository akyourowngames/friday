"""Tool definitions in OpenAI function calling format."""

from __future__ import annotations


def _tool(name: str, description: str, properties: dict, required: list[str] | None = None) -> dict:
    """Helper to create an OpenAI function-calling tool schema."""
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
    """Return all tool definitions in OpenAI function calling format."""
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
                "max_attempts": {
                    "type": "integer",
                    "default": 3,
                    "description": "Max retry attempts on failure (default 3).",
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
            "list_skills",
            "List available reusable skills/playbooks with names, categories, and descriptions.",
            {
                "category": {"type": "string", "description": "Optional category filter."},
                "query": {"type": "string", "description": "Optional search query."},
            },
        ),
        _tool(
            "load_skill",
            "Load a skill's full SKILL.md instructions into context when relevant or explicitly requested.",
            {
                "name": {"type": "string", "description": "Skill name to load."},
            },
            ["name"],
        ),
        _tool(
            "create_skill",
            "Save a reusable workflow as a local Ares skill.",
            {
                "name": {"type": "string", "description": "Skill name in lowercase-hyphen form."},
                "content": {"type": "string", "description": "Full SKILL.md content or markdown body."},
                "category": {"type": "string", "description": "Skill category.", "default": "general"},
            },
            ["name", "content"],
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
            "Execute a shell command with full output capture, and optionally display it in the interactive terminal panel. Use this when the user explicitly says 'run in terminal', 'show me in the terminal', or wants to see the command in the terminal panel. For normal command execution without visual terminal, use run_command instead.",
            {
                "command": {"type": "string", "description": "Shell command to execute"},
                "wait": {"type": "boolean", "description": "Wait for command to complete (default true)"},
                "timeout": {"type": "integer", "description": "Max seconds to wait for completion (default 30)"},
                "cwd": {"type": "string", "description": "Working directory for the command"},
            },
            required=["command"],
        ),
        _tool(
            "resume_task",
            "Resume a failed task from where it left off. Only works on tasks with state='failed'. Re-executes from the first uncompleted step.",
            {
                "task_id": {"type": "integer", "description": "ID of the failed task to resume"},
            },
            required=["task_id"],
        ),
        _tool(
            "get_task_events",
            "Get the execution log for a task. Shows all state changes, step progress, and events with timestamps.",
            {
                "task_id": {"type": "integer", "description": "ID of the task"},
                "limit": {"type": "integer", "description": "Max events to return (default 50)"},
            },
            required=["task_id"],
        ),
        _tool(
            "get_task_artifacts",
            "Get all files created or modified by a task. Shows file paths, sizes, and which step created them.",
            {
                "task_id": {"type": "integer", "description": "ID of the task"},
            },
            required=["task_id"],
        ),
    ]