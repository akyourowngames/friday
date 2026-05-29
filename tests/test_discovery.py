"""Tests for progressive tool disclosure (find_tools + load_tool).

Verifies the discovery escape hatch that lets the model find and load any tool
the router did not pre-select, implementing the GitHub/Anthropic code-execution
pattern's progressive disclosure without injecting the full catalog upfront.
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import tools  # noqa: F401  (registers all tools)
from tools.registry import execute_tool, get_tool
import agent.core as core


class DiscoveryRegistrationTests(unittest.TestCase):
    def test_meta_tools_registered(self):
        self.assertIsNotNone(get_tool("find_tools"))
        self.assertIsNotNone(get_tool("load_tool"))


class FindToolsTests(unittest.TestCase):
    def test_find_email_capability(self):
        result = execute_tool("find_tools", query="read my email inbox", limit=5, response_format="structured")
        matches = result["result"]["matches"]
        self.assertTrue(matches)
        slugs = [m["tool_slug"] for m in matches]
        self.assertIn("GMAIL_FETCH_EMAILS", slugs)

    def test_find_weather_tool(self):
        result = execute_tool("find_tools", query="what is the weather today", limit=5, response_format="structured")
        backing = [m["backing_tool"] for m in result["result"]["matches"]]
        self.assertIn("weather", backing)

    def test_empty_query_rejected(self):
        result = execute_tool("find_tools", query="", response_format="structured")
        self.assertEqual(result["error"]["code"], "EMPTY_QUERY")

    def test_meta_tools_not_discoverable(self):
        result = execute_tool("find_tools", query="find tools to load tools", limit=10, response_format="structured")
        backing = [m["backing_tool"] for m in result["result"]["matches"]]
        self.assertNotIn("find_tools", backing)
        self.assertNotIn("load_tool", backing)


class LoadToolTests(unittest.TestCase):
    def test_load_registered_tool(self):
        result = execute_tool("load_tool", names="weather", response_format="structured")
        self.assertIn("weather", result["result"]["loaded_names"])

    def test_load_resolves_capability_display_name(self):
        result = execute_tool("load_tool", names="composio:GMAIL_FETCH_EMAILS", response_format="structured")
        loaded = result["result"]["loaded"]
        self.assertEqual(loaded[0]["name"], "composio")
        self.assertEqual(loaded[0]["tool_slug"], "GMAIL_FETCH_EMAILS")

    def test_load_unknown_rejected(self):
        result = execute_tool("load_tool", names="nonexistent_tool_xyz", response_format="structured")
        self.assertEqual(result["error"]["code"], "UNKNOWN_TOOLS")

    def test_load_multiple_mixed(self):
        result = execute_tool("load_tool", names="weather, nonexistent_xyz", response_format="structured")
        self.assertIn("weather", result["result"]["loaded_names"])
        self.assertIn("nonexistent_xyz", result["result"]["unknown"])


class CoreHelperTests(unittest.TestCase):
    def test_disclosure_tool_names_default(self):
        names = core._progressive_disclosure_tool_names()
        self.assertIn("find_tools", names)
        self.assertIn("load_tool", names)

    def test_loaded_tools_parser_structured(self):
        result = execute_tool("load_tool", names="composio:GMAIL_FETCH_EMAILS", response_format="structured")
        import json

        entries = core._loaded_tools_from_result(json.dumps(result))
        self.assertEqual(entries[0]["name"], "composio")
        self.assertEqual(entries[0]["tool_slug"], "GMAIL_FETCH_EMAILS")

    def test_loaded_tools_parser_handles_garbage(self):
        self.assertEqual(core._loaded_tools_from_result("not json"), [])
        self.assertEqual(core._loaded_tools_from_result(""), [])

    def test_find_tools_result_can_make_top_match_callable(self):
        result = execute_tool("find_tools", query="check current playlist", limit=5, response_format="structured")

        entries = core._discovered_tools_from_result(result)

        self.assertEqual(entries[0]["name"], "playlist")


if __name__ == "__main__":
    unittest.main()
