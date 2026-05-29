"""Tests for the capability-routing layer.

Verifies that gateway capabilities (composio) are reachable via natural app
phrasing through semantic similarity, while casual chatter and unrelated tool
requests are not polluted with the gateway tool.

Uses the real embedder (these are routing assertions that depend on semantic
similarity), so they exercise the live path the agent uses.
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import tools  # noqa: F401  (registers all tools, including composio)
from agent.router import ToolRouter, _load_capability_rules
from agent.embedder import embed
from tools.capabilities import build_capability_rules


class CapabilityRulesTests(unittest.TestCase):
    def test_rules_load_and_map_to_registered_tools(self):
        rules = _load_capability_rules()
        self.assertTrue(rules, "capability rules should load")
        for rule in rules:
            self.assertIn("phrase", rule)
            self.assertIn("tool", rule)
            self.assertTrue(rule["phrase"])
            self.assertTrue(rule["tool"])

    def test_all_backing_tools_are_registered(self):
        from tools.registry import get_tool

        rules = _load_capability_rules()
        for rule in rules:
            self.assertIsNotNone(get_tool(rule["tool"]), f"backing tool {rule['tool']} must be registered")

    def test_composio_provider_autogenerates_slug_carrying_rules(self):
        rules = build_capability_rules()
        composio_rules = [r for r in rules if r["tool"] == "composio"]
        self.assertTrue(composio_rules, "composio capabilities should be auto-generated from the gateway")
        with_slug = [r for r in composio_rules if r.get("args", {}).get("tool_slug")]
        self.assertTrue(with_slug, "auto-generated composio rules should carry a resolved tool_slug")

    def test_static_override_carries_correct_slug(self):
        rules = build_capability_rules()
        # The static calendar override resolves to EVENTS_LIST, not a settings/get-by-id slug.
        calendar = [
            r for r in rules
            if r["tool"] == "composio" and r.get("args", {}).get("tool_slug") == "GOOGLECALENDAR_EVENTS_LIST"
        ]
        self.assertTrue(calendar, "calendar capability should resolve to GOOGLECALENDAR_EVENTS_LIST")


class CapabilityRoutingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.router = ToolRouter()

    def _selected(self, query):
        return [t["name"] for t in self.router.select_tools(query, embed(query))]

    def test_calendar_query_routes_to_composio(self):
        self.assertIn("composio", self._selected("what's on my calendar tomorrow"))

    def test_email_query_routes_to_composio(self):
        self.assertIn("composio", self._selected("check my latest emails"))

    def test_drive_query_routes_to_composio(self):
        self.assertIn("composio", self._selected("find my recent google drive files"))

    def test_notion_query_routes_to_composio(self):
        self.assertIn("composio", self._selected("search my notion for meeting notes"))

    def test_slack_query_routes_to_composio(self):
        self.assertIn("composio", self._selected("send a slack message to the team"))

    def test_weather_query_does_not_route_to_composio(self):
        names = self._selected("what is the weather in delhi")
        self.assertNotIn("composio", names)

    def test_reminder_query_does_not_route_to_composio(self):
        names = self._selected("remind me to drink water in 10 minutes")
        self.assertNotIn("composio", names)

    def test_casual_chat_does_not_route_to_composio(self):
        for chat in ("how are you doing", "what's up", "tell me a joke"):
            self.assertNotIn("composio", self._selected(chat), f"{chat!r} should not pull composio")

    def test_calendar_query_resolves_correct_slug_hint(self):
        self.router.select_tools("what's on my calendar tomorrow", embed("what's on my calendar tomorrow"))
        hint = self.router.capability_hint("composio")
        self.assertEqual(hint.get("args", {}).get("tool_slug"), "GOOGLECALENDAR_EVENTS_LIST")

    def test_email_query_resolves_correct_slug_hint(self):
        self.router.select_tools("check my latest emails", embed("check my latest emails"))
        hint = self.router.capability_hint("composio")
        self.assertEqual(hint.get("args", {}).get("tool_slug"), "GMAIL_FETCH_EMAILS")

    def test_control_query_has_no_capability_hint(self):
        self.router.select_tools("what is the weather in delhi", embed("what is the weather in delhi"))
        self.assertEqual(self.router.capability_hint("composio"), {})


if __name__ == "__main__":
    unittest.main()
