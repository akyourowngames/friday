import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import tools
from tools import browser as browser_mod
from tools import keyboard as keyboard_mod
from tools import navigator as navigator_mod
from tools import system_control as system_mod
from tools.registry import get_tool


class SystemControlToolTests(unittest.TestCase):
    def test_registered(self):
        self.assertIsNotNone(get_tool("system_control"))

    def test_wrong_config_path_falls_back_to_bundled_catalog(self):
        catalog, error, resolved = system_mod._load_controls("SYSTEM_CONTROLS.md")
        self.assertIsNone(error)
        self.assertIn("volume_up", catalog.get("actions", {}))
        self.assertTrue(resolved and resolved.exists())

    @patch.object(system_mod, "_wmi_brightness_is_effective", return_value=False)
    def test_brightness_down_uses_hardware_when_wmi_inert(self, _mock_wmi):
        system_mod._WMI_BRIGHTNESS_PROBE["checked"] = False
        system_mod._WMI_BRIGHTNESS_PROBE["effective"] = None
        with patch.object(system_mod, "_brightness_get", side_effect=[(79, ""), (79, "")]):
            with patch.object(system_mod, "_press_media_key", return_value=(True, "")) as mock_press:
                result = system_mod.system_control("brightness_down", response_format="structured")
        outcome = result["result"]["outcome"]
        self.assertEqual(outcome["path"], "hardware_key")
        mock_press.assert_called_once()

    def test_banter_clears_tool_selection(self):
        from agent.core import _filter_tools_for_conversation
        from agent.embedder import embed

        tools = [{"name": "memory_recall"}, {"name": "web_search"}]
        filtered = _filter_tools_for_conversation("you are being smarter huh", embed("you are being smarter huh"), tools)
        self.assertEqual(filtered, [])

    def test_named_tool_request_survives_banter_filter(self):
        from agent.core import _filter_tools_for_conversation
        from agent.embedder import embed

        selected_tools = [get_tool("reddit")]
        filtered = _filter_tools_for_conversation(
            "fetch me popular reddit threads",
            embed("fetch me popular reddit threads"),
            selected_tools,
        )

        self.assertEqual([tool["name"] for tool in filtered], ["reddit"])

    def test_identity_question_does_not_force_system_control(self):
        from agent.core import _ensure_local_system_control_tool
        from agent.embedder import embed

        selected = _ensure_local_system_control_tool([], "who i am king", embed("who i am king"), [])

        self.assertEqual(selected, [])

    def test_unrelated_followup_does_not_force_system_control(self):
        from agent.core import _ensure_local_system_control_tool
        from agent.embedder import embed

        messages = [
            {"role": "user", "content": "fetch me popular reddit threads"},
            {"role": "assistant", "content": "I cannot verify Reddit without a tool result."},
        ]
        selected = [get_tool("playlist")]
        result = _ensure_local_system_control_tool(
            selected,
            "whats in playlist",
            embed("Earlier: fetch me popular reddit threads\nNow: whats in playlist"),
            messages,
        )

        self.assertEqual([tool["name"] for tool in result], ["playlist"])

    def test_forced_call_on_correction_nope(self):
        from agent.core import _forced_local_system_control_call
        from agent.embedder import embed

        messages = [
            {"role": "user", "content": "decrease brightness"},
            {"role": "assistant", "content": "The brightness has been decreased."},
        ]
        schemas = [{"function": {"name": "system_control", "parameters": {}}}]
        forced = _forced_local_system_control_call("nope", schemas, messages, embed("nope"))
        self.assertIsNotNone(forced)
        self.assertEqual(forced["name"], "system_control")
        self.assertIn("brightness_down", forced["arguments"])

    def test_repair_strips_hallucinated_config_path(self):
        from agent.core import _repair_system_control_args

        repaired = _repair_system_control_args(
            "system_control",
            {"action": "brightness_down", "config_path": "SYSTEM_CONTROLS.md"},
            "decrease brightness",
        )
        self.assertNotIn("config_path", repaired)
        self.assertEqual(repaired["action"], "brightness_down")

    def test_load_controls_parses_actions(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "controls.md"
            path.write_text(
                "\n".join(
                    [
                        "# Controls",
                        "### volume_up",
                        "- method: media_key",
                        "- key: volume_up",
                        "- step: 2",
                    ]
                ),
                encoding="utf-8",
            )
            catalog, error, _resolved = system_mod._load_controls(str(path))
        self.assertIsNone(error)
        self.assertEqual(catalog["actions"]["volume_up"]["method"], "media_key")

    def test_action_alias_resolves_decrease_brightness(self):
        self.assertEqual(system_mod._normalize_action_name("decrease_brightness"), "brightness_down")

    def test_validator_ignores_invalid_optional_level(self):
        from agent.validator import ToolValidator

        validator = ToolValidator()
        ok, result = validator.validate_and_execute(
            "system_control",
            {"action": "brightness_down", "level": "decrease", "response_format": "structured"},
        )
        self.assertTrue(ok)
        self.assertEqual(result["result"]["action"], "brightness_down")

    @patch.object(system_mod, "_press_media_key", return_value=(True, ""))
    def test_volume_up_structured(self, mock_press):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "controls.md"
            path.write_text(
                "\n".join(
                    [
                        "### volume_up",
                        "- method: media_key",
                        "- key: volume_up",
                        "- step: 1",
                    ]
                ),
                encoding="utf-8",
            )
            result = system_mod.system_control("volume_up", config_path=str(path), response_format="structured")
        self.assertEqual(result["result"]["action"], "volume_up")
        mock_press.assert_called_once()

    def test_unknown_action_structured(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "controls.md"
            path.write_text("### volume_up\n- method: media_key\n- key: volume_up\n", encoding="utf-8")
            result = system_mod.system_control("brightness_down", config_path=str(path), response_format="structured")
        self.assertEqual(result["error"]["code"], "ACTION_NOT_FOUND")


class KeyboardToolTests(unittest.TestCase):
    def test_registered(self):
        self.assertIsNotNone(get_tool("keyboard_press"))
        self.assertIsNotNone(get_tool("keyboard_shortcut"))

    def test_parse_keys(self):
        vks, error = keyboard_mod._parse_keys("ctrl+c")
        self.assertIsNone(error)
        self.assertEqual(len(vks), 2)

    @patch.object(keyboard_mod, "_send_keys", return_value=(True, ""))
    def test_shortcut_from_markdown(self, mock_send):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "keys.md"
            path.write_text(
                "\n".join(
                    [
                        "### copy",
                        "- keys: ctrl+c",
                    ]
                ),
                encoding="utf-8",
            )
            result = keyboard_mod.keyboard_shortcut("copy", config_path=str(path), response_format="structured")
        self.assertTrue(result["result"]["sent"])
        mock_send.assert_called_once()


class BrowserReadUpgradeTests(unittest.TestCase):
    def test_dom_policy_loads_limits(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "dom.md"
            path.write_text(
                "\n".join(
                    [
                        "## Limits",
                        "- max_blocks: 12",
                        "- max_block_chars: 100",
                    ]
                ),
                encoding="utf-8",
            )
            policy = browser_mod._load_dom_policy(str(path))
        self.assertEqual(policy["max_blocks"], 12)

    def test_browser_read_page_uses_playwright_dom(self):
        original_load = browser_mod._load_page

        def fake_load(url, engine, timeout_ms, wait_until, max_text_chars, fields, storage_state="", read_mode="fields", dom_policy=None):
            return {
                "requested_url": url,
                "final_url": url,
                "status_code": 200,
                "title": "Read Test",
                "text": "Hello readable page body content here",
                "text_truncated": False,
                "meta": [],
                "selector_values": {},
                "engine_used": "playwright",
                "dom": {
                    "blocks": [{"tag": "p", "text": "Hello readable page body content here", "depth": 1}],
                    "links": [{"text": "Home", "href": "/"}],
                    "headings": [{"tag": "h1", "text": "Read Test", "depth": 0}],
                    "block_count": 1,
                },
                "dom_block_count": 1,
            }, None

        try:
            browser_mod._load_page = fake_load
            result = browser_mod.browser_read_page(
                url="https://example.com/article",
                read_mode="full",
                response_format="structured",
            )
        finally:
            browser_mod._load_page = original_load

        self.assertEqual(result["meta"]["tool"], "browser_read_page")
        self.assertEqual(result["result"]["read_mode"], "full")
        self.assertEqual(result["result"]["dom_block_count"], 1)
        self.assertIn("Hello", result["result"]["full_text"])


class NavigatorActionTests(unittest.TestCase):
    @patch.object(navigator_mod, "_geocode")
    def test_geocode_action(self, mock_geocode):
        mock_geocode.return_value = (
            {"name": "Tokyo", "lat": 35.6, "lon": 139.7, "display_name": "Tokyo", "precision": {}},
            "",
        )
        result = navigator_mod.navigator("Tokyo", action="geocode", response_format="structured")
        self.assertEqual(result["result"]["action"], "geocode")
        self.assertEqual(result["result"]["place"]["name"], "Tokyo")

    @patch.object(navigator_mod, "_geocode")
    def test_straight_line_action(self, mock_geocode):
        def fake_geocode(query, timeout_seconds):
            if query == "A":
                return {"name": "A", "lat": 0.0, "lon": 0.0, "display_name": "A", "precision": {}}, ""
            return {"name": "B", "lat": 0.0, "lon": 1.0, "display_name": "B", "precision": {}}, ""

        mock_geocode.side_effect = fake_geocode
        result = navigator_mod.navigator("A", "B", action="straight_line", response_format="structured")
        self.assertEqual(result["result"]["action"], "straight_line")
        self.assertIn("distance_km", result["result"]["straight_line"])


if __name__ == "__main__":
    unittest.main()
