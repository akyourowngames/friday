"""Scripted KING assistant gauntlet.

Default mode is deterministic and exercises the same contracts the live CLI depends on.
Use --cli-basic for a real subprocess conversation, and --full-live for slower network/tool turns.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agent.core import _local_time_context, _tool_call_grounded, _tool_result_content, _time_of_day
from memory.brain import Brain
import memory.brain as brain_mod
from tools.files import file_list, file_read, file_write
from tools.hackernews import hackernews
from tools.reddit import reddit
from tools.registry import execute_tool
from tools.terminal import _normalize_launch_target, _strip_ansi, terminal
from tools.web import web_search


@dataclass
class CheckResult:
    name: str
    passed: bool
    detail: str


def _fake_embed(texts, normalize=True):
    if isinstance(texts, str):
        return np.ones(4, dtype=np.float32)
    return np.ones((len(texts), 4), dtype=np.float32)


def _run_check(name: str, fn) -> CheckResult:
    try:
        detail = fn()
        return CheckResult(name, True, detail or "ok")
    except AssertionError as exc:
        return CheckResult(name, False, str(exc))
    except Exception as exc:
        return CheckResult(name, False, f"{exc.__class__.__name__}: {exc}")


def _assert_no_raw_dump(text: str):
    blocked = ["{'result'", '{"result"', "TOOL TRACE", "function_call", "tool_calls"]
    for marker in blocked:
        assert marker not in text, f"raw payload leaked marker {marker}"


def check_time_context():
    context = _local_time_context(datetime(2026, 5, 20, 16, 15))
    assert "Current local time of day: afternoon" in context
    assert "Do not use a different time-of-day greeting" in context
    return "afternoon context present"


def check_memory_controls():
    original = {
        "memory_dir": brain_mod.settings.memory_dir,
        "memory_backup_dir": brain_mod.settings.memory_backup_dir,
        "embed": brain_mod.embed,
        "MEMORY_DIR": brain_mod.MEMORY_DIR,
        "BACKUP_DIR": brain_mod.BACKUP_DIR,
    }
    with tempfile.TemporaryDirectory() as tmp:
        memory_dir = Path(tmp) / "memories"
        backup_dir = Path(tmp) / "backups"
        brain_mod.settings.memory_dir = str(memory_dir)
        brain_mod.settings.memory_backup_dir = str(backup_dir)
        brain_mod.MEMORY_DIR = memory_dir
        brain_mod.BACKUP_DIR = backup_dir
        brain_mod.embed = _fake_embed
        try:
            brain = Brain()
            kept = brain.remember("User prefers grounded assistant answers")
            trash = brain.remember("User corrected the greeting from morning to afternoon")
            removed = brain.forget("grounded assistant answers")
        finally:
            brain_mod.settings.memory_dir = original["memory_dir"]
            brain_mod.settings.memory_backup_dir = original["memory_backup_dir"]
            brain_mod.MEMORY_DIR = original["MEMORY_DIR"]
            brain_mod.BACKUP_DIR = original["BACKUP_DIR"]
            brain_mod.embed = original["embed"]
    assert kept["stored"], "stable fact was not stored"
    assert not trash["stored"], "temporary greeting correction was stored"
    assert removed["status"] == "removed", "forget did not remove exact memory"
    return "remember, filter, forget"


def check_structured_tools():
    web = web_search("", response_format="structured")
    hn = hackernews(action="search", response_format="structured")
    rd = reddit(action="search", response_format="structured")
    read = file_read("requirements.txt", response_format="structured")
    listing = file_list(".", limit=5, response_format="structured")
    write = file_write("storage/gauntlet-dry-run.txt", "ok", dry_run=True, response_format="structured")
    shell = terminal("echo gauntlet", timeout_ms=5000, response_format="structured")

    assert web["error"]["code"] == "EMPTY_QUERY"
    assert hn["error"]["code"] == "MISSING_QUERY"
    assert rd["error"]["code"] == "MISSING_QUERY"
    assert read["result"]["readable"]
    assert listing["result"]["items"]
    assert write["result"]["dry_run"]
    assert shell["result"]["exit_code"] == 0
    return "web, reddit, hn, files, terminal evidence"


def check_terminal_grounding_and_alias():
    assert _tool_call_grounded(
        "open me notepad",
        {"command": "start notepad"},
        [],
        "terminal",
    )
    assert not _tool_call_grounded(
        "open it",
        {"command": "start notepad"},
        [],
        "terminal",
    )
    return f"terminal alias policy loaded: {bool(_normalize_launch_target('start terminal'))}"


def check_tool_content_json():
    content = _tool_result_content({"result": {"title": "Evidence", "url": "https://example.com"}})
    parsed = json.loads(content)
    assert parsed["result"]["title"] == "Evidence"
    assert "'result'" not in content
    return "structured payload serialized as JSON"


def run_fast() -> list[CheckResult]:
    checks = [
        ("time_context", check_time_context),
        ("memory_controls", check_memory_controls),
        ("structured_tools", check_structured_tools),
        ("terminal_grounding_and_alias", check_terminal_grounding_and_alias),
        ("tool_content_json", check_tool_content_json),
    ]
    return [_run_check(name, fn) for name, fn in checks]


def run_cli(lines: list[str], timeout_seconds: int = 180) -> str:
    env = os.environ.copy()
    env["KING_MEMORY_STORE_ENABLED"] = "false"
    process = subprocess.run(
        [sys.executable, "main.py"],
        input="\n".join(lines + ["/exit"]) + "\n",
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=timeout_seconds,
        env=env,
    )
    output = _strip_ansi((process.stdout or "") + "\n" + (process.stderr or ""))
    assert process.returncode == 0, output[-4000:]
    return output


def run_cli_basic() -> list[CheckResult]:
    def check():
        output = run_cli(["hi bud", "is it good morning", "fuck it its good afternoon", "/memory 5"])
        _assert_no_raw_dump(output)
        lower = output.lower()
        current_tod = _time_of_day(datetime.now())
        assert current_tod in lower, f"CLI did not ground time-of-day correction (expected {current_tod} in output)"
        assert "warning:" not in lower, "runtime warning leaked to user"
        assert "memory:" in lower, "/memory did not render memory state"
        return "real CLI greeting and memory command"

    return [_run_check("cli_basic", check)]


def run_full_live() -> list[CheckResult]:
    scenarios = [
        (
            "cli_web_hn_reddit",
            [
                "search web for latest python release, one result",
                "search hacker news for python, one result",
                "search reddit for python, one result",
            ],
            "search",
        ),
        (
            "cli_file_and_ambiguous_action",
            [
                "list files in this folder",
                "open it",
            ],
            "files",
        ),
    ]
    results = []
    for name, lines, expected in scenarios:
        def check(lines=lines, expected=expected):
            output = run_cli(lines, timeout_seconds=300)
            _assert_no_raw_dump(output)
            assert expected in output.lower(), f"expected evidence containing {expected}"
            return "real CLI scenario"
        results.append(_run_check(name, check))
    return results


def print_results(results: list[CheckResult]) -> int:
    failures = [result for result in results if not result.passed]
    for result in results:
        status = "PASS" if result.passed else "FAIL"
        print(f"{status} {result.name}: {result.detail}")
    if failures:
        print(f"\n{len(failures)} gauntlet check(s) failed")
        return 1
    print(f"\n{len(results)} gauntlet check(s) passed")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Run KING live assistant gauntlet")
    parser.add_argument("--cli-basic", action="store_true", help="Run real CLI greeting and memory scenario")
    parser.add_argument("--full-live", action="store_true", help="Run slower real CLI tool scenarios")
    args = parser.parse_args()

    results = run_fast()
    if args.cli_basic:
        results.extend(run_cli_basic())
    if args.full_live:
        results.extend(run_full_live())
    return print_results(results)


if __name__ == "__main__":
    raise SystemExit(main())
