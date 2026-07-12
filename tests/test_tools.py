"""Tests for tool definitions and implementations."""

import asyncio
import json

import pytest

from ares.conversations import ConversationStore
from ares.memory import MemoryStore
from ares.tools import ToolExecutor, get_tool_definitions


class TestToolDefinitions:
    def test_has_expected_tools(self):
        """We define the expected local tool surface."""
        tools = get_tool_definitions()
        assert len(tools) == 83

    def test_tool_names(self):
        """Tool names match expected set."""
        tools = get_tool_definitions()
        names = {t["function"]["name"] for t in tools}
        assert names == {
            "store_memory",
            "search_memory",
            "update_memory",
            "delete_memory",
            "remember_person",
            "search_person",
            "update_person",
            "forget_person",
            "search_actions",
            "list_skills",
            "load_skill",
            "create_skill",
            "search_skill_marketplace",
            "install_marketplace_skill",
            "search_mcp_marketplace",
            "add_marketplace_mcp",
            "export_data",
            "web_search",
            "fetch_url",
            "read_file",
            "search_files",
            "list_directory",
            "get_file_info",
            "glob_pattern",
            "write_file",
            "edit_file",
            "create_directory",
            "delete_file",
            "move_file",
            "batch_edit",
            "glob_apply",
            "show_file_with_line_numbers",
            "insert_line",
            "replace_lines",
            "delete_lines",
            "preview_diff",
            "backup_file",
            "undo_last_edit",
            "batch_file_ops",
            "find_text",
            "append_to_file",
            "prepend_to_file",
            "compare_files",
            "create_file_from_template",
            "safe_path_status",
            "disk_usage",
            "checksum",
            "copy_file",
            "find_duplicates",
            "tail_file",
            "head_file",
            "count_lines",
            "file_tree",
            "run_code",
            "run_command",
            "generate_image",
            "image_info",
            "resize_image",
            "convert_image",
            "crop_image",
            "terminal_exec",
            "phone_status",
            "phone_get_notifications",
            "phone_search_contact",
            "phone_send_sms",
            "phone_call_number",
            "phone_launch_app",
            "phone_open_url",
            "update_config",
            "create_cron_job",
            "list_cron_jobs",
            "get_cron_job",
            "update_cron_job",
            "delete_cron_job",
            "run_cron_job_now",
            "get_cron_logs",
            "create_task",
            "list_tasks",
            "get_task_status",
            "update_task",
            "cancel_task",
            "run_task",
            "get_current_datetime",
        }

    def test_durable_task_tools_are_registered_without_legacy_aliases(self):
        """The continuity plan replaces the old unfinished task-list gap."""
        tools = get_tool_definitions()
        names = {t["function"]["name"] for t in tools}
        assert {
            "create_task",
            "list_tasks",
            "get_task_status",
            "update_task",
            "cancel_task",
            "run_task",
        }.issubset(names)
        assert not {"search_tasks", "complete_task", "get_due_soon"}.intersection(names)

    def test_tools_have_schemas(self):
        """Each tool has a valid OpenAI-compatible parameters schema."""
        tools = get_tool_definitions()
        for tool in tools:
            assert "function" in tool
            assert "parameters" in tool["function"]
            assert "properties" in tool["function"]["parameters"]


def test_get_file_info_tool_definition():
    from ares.tools import get_tool_definitions
    defs = get_tool_definitions()
    names = [d["function"]["name"] for d in defs]
    assert "get_file_info" in names


def test_glob_pattern_tool_definition():
    from ares.tools import get_tool_definitions
    defs = get_tool_definitions()
    names = [d["function"]["name"] for d in defs]
    assert "glob_pattern" in names


def test_write_file_tool_definition():
    from ares.tools import get_tool_definitions
    defs = get_tool_definitions()
    names = [d["function"]["name"] for d in defs]
    assert "write_file" in names


def test_edit_file_tool_definition():
    from ares.tools import get_tool_definitions
    defs = get_tool_definitions()
    names = [d["function"]["name"] for d in defs]
    assert "edit_file" in names


def test_create_directory_tool_definition():
    from ares.tools import get_tool_definitions
    defs = get_tool_definitions()
    names = [d["function"]["name"] for d in defs]
    assert "create_directory" in names


def test_delete_file_tool_definition():
    from ares.tools import get_tool_definitions
    defs = get_tool_definitions()
    names = [d["function"]["name"] for d in defs]
    assert "delete_file" in names


def test_move_file_tool_definition():
    from ares.tools import get_tool_definitions
    defs = get_tool_definitions()
    names = [d["function"]["name"] for d in defs]
    assert "move_file" in names


class TestToolExecutor:
    @pytest.fixture
    def executor(self, tmp_path, fake_embedding_provider):
        mem_store = MemoryStore(
            db_path=tmp_path / "mem.db",
            embedding_provider=fake_embedding_provider,
        )
        conversation_store = ConversationStore(db_path=tmp_path / "convo.db")
        return ToolExecutor(
            memory_store=mem_store,
            conversation_store=conversation_store,
        )

    def test_store_memory(self, executor):
        """store_memory tool stores a fact and returns confirmation."""
        result = executor.execute("store_memory", {
            "content": "User prefers dark mode",
            "category": "preference",
        })
        assert "Stored" in result
        assert "dark mode" in result

    def test_store_memory_rejects_temporary_or_tool_state(self, executor):
        """store_memory rejects non-durable facts before they pollute memory."""
        result = executor.execute("store_memory", {
            "content": "Delhi weather is rainy tonight",
            "category": "fact",
        })

        assert result.startswith("Memory not stored")
        assert not executor.memory.search("rainy")

    def test_search_memory(self, executor):
        """search_memory tool retrieves relevant facts."""
        executor.execute("store_memory", {"content": "Birthday is March 5"})
        result = executor.execute("search_memory", {"query": "birthday"})
        assert "birthday" in result.lower() or "march" in result.lower()

    def test_update_and_delete_memory(self, executor):
        """Memory tools can correct and forget facts by ID."""
        created = executor.execute("store_memory", {"content": "User likes tea"})
        fact_id = int(created.split("#", 1)[1].split(":", 1)[0])
        updated = executor.execute("update_memory", {
            "fact_id": fact_id,
            "content": "User likes coffee",
            "importance": 0.9,
        })
        assert "coffee" in updated
        deleted = executor.execute("delete_memory", {"fact_id": fact_id})
        assert "Forgot" in deleted

    def test_search_memory_empty(self, executor):
        """search_memory with no matches returns informative message."""
        result = executor.execute("search_memory", {"query": "xyznonexistent"})
        assert "no matching" in result.lower() or "found" in result.lower()

    def test_export_data_tool(self, executor, tmp_path):
        """export_data writes a JSON backup."""
        output = tmp_path / "backup.json"
        result = executor.execute("export_data", {"path": str(output)})
        assert "Exported" in result
        assert output.exists()

    def test_web_search_tool(self, executor, monkeypatch):
        """web_search tool returns structured JSON payload."""
        monkeypatch.setattr("ares.tools.executor.web_search_payload", lambda query, max_results=5, provider=None, fetch_top=3, max_fetch_chars=8000: {
            "query": query,
            "provider": provider or "ddgs",
            "summary": "Summary",
            "answer": "",
            "results": [{"title": "Result", "url": "https://example.com", "snippet": query}],
            "fetched": [],
            "errors": [],
        })
        result = executor.execute("web_search", {"query": "current news", "max_results": 1})
        payload = json.loads(result)
        assert payload["summary"] == "Summary"
        assert payload["results"][0]["title"] == "Result"

    def test_web_search_tool_passes_fetch_controls(self, executor, monkeypatch):
        """web_search forwards fetch controls into the payload builder."""
        seen = {}

        def fake_payload(query, max_results=5, provider=None, fetch_top=3, max_fetch_chars=8000):
            seen.update(fetch_top=fetch_top, max_fetch_chars=max_fetch_chars)
            return {
                "query": query,
                "provider": "ddgs",
                "summary": "Summary",
                "answer": "",
                "results": [],
                "fetched": [],
                "errors": [],
            }

        monkeypatch.setattr("ares.tools.executor.web_search_payload", fake_payload)

        executor.execute("web_search", {
            "query": "current news",
            "fetch_top": 1,
            "max_fetch_chars": 1234,
        })

        assert seen == {"fetch_top": 1, "max_fetch_chars": 1234}

    def test_web_search_async_prefers_fetch_mcp(self, executor, monkeypatch):
        """web_search can combine search with Fetch MCP when connected."""
        monkeypatch.setattr("ares.tools.executor.web_search_payload", lambda query, max_results=5, provider=None, fetch_top=3, max_fetch_chars=8000: {
            "query": query,
            "provider": "ddgs",
            "summary": "Summary",
            "answer": "",
            "results": [{"title": "Result", "url": "https://example.com", "snippet": query}],
            "fetched": [],
            "errors": [],
        })

        class FakeMCPManager:
            tool_definitions = [
                {"function": {"name": "mcp__fetch__fetch"}},
            ]

            def __init__(self):
                self.calls = []

            async def call_tool(self, tool_name, arguments):
                self.calls.append((tool_name, arguments))
                return "Fetched by MCP"

        fake_mcp = FakeMCPManager()
        executor.mcp_manager = fake_mcp

        result = asyncio.run(executor.execute_async("web_search", {
            "query": "current news",
            "fetch_top": 1,
            "fetcher": "auto",
            "max_fetch_chars": 500,
        }))

        payload = json.loads(result)
        assert payload["fetched"][0]["content"] == "Fetched by MCP"
        assert payload["fetched"][0]["fetcher"] == "mcp"
        assert fake_mcp.calls == [
            ("mcp__fetch__fetch", {"url": "https://example.com", "max_length": 500})
        ]

    def test_fetch_url_tool(self, executor, monkeypatch):
        """fetch_url returns structured page extraction payload."""
        monkeypatch.setattr("ares.tools.executor.fetch_url_tool", lambda args: json.dumps({
            "url": args["url"],
            "title": "Example",
            "content": "Readable page text",
            "error": "",
        }))
        result = executor.execute("fetch_url", {"url": "https://example.com"})
        payload = json.loads(result)
        assert payload["title"] == "Example"
        assert payload["content"] == "Readable page text"

    def test_file_tools(self, executor, tmp_path, monkeypatch):
        """read_file, search_files, and list_directory expose read-only local access."""
        monkeypatch.setattr("ares.filesystem.Path.home", lambda: tmp_path)
        test_file = tmp_path / "sample.txt"
        test_file.write_text("alpha\nbeta\n", encoding="utf-8")

        read_result = executor.execute("read_file", {"path": str(test_file)})
        assert "alpha" in read_result
        assert "1\talpha" in read_result

        search_result = executor.execute("search_files", {
            "query": "beta",
            "path": str(tmp_path),
            "max_results": 5,
        })
        assert "sample.txt" in search_result

        list_result = executor.execute("list_directory", {"path": str(tmp_path)})
        assert "sample.txt" in list_result

    def test_unknown_tool(self, executor):
        """Unknown tool name raises ValueError."""
        with pytest.raises(ValueError, match="Unknown tool"):
            executor.execute("nonexistent_tool", {})

    def test_executor_write_file_new(self, executor, tmp_path, monkeypatch):
        monkeypatch.setattr("ares.filesystem.Path.home", lambda: tmp_path)
        path = str(tmp_path / "test.txt")
        result = executor.execute("write_file", {"path": path, "content": "hello"})
        assert "Created" in result

    def test_executor_write_file_overwrite_blocked(self, executor, tmp_path, monkeypatch):
        monkeypatch.setattr("ares.filesystem.Path.home", lambda: tmp_path)
        path = tmp_path / "existing.txt"
        path.write_text("old", encoding="utf-8")
        result = executor.execute("write_file", {"path": str(path), "content": "new"})
        assert "CONFIRM" in result
        assert path.read_text(encoding="utf-8") == "old"  # unchanged

    def test_executor_delete_file_blocked_without_confirm(self, executor, tmp_path, monkeypatch):
        monkeypatch.setattr("ares.filesystem.Path.home", lambda: tmp_path)
        path = tmp_path / "victim.txt"
        path.write_text("bye", encoding="utf-8")
        result = executor.execute("delete_file", {"path": str(path)})
        assert "CONFIRM" in result
        assert path.exists()  # unchanged


