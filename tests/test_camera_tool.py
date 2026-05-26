import base64
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import tools
import tools.camera as camera_mod
from tools.registry import execute_tool, get_tool


class CameraVisionToolTests(unittest.TestCase):
    def test_camera_tool_registered(self):
        self.assertIsNotNone(get_tool("camera_vision"))

    def test_camera_rejects_invalid_base64(self):
        result = execute_tool("camera_vision", image_base64="not image bytes", response_format="structured")

        self.assertEqual(result["error"]["code"], "INVALID_BASE64")

    def test_camera_structured_success_from_provider(self):
        original = camera_mod._call_nim_vision
        captured = {}

        def fake_call(image_base64, mime_type, prompt, timeout_ms):
            captured["image_base64"] = image_base64
            captured["mime_type"] = mime_type
            captured["prompt"] = prompt
            captured["timeout_ms"] = timeout_ms
            return "I can see a test frame.", "fake/vision", [{"model": "fake/vision", "status": "success"}]

        image = base64.b64encode(b"\xff\xd8" + (b"x" * 64) + b"\xff\xd9").decode("ascii")
        try:
            camera_mod._call_nim_vision = fake_call
            result = execute_tool(
                "camera_vision",
                image_base64=image,
                prompt="What is visible?",
                response_format="structured",
            )
        finally:
            camera_mod._call_nim_vision = original

        self.assertIn("result", result)
        self.assertEqual(result["result"]["description"], "I can see a test frame.")
        self.assertEqual(result["result"]["model"], "fake/vision")
        self.assertEqual(captured["mime_type"], "image/jpeg")
        self.assertEqual(captured["prompt"], "What is visible?")

    def test_camera_accepts_data_url_mime(self):
        original = camera_mod._call_nim_vision
        captured = {}

        def fake_call(_image_base64, mime_type, _prompt, _timeout_ms):
            captured["mime_type"] = mime_type
            return "PNG frame described.", "fake/vision", [{"model": "fake/vision", "status": "success"}]

        image = base64.b64encode(b"\x89PNG" + (b"x" * 64)).decode("ascii")
        try:
            camera_mod._call_nim_vision = fake_call
            result = execute_tool(
                "camera_vision",
                image_base64="data:image/png;base64," + image,
                response_format="structured",
            )
        finally:
            camera_mod._call_nim_vision = original

        self.assertEqual(result["result"]["description"], "PNG frame described.")
        self.assertEqual(captured["mime_type"], "image/png")


if __name__ == "__main__":
    unittest.main()
