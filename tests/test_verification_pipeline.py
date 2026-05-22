import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import tools
from tools.registry import get_tool
from tools.verification_pipeline import tool_verification_pipeline


def _python_command(script: str) -> str:
    if sys.platform == "win32":
        return f'& "{sys.executable}" -c "{script}"; exit $LASTEXITCODE'
    return f'"{sys.executable}" -c "{script}"'


class VerificationPipelineToolTests(unittest.TestCase):
    def test_tool_is_registered_after_tools_import(self):
        self.assertIsNotNone(tools)
        self.assertIsNotNone(get_tool("tool_verification_pipeline"))

    def test_pipeline_runs_markdown_command_with_structured_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pipeline = root / "pipeline.md"
            command = _python_command("print('pipeline-ok')")
            pipeline.write_text(
                "\n".join([
                    "# Test Pipeline",
                    f"- command: `{command}`",
                    "  required: true",
                    "  reason: proves command execution evidence is captured",
                ]),
                encoding="utf-8",
            )

            stream = io.StringIO()
            with redirect_stdout(stream):
                result = tool_verification_pipeline(
                    root=str(root),
                    pipeline_path="pipeline.md",
                    timeout_ms=5000,
                    response_format="structured",
                    trace_enabled=True,
                )

        trace = json.loads(stream.getvalue().strip())
        self.assertEqual(result["result"]["status"], "success")
        self.assertEqual(result["result"]["ship_decision"], "ship")
        self.assertEqual(result["result"]["passed"], 1)
        self.assertIn("pipeline-ok", result["result"]["checks"][0]["stdout"])
        self.assertEqual(trace["tool"], "tool_verification_pipeline")
        self.assertEqual(trace["status"], "SUCCESS")

    def test_dry_run_does_not_execute_markdown_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            marker = root / "marker.txt"
            pipeline = root / "pipeline.md"
            script = f"from pathlib import Path; Path({str(marker)!r}).write_text('ran')"
            pipeline.write_text(
                "\n".join([
                    "# Test Pipeline",
                    f"- command: `{_python_command(script)}`",
                    "  required: true",
                ]),
                encoding="utf-8",
            )

            result = tool_verification_pipeline(
                root=str(root),
                pipeline_path="pipeline.md",
                dry_run=True,
                response_format="structured",
            )

        self.assertFalse(marker.exists())
        self.assertEqual(result["result"]["status"], "dry_run")
        self.assertEqual(result["result"]["checks"][0]["status"], "DRY_RUN")
        self.assertEqual(result["result"]["run_count"], 0)

    def test_pipeline_file_must_stay_inside_root(self):
        with tempfile.TemporaryDirectory() as root_tmp, tempfile.TemporaryDirectory() as plan_tmp:
            root = Path(root_tmp)
            outside = Path(plan_tmp) / "outside.md"
            command = _python_command("print('outside')")
            outside.write_text(
                f"- command: `{command}`",
                encoding="utf-8",
            )

            result = tool_verification_pipeline(
                root=str(root),
                pipeline_path=str(outside),
                response_format="structured",
            )

        self.assertEqual(result["error"]["code"], "PIPELINE_OUT_OF_SCOPE")

    def test_failed_required_check_holds_ship_decision(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pipeline = root / "pipeline.md"
            pipeline.write_text(
                "\n".join([
                    "# Test Pipeline",
                    f"- command: `{_python_command('import sys; sys.exit(3)')}`",
                    "  required: true",
                ]),
                encoding="utf-8",
            )

            result = tool_verification_pipeline(
                root=str(root),
                pipeline_path="pipeline.md",
                timeout_ms=5000,
                response_format="structured",
            )

        self.assertEqual(result["result"]["status"], "failed")
        self.assertEqual(result["result"]["ship_decision"], "hold")
        self.assertNotEqual(result["result"]["checks"][0]["exit_code"], 0)


if __name__ == "__main__":
    unittest.main()
