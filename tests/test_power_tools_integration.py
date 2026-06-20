"""Integration tests for power tools through ToolExecutor."""

import os
import pytest
from ares.tools import ToolExecutor, get_tool_definitions
from ares.memory import MemoryStore
from ares.tasks import TaskStore


@pytest.fixture
def executor():
    memory = MemoryStore()
    tasks = TaskStore()
    return ToolExecutor(memory_store=memory, task_store=tasks)


class TestToolDefinitions:
    """Verify all power tools are registered in get_tool_definitions()."""

    def test_all_power_tools_registered(self):
        defs = get_tool_definitions()
        names = {d["function"]["name"] for d in defs}
        power_tools = {"run_code", "run_command", "generate_image",
                       "image_info", "resize_image", "convert_image", "crop_image"}
        assert power_tools.issubset(names)

    def test_total_tool_count_increased(self):
        defs = get_tool_definitions()
        assert len(defs) >= 29


class TestRunCodeViaExecutor:
    """Test run_code through ToolExecutor dispatch."""

    def test_execute_run_code(self, executor):
        result = executor.execute("run_code", {"code": "print(2 + 2)"})
        assert "4" in result
        assert "Exit code: 0" in result

    def test_execute_run_code_timeout(self, executor):
        result = executor.execute("run_code", {
            "code": "import time; time.sleep(60)",
            "timeout": 2,
        })
        assert "timed out" in result.lower() or "timeout" in result.lower()


class TestRunCommandViaExecutor:
    """Test run_command through ToolExecutor dispatch."""

    def test_execute_run_command(self, executor):
        result = executor.execute("run_command", {"command": "echo hello"})
        assert "hello" in result


class TestImageEditViaExecutor:
    """Test image editing through ToolExecutor dispatch."""

    def test_execute_image_info(self, executor, tmp_path):
        from PIL import Image
        img_path = str(tmp_path / "test.png")
        Image.new("RGB", (100, 50)).save(img_path)

        result = executor.execute("image_info", {"path": img_path})
        assert "100\u00d750" in result
        assert "PNG" in result

    def test_execute_resize_image(self, executor, tmp_path):
        from PIL import Image
        img_path = str(tmp_path / "test.png")
        Image.new("RGB", (200, 100)).save(img_path)

        result = executor.execute("resize_image", {
            "path": img_path,
            "width": 100,
        })
        assert "Resized" in result

    def test_execute_crop_image(self, executor, tmp_path):
        from PIL import Image
        img_path = str(tmp_path / "test.png")
        Image.new("RGB", (200, 200)).save(img_path)

        result = executor.execute("crop_image", {
            "path": img_path,
            "left": 10,
            "top": 10,
            "right": 100,
            "bottom": 100,
        })
        assert "Cropped" in result

    def test_unknown_tool_returns_error(self, executor):
        with pytest.raises(ValueError, match="Unknown tool"):
            executor.execute("nonexistent_tool", {})
