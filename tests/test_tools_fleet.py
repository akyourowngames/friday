import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import tools
from tools.image import imagine, images_manage
from tools.manifest_audit import tool_manifest_audit
from tools.registry import get_tool


class ToolsFleetPolishTests(unittest.TestCase):
    def test_memory_and_image_tools_registered(self):
        for name in ("memory_assess", "memory_recall", "memory_remember", "memory_forget", "imagine", "gallery"):
            self.assertIsNotNone(get_tool(name))

    def test_manifest_audit_structured_success(self):
        result = tool_manifest_audit(".", response_format="structured", include_schema=True)
        self.assertIn(result["result"]["status"], ("success", "partial"))
        self.assertIn("observed_modules", result["result"])

    def test_imagine_rejects_short_prompt_structured(self):
        result = imagine("hi", response_format="structured")
        self.assertEqual(result["error"]["code"], "SHORT_PROMPT")

    def test_gallery_list_structured_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            original = Path(__file__).resolve().parent.parent / "tools" / "image.py"
            import tools.image as image_mod

            old_dir = image_mod._IMAGE_DIR
            image_mod._IMAGE_DIR = Path(tmp)
            try:
                result = images_manage("list", response_format="structured")
            finally:
                image_mod._IMAGE_DIR = old_dir
        self.assertEqual(result["result"]["count"], 0)

    def test_gallery_trace_emits_json(self):
        stream = io.StringIO()
        with redirect_stdout(stream):
            tool_manifest_audit(".", response_format="legacy", trace_enabled=True, max_items=20)
        trace = json.loads(stream.getvalue().strip())
        self.assertEqual(trace["tool"], "tool_manifest_audit")


if __name__ == "__main__":
    unittest.main()
