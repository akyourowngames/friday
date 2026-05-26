import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import api_server
from api_server import app


class ComposioApiTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)

    def test_composio_status_returns_gateway_result(self):
        tool_result = {
            "result": {
                "action": "status",
                "enabled": True,
                "api_key_present": True,
                "session_id_present": False,
                "enabled_toolkits": ["github"],
                "enabled_tools": [{"slug": "GITHUB_GET_A_REPOSITORY", "toolkit": "github", "risk": "read", "note": ""}],
            },
            "meta": {"tool": "composio"},
        }
        with patch("api_server.execute_tool", return_value=tool_result) as run_tool:
            response = self.client.get("/composio/status")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["api_key_present"])
        self.assertEqual(payload["enabled_toolkits"], ["github"])
        self.assertEqual(run_tool.call_args.kwargs["action"], "status")

    def test_composio_action_maps_missing_key_error(self):
        tool_result = {
            "error": {"code": "MISSING_API_KEY", "message": "Composio API key is not configured."},
            "meta": {"tool": "composio"},
        }
        with patch("api_server.execute_tool", return_value=tool_result):
            response = self.client.post("/composio/action", json={"action": "create_session"})

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["detail"]["code"], "MISSING_API_KEY")

    def test_composio_policy_tool_updates_markdown_allow_list(self):
        with tempfile.TemporaryDirectory() as tmp:
            policy = Path(tmp) / "COMPOSIO_GATEWAY.md"
            policy.write_text(
                "\n".join(
                    [
                        "# Composio Gateway",
                        "",
                        "## Runtime",
                        "",
                        "- enabled: true",
                        "",
                        "## Enabled Toolkits",
                        "",
                        "- github",
                        "",
                        "## Enabled Tools",
                        "",
                        "- GITHUB_GET_A_REPOSITORY | toolkit: github | risk: read | enabled: true | note: existing",
                    ]
                ),
                encoding="utf-8",
            )
            old_file = api_server.settings.composio_policy_file
            try:
                api_server.settings.composio_policy_file = str(policy)
                response = self.client.post(
                    "/composio/policy/tool",
                    json={
                        "slug": "GITHUB_LIST_STARGAZERS",
                        "toolkit": "github",
                        "risk": "read",
                        "enabled": True,
                        "note": "stars",
                    },
                )
            finally:
                api_server.settings.composio_policy_file = old_file

            text = policy.read_text(encoding="utf-8")

        self.assertEqual(response.status_code, 200)
        self.assertIn("GITHUB_LIST_STARGAZERS", text)
        self.assertIn("stars", text)
        self.assertIn("GITHUB_LIST_STARGAZERS", [item["slug"] for item in response.json()["enabled_tools"]])

    def test_composio_policy_tools_bulk_updates_markdown_allow_list(self):
        with tempfile.TemporaryDirectory() as tmp:
            policy = Path(tmp) / "COMPOSIO_GATEWAY.md"
            policy.write_text(
                "\n".join(
                    [
                        "# Composio Gateway",
                        "",
                        "## Runtime",
                        "",
                        "- enabled: true",
                        "",
                        "## Enabled Toolkits",
                        "",
                        "- github",
                        "",
                        "## Enabled Tools",
                        "",
                        "- GITHUB_GET_A_REPOSITORY | toolkit: github | risk: read | enabled: true | note: existing",
                    ]
                ),
                encoding="utf-8",
            )
            old_file = api_server.settings.composio_policy_file
            try:
                api_server.settings.composio_policy_file = str(policy)
                response = self.client.post(
                    "/composio/policy/tools",
                    json={
                        "tools": [
                            {"slug": "GITHUB_LIST_STARGAZERS", "toolkit": "github", "risk": "read", "enabled": True, "note": "stars"},
                            {"slug": "SLACK_LIST_CHANNELS", "toolkit": "slack", "risk": "read", "enabled": True, "note": "channels"},
                        ]
                    },
                )
            finally:
                api_server.settings.composio_policy_file = old_file

            text = policy.read_text(encoding="utf-8")

        self.assertEqual(response.status_code, 200)
        self.assertIn("GITHUB_LIST_STARGAZERS", text)
        self.assertIn("SLACK_LIST_CHANNELS", text)
        self.assertIn("- slack", text)
        self.assertEqual(response.json()["bulk_update"]["updated"], 2)

    def test_composio_tools_endpoint_dispatches_catalog_action(self):
        tool_result = {
            "result": {"action": "tools", "toolkit": "github", "data": {"items": []}},
            "meta": {"tool": "composio"},
        }
        with patch("api_server.execute_tool", return_value=tool_result) as run_tool:
            response = self.client.get("/composio/tools?toolkit=github&query=repo&limit=5")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["action"], "tools")
        self.assertEqual(run_tool.call_args.kwargs["action"], "tools")
        self.assertEqual(run_tool.call_args.kwargs["toolkit"], "github")
        self.assertEqual(run_tool.call_args.kwargs["query"], "repo")
        self.assertEqual(run_tool.call_args.kwargs["limit"], 5)

    def test_composio_policy_returns_repo_argument_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            policy = Path(tmp) / "COMPOSIO_GATEWAY.md"
            policy.write_text(
                "\n".join(
                    [
                        "# Composio Gateway",
                        "",
                        "## Runtime",
                        "",
                        "- enabled: true",
                        "",
                        "## Enabled Toolkits",
                        "",
                        "- github",
                        "",
                        "## Enabled Tools",
                        "",
                        "- GITHUB_LIST_STARGAZERS | toolkit: github | risk: read | enabled: true | note: stars",
                        "",
                        "## Argument Defaults",
                        "",
                        "- GITHUB_LIST_STARGAZERS | owner: local.owner | repo: local.repo",
                    ]
                ),
                encoding="utf-8",
            )
            old_file = api_server.settings.composio_policy_file
            try:
                api_server.settings.composio_policy_file = str(policy)
                with patch(
                    "tools.composio._local_repository_hint",
                    return_value={"owner": "akyourowngames", "repo": "friday", "remote_url": "https://github.com/akyourowngames/friday.git"},
                ):
                    response = self.client.get("/composio/policy")
            finally:
                api_server.settings.composio_policy_file = old_file

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["local_repository"]["owner"], "akyourowngames")
        self.assertEqual(payload["argument_defaults"]["GITHUB_LIST_STARGAZERS"]["repo"], "friday")

    def test_composio_policy_tool_rejects_invalid_risk(self):
        response = self.client.post(
            "/composio/policy/tool",
            json={"slug": "GITHUB_BAD", "toolkit": "github", "risk": "unsafe"},
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["detail"]["code"], "INVALID_RISK")


if __name__ == "__main__":
    unittest.main()
