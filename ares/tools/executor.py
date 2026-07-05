"""ToolExecutor — dispatches tool calls to local implementations."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
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
from ares.tools.web import fetch_url_tool, payload_to_json, web_search_payload
from ares.tools.filesystem_write import write_file as _write_file_impl
from ares.tools.filesystem_write import edit_file as _edit_file_impl
from ares.tools.filesystem_write import create_directory as _create_directory_impl
from ares.tools.filesystem_write import delete_file as _delete_file_impl
from ares.tools.filesystem_write import move_file as _move_file_impl
from ares.tools.filesystem_write import batch_edit as _batch_edit_impl
from ares.tools.filesystem_write import glob_apply as _glob_apply_impl
from ares.tools.filesystem_write import show_file_with_line_numbers as _show_file_with_line_numbers_impl
from ares.tools.filesystem_write import insert_line as _insert_line_impl
from ares.tools.filesystem_write import replace_lines as _replace_lines_impl
from ares.tools.filesystem_write import delete_lines as _delete_lines_impl
from ares.tools.filesystem_write import preview_diff as _preview_diff_impl
from ares.tools.filesystem_write import backup_file as _backup_file_impl
from ares.tools.filesystem_write import undo_last_edit as _undo_last_edit_impl
from ares.tools.filesystem_write import batch_file_ops as _batch_file_ops_impl
from ares.tools.filesystem_write import find_text as _find_text_impl
from ares.tools.filesystem_write import append_to_file as _append_to_file_impl
from ares.tools.filesystem_write import prepend_to_file as _prepend_to_file_impl
from ares.tools.filesystem_write import compare_files as _compare_files_impl
from ares.tools.filesystem_write import create_file_from_template as _create_file_from_template_impl
from ares.tools.filesystem_write import safe_path_status as _safe_path_status_impl
from ares.tools.repl import PersistentREPL
from ares.tools.image_generate import generate_image
from ares.tools.image_edit import image_info as _image_info
from ares.tools.image_edit import resize_image as _resize_image
from ares.tools.image_edit import convert_image as _convert_image
from ares.tools.image_edit import crop_image as _crop_image
from ares.cron.store import CronStore
from ares.cron.tools import CronToolHandlers
from ares.tools.datetime_tool import get_current_datetime_result as _get_current_datetime_impl
from ares.tools import adb_bridge as _adb_bridge
from ares.tools import kdeconnect_bridge as _kdeconnect_bridge


class ToolExecutor:
    """Executes tool calls locally."""

    def __init__(
        self,
        memory_store: MemoryStore,
        conversation_store: ConversationStore | None = None,
        config: AppConfig | None = None,
    ):
        self.memory = memory_store
        self.conversations = conversation_store
        self.config = config
        self.repl = PersistentREPL()
        data_root = None
        if config is not None:
            from pathlib import Path
            data_root = Path(config.data_dir).expanduser().parent
        self.cron = CronToolHandlers(CronStore(data_root))

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
            "batch_edit": self._batch_edit,
            "glob_apply": self._glob_apply,
            "show_file_with_line_numbers": self._show_file_with_line_numbers,
            "insert_line": self._insert_line,
            "replace_lines": self._replace_lines,
            "delete_lines": self._delete_lines,
            "preview_diff": self._preview_diff,
            "backup_file": self._backup_file,
            "undo_last_edit": self._undo_last_edit,
            "batch_file_ops": self._batch_file_ops,
            "find_text": self._find_text,
            "append_to_file": self._append_to_file,
            "prepend_to_file": self._prepend_to_file,
            "compare_files": self._compare_files,
            "create_file_from_template": self._create_file_from_template,
            "safe_path_status": self._safe_path_status,
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
            "create_cron_job": self.cron.create_cron_job,
            "list_cron_jobs": self.cron.list_cron_jobs,
            "get_cron_job": self.cron.get_cron_job,
            "update_cron_job": self.cron.update_cron_job,
            "delete_cron_job": self.cron.delete_cron_job,
            "run_cron_job_now": self.cron.run_cron_job_now,
            "get_cron_logs": self.cron.get_cron_logs,
            "phone_status": self._phone_status,
            "phone_get_notifications": self._phone_get_notifications,
            "phone_search_contact": self._phone_search_contact,
            "phone_send_sms": self._phone_send_sms,
            "phone_call_number": self._phone_call_number,
            "get_current_datetime": self._get_current_datetime,
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

    def _batch_edit(self, args: dict) -> str:
        return _batch_edit_impl(
            operations=args.get("operations", []),
            dry_run=bool(args.get("dry_run", False)),
            confirm=bool(args.get("confirm", False)),
            max_operations=int(args.get("max_operations", 100)),
        )

    def _glob_apply(self, args: dict) -> str:
        return _glob_apply_impl(
            pattern=args["pattern"],
            action=args.get("action", "list"),
            path=args.get("path", "."),
            destination=args.get("destination", ""),
            replacement=args.get("replacement", ""),
            dry_run=bool(args.get("dry_run", True)),
            confirm=bool(args.get("confirm", False)),
            max_matches=int(args.get("max_matches", 100)),
        )

    def _show_file_with_line_numbers(self, args: dict) -> str:
        return _show_file_with_line_numbers_impl(args["path"], args.get("start"), args.get("end"))

    def _insert_line(self, args: dict) -> str:
        return _insert_line_impl(
            args["path"],
            int(args["line"]),
            args.get("text", ""),
            position=args.get("position", "after"),
            dry_run=bool(args.get("dry_run", False)),
            confirm_dangerous=bool(args.get("confirm_dangerous", False)),
        )

    def _replace_lines(self, args: dict) -> str:
        return _replace_lines_impl(
            args["path"],
            int(args["start"]),
            int(args["end"]),
            args.get("new_text", ""),
            dry_run=bool(args.get("dry_run", False)),
            confirm_dangerous=bool(args.get("confirm_dangerous", False)),
        )

    def _delete_lines(self, args: dict) -> str:
        return _delete_lines_impl(
            args["path"],
            int(args["start"]),
            int(args["end"]),
            dry_run=bool(args.get("dry_run", False)),
            confirm_dangerous=bool(args.get("confirm_dangerous", False)),
        )

    def _preview_diff(self, args: dict) -> str:
        return _preview_diff_impl(args["path"], args.get("new_content", ""))

    def _backup_file(self, args: dict) -> str:
        return _backup_file_impl(args["path"], label=args.get("label", ""))

    def _undo_last_edit(self, args: dict) -> str:
        return _undo_last_edit_impl(args["path"], dry_run=bool(args.get("dry_run", False)))

    def _batch_file_ops(self, args: dict) -> str:
        return _batch_file_ops_impl(
            args.get("ops", []),
            dry_run=bool(args.get("dry_run", False)),
            confirm_dangerous=bool(args.get("confirm_dangerous", False)),
            max_operations=int(args.get("max_operations", 100)),
        )

    def _find_text(self, args: dict) -> str:
        return _find_text_impl(
            args["path"],
            args["query"],
            context=int(args.get("context", 2)),
            max_results=int(args.get("max_results", 20)),
        )

    def _append_to_file(self, args: dict) -> str:
        return _append_to_file_impl(
            args["path"],
            args.get("text", ""),
            dry_run=bool(args.get("dry_run", False)),
            confirm_dangerous=bool(args.get("confirm_dangerous", False)),
        )

    def _prepend_to_file(self, args: dict) -> str:
        return _prepend_to_file_impl(
            args["path"],
            args.get("text", ""),
            dry_run=bool(args.get("dry_run", False)),
            confirm_dangerous=bool(args.get("confirm_dangerous", False)),
        )

    def _compare_files(self, args: dict) -> str:
        return _compare_files_impl(args["left"], args["right"])

    def _create_file_from_template(self, args: dict) -> str:
        return _create_file_from_template_impl(
            args["path"],
            template=args.get("template", "notes"),
            dry_run=bool(args.get("dry_run", False)),
            confirm=bool(args.get("confirm", False)),
            confirm_dangerous=bool(args.get("confirm_dangerous", False)),
        )

    def _safe_path_status(self, args: dict) -> str:
        return _safe_path_status_impl(args["path"])
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


    # ── Phone tools ───────────────────────────────────────────────

    def _phone_disabled(self) -> str:
        return '{"ok": false, "error": "Phone bridge is disabled. Set phone.enabled=true in config."}'

    def _phone_status(self, args: dict) -> str:
        if not self.config or not self.config.phone.enabled:
            return self._phone_disabled()
        return _adb_bridge.phone_status()

    def _phone_get_notifications(self, args: dict) -> str:
        if not self.config or not self.config.phone.enabled:
            return self._phone_disabled()
        return _kdeconnect_bridge.get_recent_notifications(limit=int(args.get("limit", 20)))

    def _phone_search_contact(self, args: dict) -> str:
        if not self.config or not self.config.phone.enabled:
            return self._phone_disabled()
        return _kdeconnect_bridge.search_contacts(args["query"])

    def _phone_send_sms(self, args: dict) -> str:
        if not self.config or not self.config.phone.enabled:
            return self._phone_disabled()
        return _kdeconnect_bridge.send_sms(args["number"], args["message"])

    def _phone_call_number(self, args: dict) -> str:
        if not self.config or not self.config.phone.enabled:
            return self._phone_disabled()
        return _adb_bridge.call_number(args["number"], confirm=bool(args.get("confirm", False)))

    # ── DateTime tool ─────────────────────────────────────────────

    def _get_current_datetime(self, args: dict) -> str:
        """Get the current date and time."""
        import json
        result = _get_current_datetime_impl(timezone_name=args.get("timezone"))
        return json.dumps(result, indent=2)
