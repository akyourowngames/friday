"""Focused tests for advanced modes on existing file and runtime tools."""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

from ares.memory import MemoryStore
from ares.models import AppConfig
from ares.tools import ToolExecutor, get_tool_definitions
from ares.tools.file_upgrades import advanced_search, plan_batch


ENVELOPE_KEYS = {
    "ok", "status", "summary", "data", "artifacts", "warnings", "errors",
    "next_actions", "provenance", "metrics", "undo_id",
}


@pytest.fixture
def executor(tmp_path, fake_embedding_provider):
    memory = MemoryStore(tmp_path / "data" / "ares.db", embedding_provider=fake_embedding_provider)
    tool_executor = ToolExecutor(memory, config=AppConfig(data_dir=str(tmp_path / "data")))
    yield tool_executor
    tool_executor.close()
    memory.close()


def _tool(name: str) -> dict:
    return next(item["function"] for item in get_tool_definitions() if item["function"]["name"] == name)


def test_schemas_add_modes_without_adding_public_tool_names():
    definitions = {item["function"]["name"]: item["function"] for item in get_tool_definitions()}
    assert {"read_file", "search_files", "edit_file", "write_file", "batch_edit", "run_code", "run_command", "terminal_exec"} <= set(definitions)
    assert "mode" in definitions["search_files"]["parameters"]["properties"]
    assert "symbol" in definitions["read_file"]["parameters"]["properties"]
    assert "checkpoint_id" in definitions["run_code"]["parameters"]["properties"]
    assert "job_id" in definitions["run_command"]["parameters"]["properties"]
    assert _tool("edit_file")["parameters"]["required"] == ["path"]


def test_read_file_symbol_heading_json_cursor_and_encoding(executor, tmp_path):
    source = tmp_path / "module.py"
    source.write_text(
        "import os\n\nclass Engine:\n    def run(self):\n        return os.getcwd()\n\ndef helper():\n    return 3\n",
        encoding="utf-8",
    )
    symbol = json.loads(executor.execute("read_file", {
        "path": str(source), "mode": "symbol", "symbol": "Engine.run", "response_format": "structured",
    }))
    assert set(symbol) == ENVELOPE_KEYS
    assert "def run" in symbol["data"]["content"]
    assert symbol["data"]["encoding"] == "utf-8"

    first_page = json.loads(executor.execute("read_file", {
        "path": str(source), "mode": "lines", "num_lines": 2, "response_format": "structured",
    }))
    second_page = json.loads(executor.execute("read_file", {
        "path": str(source), "mode": "lines", "num_lines": 2,
        "cursor": first_page["data"]["cursor"], "response_format": "structured",
    }))
    assert first_page["data"]["end_line"] == 2
    assert second_page["data"]["start_line"] == 3

    markdown = tmp_path / "guide.md"
    markdown.write_text("# One\nalpha\n## Child\nbeta\n# Two\ngamma\n", encoding="utf-8")
    heading = json.loads(executor.execute("read_file", {
        "path": str(markdown), "mode": "heading", "heading": "One", "response_format": "structured",
    }))
    assert "beta" in heading["data"]["content"] and "gamma" not in heading["data"]["content"]

    config = tmp_path / "config.json"
    config.write_text('{"app": {"theme": "dark"}}', encoding="utf-8")
    selected = json.loads(executor.execute("read_file", {
        "path": str(config), "mode": "json", "selector": "app.theme", "response_format": "structured",
    }))
    assert json.loads(selected["data"]["content"]) == "dark"


def test_search_modes_grouping_changed_files_and_related_tests(executor, tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    (project / "service.py").write_text(
        "import json\n\ndef calculate_total(value):\n    # TODO: cache totals\n    return value + 1\n",
        encoding="utf-8",
    )
    (project / "test_service.py").write_text("from service import calculate_total\n", encoding="utf-8")

    symbol = json.loads(executor.execute("search_files", {
        "path": str(project), "mode": "symbol", "symbol": "calculate_total",
        "include_related_tests": True, "group_by": "file", "response_format": "structured",
    }))
    assert symbol["data"]["results"][0]["match_reason"] == "Python AST symbol definition"
    assert str((project / "test_service.py").resolve()) in symbol["data"]["related_tests"]
    assert symbol["data"]["groups"]

    todo = advanced_search({"path": str(project), "mode": "todo", "max_results": 10})
    assert todo["results"][0]["line"] == 4
    semantic = advanced_search({"path": str(project), "mode": "semantic", "query": "cache totals"})
    assert semantic["results"] and semantic["results"][0]["score"] > 0

    subprocess.run(["git", "init", "-q", str(project)], check=True)
    subprocess.run(["git", "-C", str(project), "config", "user.email", "test@example.test"], check=True)
    subprocess.run(["git", "-C", str(project), "config", "user.name", "Ares Tests"], check=True)
    subprocess.run(["git", "-C", str(project), "add", "."], check=True)
    subprocess.run(["git", "-C", str(project), "commit", "-qm", "initial"], check=True)
    (project / "service.py").write_text("# changed\n" + (project / "service.py").read_text(encoding="utf-8"), encoding="utf-8")
    changed = advanced_search({"path": str(project), "mode": "changed"})
    assert [item["relative_path"] for item in changed["results"]] == ["service.py"]


def test_project_directory_and_tree_summaries(executor, tmp_path):
    root = tmp_path / "project"
    (root / "src").mkdir(parents=True)
    (root / "src" / "app.py").write_text("print('hi')\n", encoding="utf-8")
    (root / "README.md").write_text("# Demo\n", encoding="utf-8")

    listing = json.loads(executor.execute("list_directory", {
        "path": str(root), "mode": "project", "include_summary": True,
        "sort": "size", "response_format": "structured",
    }))
    assert listing["data"]["summary"]["file_count"] == 1
    assert listing["data"]["summary"]["directory_count"] == 1

    tree = json.loads(executor.execute("file_tree", {
        "path": str(root), "mode": "project", "max_depth": 3, "response_format": "structured",
    }))
    assert tree["data"]["summary"]["file_count"] == 2
    assert any(item["relative_path"] == str(Path("src") / "app.py") for item in tree["data"]["items"])


def test_write_modes_validation_confirmation_and_verification_rollback(executor, tmp_path):
    target = tmp_path / "config.json"
    created = json.loads(executor.execute("write_file", {
        "path": str(target), "content": '{"one": 1}', "mode": "overwrite",
        "formatter": "json", "validation": "json", "response_format": "structured",
    }))
    assert created["ok"] is True and created["data"]["created"] is True

    blocked = json.loads(executor.execute("write_file", {
        "path": str(target), "content": '{"two": 2}', "mode": "overwrite",
        "response_format": "structured",
    }))
    assert blocked["status"] == "preview"
    assert target.read_text(encoding="utf-8").startswith('{\n  "one"')

    merged = json.loads(executor.execute("write_file", {
        "path": str(target), "content": '{"two": 2}', "mode": "merge",
        "validation": "json", "response_format": "structured",
    }))
    assert json.loads(target.read_text(encoding="utf-8")) == {"one": 1, "two": 2}
    assert merged["undo_id"]

    failed = json.loads(executor.execute("write_file", {
        "path": str(target), "content": '{"broken": true}', "mode": "overwrite", "confirm": True,
        "verify_command": f'"{sys.executable}" -c "import sys; sys.exit(7)"',
        "response_format": "structured",
    }))
    assert failed["ok"] is False
    assert json.loads(target.read_text(encoding="utf-8")) == {"one": 1, "two": 2}


def test_edit_modes_and_undo_metadata(executor, tmp_path):
    source = tmp_path / "module.py"
    source.write_text("def value():\n    return 1\n\ndef other():\n    return 2\n", encoding="utf-8")
    changed = json.loads(executor.execute("edit_file", {
        "path": str(source), "mode": "python_ast", "symbol": "value",
        "new_text": "def value():\n    return 42", "validation": "python",
        "response_format": "structured",
    }))
    assert changed["undo_id"]
    assert "return 42" in source.read_text(encoding="utf-8")

    regex = json.loads(executor.execute("edit_file", {
        "path": str(source), "mode": "regex", "pattern": "return 2", "replacement": "return 3",
        "response_format": "structured",
    }))
    assert regex["ok"] is True and "return 3" in source.read_text(encoding="utf-8")

    data = tmp_path / "data.json"
    data.write_text('{"a": 1}\n', encoding="utf-8")
    json.loads(executor.execute("edit_file", {
        "path": str(data), "mode": "json_fields", "fields": {"b": 2},
        "validation": "json", "response_format": "structured",
    }))
    assert json.loads(data.read_text(encoding="utf-8")) == {"a": 1, "b": 2}


def test_batch_dependency_plan_conditions_cycle_and_atomic_failure(executor, tmp_path):
    first = tmp_path / "first.txt"
    second = tmp_path / "second.txt"
    operations = [
        {"id": "create", "action": "write", "path": str(first), "content": "one"},
        {"id": "second", "depends_on": ["create"], "condition": {"not_exists": True}, "action": "write", "path": str(second), "content": "two"},
    ]
    plan = plan_batch(operations)
    assert [item["id"] for item in plan["ordered"]] == ["create", "second"]
    preview = json.loads(executor.execute("batch_edit", {
        "operations": operations, "mode": "plan", "response_format": "structured",
    }))
    assert preview["status"] == "preview"

    with pytest.raises(ValueError, match="cycle"):
        plan_batch([
            {"id": "a", "depends_on": ["b"]},
            {"id": "b", "depends_on": ["a"]},
        ])

    first.write_text("before", encoding="utf-8")
    failed = json.loads(executor.execute("batch_edit", {
        "operations": [
            {"id": "edit", "action": "edit", "path": str(first), "old_text": "before", "new_text": "after"},
            {"id": "fail", "depends_on": ["edit"], "action": "edit", "path": str(first), "old_text": "missing", "new_text": "x"},
        ],
        "response_format": "structured",
    }))
    assert failed["ok"] is False
    assert first.read_text(encoding="utf-8") == "before"


def test_named_python_cells_variables_artifacts_checkpoint_and_rollback(executor, tmp_path):
    first = json.loads(executor.execute("run_code", {
        "mode": "execute", "session_id": "analysis", "cell_name": "seed", "cwd": str(tmp_path),
        "code": "x = 10\nopen('artifact.txt', 'w').write('made')", "response_format": "structured",
    }))
    assert first["ok"] is True
    assert any(item["path"].endswith("artifact.txt") for item in first["artifacts"])

    checkpoint = json.loads(executor.execute("run_code", {
        "mode": "checkpoint", "session_id": "analysis", "response_format": "structured",
    }))
    checkpoint_id = checkpoint["data"]["checkpoint_id"]
    json.loads(executor.execute("run_code", {
        "mode": "execute", "session_id": "analysis", "code": "x = 99", "response_format": "structured",
    }))
    json.loads(executor.execute("run_code", {
        "mode": "rollback", "session_id": "analysis", "checkpoint_id": checkpoint_id, "response_format": "structured",
    }))
    variables = json.loads(executor.execute("run_code", {
        "mode": "variables", "session_id": "analysis", "response_format": "structured",
    }))
    assert variables["data"]["variables"]["x"]["repr"] == "10"
    history = json.loads(executor.execute("run_code", {
        "mode": "history", "session_id": "analysis", "response_format": "structured",
    }))
    assert [item["cell_name"] for item in history["data"]["history"]] == ["seed"]


def test_shell_discovery_git_summary_owned_job_and_terminal_history(executor, tmp_path):
    (tmp_path / "pyproject.toml").write_text("[project]\nname='demo'\n", encoding="utf-8")
    discovery = json.loads(executor.execute("run_command", {
        "mode": "discover", "cwd": str(tmp_path), "response_format": "structured",
    }))
    assert "pytest" in discovery["data"]["commands"]

    started = json.loads(executor.execute("run_command", {
        "mode": "start", "cwd": str(tmp_path),
        "command": f'"{sys.executable}" -u -c "import time; print(123); time.sleep(30)"',
        "response_format": "structured",
    }))
    job_id = started["data"]["job"]["job_id"]
    time.sleep(0.5)
    attached = json.loads(executor.execute("run_command", {
        "mode": "attach", "job_id": job_id, "response_format": "structured",
    }))
    assert "123" in attached["data"]["job"]["stdout"]
    stopped = json.loads(executor.execute("run_command", {
        "mode": "stop", "job_id": job_id, "response_format": "structured",
    }))
    assert stopped["data"]["job"]["status"] == "stopped"

    command = json.loads(executor.execute("terminal_exec", {
        "mode": "execute", "session_id": "terminal", "command": "echo terminal-ok",
        "cwd": str(tmp_path), "response_format": "structured",
    }))
    assert command["ok"] is True
    history = json.loads(executor.execute("terminal_exec", {
        "mode": "history", "session_id": "terminal", "response_format": "structured",
    }))
    assert history["data"]["history"][0]["command"] == "echo terminal-ok"


def test_legacy_file_and_runtime_defaults_remain_text(executor, tmp_path):
    target = tmp_path / "legacy.txt"
    assert "Created" in executor.execute("write_file", {"path": str(target), "content": "legacy"})
    assert "legacy" in executor.execute("read_file", {"path": str(target)})
    assert "legacy" in executor.execute("search_files", {"path": str(tmp_path), "query": "legacy"})
    assert "legacy-code" in executor.execute("run_code", {"code": "print('legacy-code')"})
