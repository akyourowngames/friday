"""Integration tests for ToolExecutor with file tools."""

import pytest
import tempfile
from pathlib import Path

from ares.memory import MemoryStore
from ares.tasks import TaskStore
from ares.tools import ToolExecutor, get_tool_definitions


class TestToolExecutorFileTools:
    """Integration tests for file tools via ToolExecutor."""

    @pytest.fixture
    def executor(self):
        """Create a ToolExecutor with temporary stores."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = Path(tmp_dir) / "test.db"
            mem_store = MemoryStore(db_path)
            task_store = TaskStore(db_path)
            yield ToolExecutor(mem_store, task_store)
            mem_store.close()
            task_store.close()

    @pytest.fixture
    def temp_home(self, monkeypatch):
        """Monkeypatch home directory to a temp location."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            monkeypatch.setattr("ares.filesystem.Path.home", lambda: tmp)
            yield tmp

    def test_get_file_info_via_executor(self, executor, temp_home):
        """get_file_info should work via ToolExecutor."""
        test_file = temp_home / "test.txt"
        test_file.write_text("hello", encoding="utf-8")

        result = executor.execute("get_file_info", {"path": str(test_file)})

        assert "Type: file" in result
        assert "hello.txt" in result or "test.txt" in result

    def test_glob_pattern_via_executor(self, executor, temp_home):
        """glob_pattern should work via ToolExecutor."""
        (temp_home / "a.py").write_text("x")
        (temp_home / "b.py").write_text("y")
        (temp_home / "c.txt").write_text("z")

        result = executor.execute(
            "glob_pattern", {"pattern": "*.py", "path": str(temp_home)}
        )

        assert "a.py" in result
        assert "b.py" in result
        assert "c.txt" not in result

    def test_write_file_new_via_executor(self, executor, temp_home):
        """write_file for new file should work via ToolExecutor."""
        target = temp_home / "new.txt"

        result = executor.execute(
            "write_file", {"path": str(target), "content": "hello"}
        )

        assert "Created" in result
        assert target.read_text(encoding="utf-8") == "hello"

    def test_write_file_overwrite_requires_confirm_via_executor(
        self, executor, temp_home
    ):
        """write_file overwrite should require confirmation."""
        target = temp_home / "existing.txt"
        target.write_text("old")

        # Without confirm
        result = executor.execute(
            "write_file", {"path": str(target), "content": "new"}
        )

        assert "CONFIRM REQUIRED" in result
        assert target.read_text(encoding="utf-8") == "old"  # unchanged

    def test_write_file_overwrite_with_confirm_via_executor(
        self, executor, temp_home
    ):
        """write_file overwrite with confirm should work."""
        target = temp_home / "existing.txt"
        target.write_text("old")

        result = executor.execute(
            "write_file",
            {"path": str(target), "content": "new", "confirm": True},
        )

        assert "Overwrote" in result
        assert target.read_text(encoding="utf-8") == "new"

    def test_write_file_dry_run_via_executor(self, executor, temp_home):
        """write_file with dry_run should not create file."""
        target = temp_home / "dry.txt"

        result = executor.execute(
            "write_file",
            {"path": str(target), "content": "content", "dry_run": True},
        )

        assert "DRY RUN" in result
        assert not target.exists()

    def test_edit_file_via_executor(self, executor, temp_home):
        """edit_file should work via ToolExecutor."""
        target = temp_home / "code.py"
        target.write_text("def foo():\n    pass\n")

        result = executor.execute(
            "edit_file",
            {
                "path": str(target),
                "old_text": "pass",
                "new_text": "return 42",
            },
        )

        assert "Edited" in result
        assert "return 42" in target.read_text(encoding="utf-8")

    def test_edit_file_no_match_via_executor(self, executor, temp_home):
        """edit_file with no match should return suggestion."""
        target = temp_home / "code.py"
        target.write_text("def foo():\n    print('hello')\n")

        result = executor.execute(
            "edit_file",
            {
                "path": str(target),
                "old_text": "nonexistent",
                "new_text": "new",
            },
        )

        assert "No match" in result or "Did you mean" in result

    def test_create_directory_via_executor(self, executor, temp_home):
        """create_directory should work via ToolExecutor."""
        target = temp_home / "nested" / "dir"

        result = executor.execute(
            "create_directory", {"path": str(target)}
        )

        assert "Created" in result
        assert target.is_dir()

    def test_delete_file_requires_confirm_via_executor(self, executor, temp_home):
        """delete_file should require confirmation."""
        target = temp_home / "delete_me.txt"
        target.write_text("bye")

        # Without confirm
        result = executor.execute("delete_file", {"path": str(target)})

        assert "CONFIRM REQUIRED" in result
        assert target.exists()  # unchanged

    def test_delete_file_with_confirm_via_executor(self, executor, temp_home):
        """delete_file with confirm should delete file."""
        target = temp_home / "delete_me.txt"
        target.write_text("bye")

        result = executor.execute(
            "delete_file", {"path": str(target), "confirm": True}
        )

        assert "Deleted" in result
        assert not target.exists()

    def test_delete_file_dry_run_via_executor(self, executor, temp_home):
        """delete_file with dry_run should not delete."""
        target = temp_home / "keep.txt"
        target.write_text("keep")

        result = executor.execute(
            "delete_file", {"path": str(target), "dry_run": True}
        )

        assert "DRY RUN" in result
        assert target.exists()

    def test_move_file_basic_via_executor(self, executor, temp_home):
        """move_file should work via ToolExecutor."""
        src = temp_home / "old.txt"
        src.write_text("content")
        dst = temp_home / "new.txt"

        result = executor.execute(
            "move_file", {"source": str(src), "destination": str(dst)}
        )

        assert "Moved" in result
        assert not src.exists()
        assert dst.read_text(encoding="utf-8") == "content"

    def test_move_file_overwrite_requires_confirm_via_executor(
        self, executor, temp_home
    ):
        """move_file to existing destination should require confirm."""
        src = temp_home / "src.txt"
        src.write_text("new")
        dst = temp_home / "dst.txt"
        dst.write_text("old")

        # Without confirm
        result = executor.execute(
            "move_file", {"source": str(src), "destination": str(dst)}
        )

        assert "CONFIRM REQUIRED" in result
        assert dst.read_text(encoding="utf-8") == "old"  # unchanged

    def test_move_file_overwrite_with_confirm_via_executor(
        self, executor, temp_home
    ):
        """move_file with confirm should overwrite destination."""
        src = temp_home / "src.txt"
        src.write_text("new")
        dst = temp_home / "dst.txt"
        dst.write_text("old")

        result = executor.execute(
            "move_file",
            {"source": str(src), "destination": str(dst), "confirm": True},
        )

        assert "Moved" in result
        assert not src.exists()
        assert dst.read_text(encoding="utf-8") == "new"

    def test_move_file_dry_run_via_executor(self, executor, temp_home):
        """move_file with dry_run should not move."""
        src = temp_home / "src.txt"
        src.write_text("data")
        dst = temp_home / "dst.txt"

        result = executor.execute(
            "move_file",
            {"source": str(src), "destination": str(dst), "dry_run": True},
        )

        assert "DRY RUN" in result
        assert src.exists()
        assert not dst.exists()

    def test_read_file_via_executor(self, executor, temp_home):
        """read_file should work via ToolExecutor."""
        target = temp_home / "read_me.txt"
        target.write_text("line 1\nline 2\nline 3\n")

        result = executor.execute(
            "read_file", {"path": str(target), "num_lines": 2}
        )

        assert "line 1" in result
        assert "line 2" in result
        assert "read_me.txt" in result or "File:" in result

    def test_search_files_via_executor(self, executor, temp_home):
        """search_files should work via ToolExecutor."""
        (temp_home / "a.py").write_text("def hello(): pass")
        (temp_home / "b.py").write_text("def goodbye(): pass")

        result = executor.execute(
            "search_files", {"query": "hello", "path": str(temp_home)}
        )

        assert "a.py" in result or "hello" in result.lower()

    def test_list_directory_via_executor(self, executor, temp_home):
        """list_directory should work via ToolExecutor."""
        (temp_home / "file1.txt").write_text("x")
        (temp_home / "file2.txt").write_text("y")
        (temp_home / "subdir").mkdir()

        result = executor.execute("list_directory", {"path": str(temp_home)})

        assert "file1.txt" in result
        assert "file2.txt" in result
        assert "subdir" in result


class TestToolDefinitions:
    """Tests for tool definitions structure."""

    def test_all_file_tools_defined(self):
        """All 8 file tools should be defined."""
        tools = get_tool_definitions()
        tool_names = [t["function"]["name"] for t in tools]

        required = [
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
        ]

        for req in required:
            assert req in tool_names, f"Tool {req} not found in definitions"

    def test_write_file_tool_has_confirm_param(self):
        """write_file tool should have confirm parameter."""
        tools = get_tool_definitions()
        write_file_tool = next(t for t in tools if t["function"]["name"] == "write_file")

        params = write_file_tool["function"]["parameters"]["properties"]
        assert "confirm" in params, "write_file missing confirm parameter"
        assert "dry_run" in params, "write_file missing dry_run parameter"

    def test_delete_file_tool_has_confirm_param(self):
        """delete_file tool should have confirm parameter."""
        tools = get_tool_definitions()
        delete_tool = next(t for t in tools if t["function"]["name"] == "delete_file")

        params = delete_tool["function"]["parameters"]["properties"]
        assert "confirm" in params, "delete_file missing confirm parameter"
        assert "dry_run" in params, "delete_file missing dry_run parameter"

    def test_move_file_tool_has_confirm_param(self):
        """move_file tool should have confirm parameter."""
        tools = get_tool_definitions()
        move_tool = next(t for t in tools if t["function"]["name"] == "move_file")

        params = move_tool["function"]["parameters"]["properties"]
        assert "confirm" in params, "move_file missing confirm parameter"
        assert "dry_run" in params, "move_file missing dry_run parameter"

    def test_all_write_tools_have_dry_run(self):
        """All write tools should have dry_run parameter."""
        tools = get_tool_definitions()
        write_tools = ["write_file", "edit_file", "create_directory", "delete_file", "move_file"]

        for tool_name in write_tools:
            tool = next(t for t in tools if t["function"]["name"] == tool_name)
            params = tool["function"]["parameters"]["properties"]
            assert "dry_run" in params, f"{tool_name} missing dry_run parameter"

    def test_edit_file_has_old_new_text_params(self):
        """edit_file should have old_text and new_text parameters."""
        tools = get_tool_definitions()
        edit_tool = next(t for t in tools if t["function"]["name"] == "edit_file")

        params = edit_tool["function"]["parameters"]["properties"]
        assert "old_text" in params
        assert "new_text" in params

    def test_glob_pattern_has_pattern_param(self):
        """glob_pattern should have pattern parameter."""
        tools = get_tool_definitions()
        glob_tool = next(t for t in tools if t["function"]["name"] == "glob_pattern")

        params = glob_tool["function"]["parameters"]["properties"]
        assert "pattern" in params

    def test_get_file_info_has_path_param(self):
        """get_file_info should have path parameter."""
        tools = get_tool_definitions()
        info_tool = next(t for t in tools if t["function"]["name"] == "get_file_info")

        params = info_tool["function"]["parameters"]["properties"]
        assert "path" in params


class TestEndToEndWorkflows:
    """End-to-end integration tests combining multiple tools."""

    @pytest.fixture
    def executor(self):
        """Create a ToolExecutor with temporary stores."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = Path(tmp_dir) / "test.db"
            mem_store = MemoryStore(db_path)
            task_store = TaskStore(db_path)
            yield ToolExecutor(mem_store, task_store)
            mem_store.close()
            task_store.close()

    @pytest.fixture
    def temp_home(self, monkeypatch):
        """Monkeypatch home directory to a temp location."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            monkeypatch.setattr("ares.filesystem.Path.home", lambda: tmp)
            yield tmp

    def test_workflow_create_edit_search_delete(self, executor, temp_home):
        """Create file, edit it, search for it, then delete."""
        # Create file
        target = str(temp_home / "workflow.py")
        executor.execute("write_file", {"path": target, "content": "def greet():\n    pass\n"})

        # Edit it
        executor.execute(
            "edit_file",
            {
                "path": target,
                "old_text": "pass",
                "new_text": "return 'hello'",
            },
        )

        # Search for it
        result = executor.execute(
            "search_files", {"query": "greet", "path": str(temp_home)}
        )
        assert "workflow.py" in result

        # Get file info
        result = executor.execute("get_file_info", {"path": target})
        assert "workflow.py" in result
        assert "file" in result.lower()

        # Delete with confirm
        result = executor.execute(
            "delete_file", {"path": target, "confirm": True}
        )
        assert "Deleted" in result

    def test_workflow_organize_project_structure(self, executor, temp_home):
        """Create a project structure with nested directories and files."""
        project = temp_home / "myproject"

        # Create structure
        executor.execute("create_directory", {"path": str(project / "src")})
        executor.execute("create_directory", {"path": str(project / "tests")})
        executor.execute(
            "create_directory", {"path": str(project / "docs")}
        )

        # Create files
        executor.execute(
            "write_file",
            {
                "path": str(project / "src" / "main.py"),
                "content": "if __name__ == '__main__':\n    pass\n",
            },
        )
        executor.execute(
            "write_file",
            {
                "path": str(project / "tests" / "test_main.py"),
                "content": "import pytest\n",
            },
        )

        # List structure
        result = executor.execute("list_directory", {"path": str(project)})
        assert "src" in result
        assert "tests" in result
        assert "docs" in result

        # Glob for Python files
        result = executor.execute(
            "glob_pattern", {"pattern": "**/*.py", "path": str(project)}
        )
        assert "main.py" in result
        assert "test_main.py" in result
