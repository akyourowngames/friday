import base64
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from api_server import app


class CameraApiTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)

    def test_camera_intent_uses_semantic_router(self):
        casual = self.client.post("/camera/intent", json={"message": "hi"})
        vision = self.client.post("/camera/intent", json={"message": "what is this jarvis"})

        self.assertEqual(casual.status_code, 200)
        self.assertFalse(casual.json()["should_use_camera"])
        self.assertEqual(vision.status_code, 200)
        self.assertTrue(vision.json()["should_use_camera"])
        self.assertIn("camera_vision", vision.json()["selected"])

    def test_chat_without_image_does_not_call_camera_tool(self):
        with patch("api_server._run_camera_tool") as camera_tool, patch("api_server._run_agent") as run_agent:
            run_agent.return_value = {"response": "hello", "messages": []}
            response = self.client.post(
                "/chat/jarvis/stream",
                json={"message": "hi", "session_id": "camera-api-no-image", "tts": False},
            )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(camera_tool.called)
        self.assertIn("hello", response.text)

    def test_chat_with_image_calls_camera_tool(self):
        image = base64.b64encode(b"\xff\xd8" + (b"x" * 64) + b"\xff\xd9").decode("ascii")
        fake_result = {
            "result": {
                "description": "I can see the test frame.",
                "transcript": "I can see the test frame.",
                "prompt": "what is this",
                "provider": "fake",
                "model": "fake/vision",
                "mime_type": "image/jpeg",
                "image_bytes": 68,
                "captured_at": "now",
            },
            "meta": {"tool": "camera_vision"},
        }
        with patch("api_server._run_camera_tool", return_value=fake_result) as camera_tool:
            response = self.client.post(
                "/chat/jarvis/stream",
                json={"message": "what is this TTCAMTOKENTT", "session_id": "camera-api-image", "tts": False, "imgbase64": image},
            )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(camera_tool.called)
        self.assertIn("vision_result", response.text)
        self.assertIn("I can see the test frame.", response.text)


if __name__ == "__main__":
    unittest.main()
