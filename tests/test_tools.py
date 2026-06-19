"""Tests for tool definitions and implementations."""

import json

import pytest

from ares.conversations import ConversationStore
from ares.memory import MemoryStore
from ares.tasks import TaskStore
from ares.tools import ToolExecutor, get_tool_definitions


class TestToolDefinitions:
    def test_has_expected_tools(self):
        """We define the expected local tool surface."""
        tools = get_tool_definitions()
        assert len(tools) == 23

    def test_tool_names(self):
        """Tool names match expected set."""
        tools = get_tool_definitions()
        names = {t["function"]["name"] for t in tools}
        assert names == {
            "store_memory",
            "search_memory",
            "update_memory",
            "delete_memory",
            "create_task",
            "list_tasks",
            "search_tasks",
            "complete_task",
            "cancel_task",
            "get_due_soon",
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
        }

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
        task_store = TaskStore(db_path=tmp_path / "tasks.db")
        conversation_store = ConversationStore(db_path=tmp_path / "convo.db")
        return ToolExecutor(
            memory_store=mem_store,
            task_store=task_store,
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

    def test_create_task(self, executor):
        """create_task tool creates a task and returns confirmation."""
        result = executor.execute("create_task", {
            "title": "Call dentist",
            "due": "2026-06-19T14:00:00",
            "priority": "medium",
        })
        assert "Created" in result or "task" in result.lower()

    def test_list_tasks_empty(self, executor):
        """list_tasks with no tasks returns informative message."""
        result = executor.execute("list_tasks", {})
        assert "no" in result.lower() or "empty" in result.lower()

    def test_list_tasks_with_items(self, executor):
        """list_tasks shows pending tasks."""
        executor.execute("create_task", {"title": "Buy milk"})
        executor.execute("create_task", {"title": "Walk dog"})
        result = executor.execute("list_tasks", {})
        assert "milk" in result.lower() or "dog" in result.lower()

    def test_task_management_tools(self, executor):
        """Task tools can search, complete, cancel, and report due soon."""
        create_result = executor.execute("create_task", {
            "title": "Submit taxes",
            "due": "2099-01-01T12:00:00+00:00",
            "priority": "high",
        })
        task_id = int(create_result.split("#", 1)[1].split(":", 1)[0])
        assert "taxes" in executor.execute("search_tasks", {"query": "taxes"}).lower()
        assert "No tasks due" in executor.execute("get_due_soon", {"hours": 1})
        assert "Completed" in executor.execute("complete_task", {"task_id": task_id})

        second = executor.execute("create_task", {"title": "Cancel me"})
        second_id = int(second.split("#", 1)[1].split(":", 1)[0])
        assert "Cancelled" in executor.execute("cancel_task", {"task_id": second_id})

    def test_export_data_tool(self, executor, tmp_path):
        """export_data writes a JSON backup."""
        output = tmp_path / "backup.json"
        result = executor.execute("export_data", {"path": str(output)})
        assert "Exported" in result
        assert output.exists()

    def test_web_search_tool(self, executor, monkeypatch):
        """web_search tool returns structured JSON payload."""
        monkeypatch.setattr("ares.tools.web_search_payload", lambda query, max_results=5, provider=None: {
            "query": query,
            "provider": provider or "ddgs",
            "summary": "Summary",
            "answer": "",
            "results": [{"title": "Result", "url": "https://example.com", "snippet": query}],
            "errors": [],
        })
        result = executor.execute("web_search", {"query": "current news", "max_results": 1})
        payload = json.loads(result)
        assert payload["summary"] == "Summary"
        assert payload["results"][0]["title"] == "Result"

    def test_fetch_url_tool(self, executor, monkeypatch):
        """fetch_url returns structured page extraction payload."""
        monkeypatch.setattr("ares.tools.fetch_url_tool", lambda args: json.dumps({
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
        monkeypatch.setattr("ares.filesystem_write._home", lambda: tmp_path)
        path = str(tmp_path / "test.txt")
        result = executor.execute("write_file", {"path": path, "content": "hello"})
        assert "Created" in result

    def test_executor_write_file_overwrite_blocked(self, executor, tmp_path, monkeypatch):
        monkeypatch.setattr("ares.filesystem.Path.home", lambda: tmp_path)
        monkeypatch.setattr("ares.filesystem_write._home", lambda: tmp_path)
        path = tmp_path / "existing.txt"
        path.write_text("old", encoding="utf-8")
        result = executor.execute("write_file", {"path": str(path), "content": "new"})
        assert "CONFIRM" in result
        assert path.read_text(encoding="utf-8") == "old"  # unchanged

    def test_executor_delete_file_blocked_without_confirm(self, executor, tmp_path, monkeypatch):
        monkeypatch.setattr("ares.filesystem.Path.home", lambda: tmp_path)
        monkeypatch.setattr("ares.filesystem_write._home", lambda: tmp_path)
        path = tmp_path / "victim.txt"
        path.write_text("bye", encoding="utf-8")
        result = executor.execute("delete_file", {"path": str(path)})
        assert "CONFIRM" in result
        assert path.exists()  # unchanged


