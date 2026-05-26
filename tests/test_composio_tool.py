import os
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import tools
import tools.composio as composio_mod
from tools.registry import get_tool


class FakeResponse:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.text = text

    def json(self):
        return self._payload


class ComposioToolTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.policy_path = Path(self.tmp.name) / "COMPOSIO_GATEWAY.md"
        self.policy_path.write_text(
            "\n".join(
                [
                    "# Composio Gateway",
                    "",
                    "## Runtime",
                    "",
                    "- enabled: true",
                    "- base_url: https://backend.composio.dev/api/v3.1",
                    "- api_key_env: KING_TEST_COMPOSIO_API_KEY",
                    "- user_id_env: KING_TEST_COMPOSIO_USER_ID",
                    "- session_id_env: KING_TEST_COMPOSIO_SESSION_ID",
                    "- default_timeout_ms: 20000",
                    "- max_response_chars: 4000",
                    "- semantic_slug_resolution: true",
                    "- semantic_slug_min_score: 0.35",
                    "- semantic_slug_min_margin: 0.03",
                    "- create_sessions_with_search: true",
                    "- create_sessions_with_manage_connections: true",
                    "- create_sessions_with_workbench: false",
                    "",
                    "## Enabled Toolkits",
                    "",
                    "- github",
                    "",
                    "## Enabled Tools",
                    "",
                    "- GITHUB_GET_A_REPOSITORY | toolkit: github | risk: read | enabled: true | note: get repository details and metadata after GitHub is connected",
                    "- GITHUB_LIST_STARGAZERS | toolkit: github | risk: read | enabled: true | note: test read",
                    "- GITHUB_CREATE_AN_ISSUE | toolkit: github | risk: write | enabled: true | note: test write",
                    "",
                    "## Argument Defaults",
                    "",
                    "- GITHUB_GET_A_REPOSITORY | owner: local.owner | repo: local.repo",
                    "- GITHUB_LIST_STARGAZERS | owner: local.owner | repo: local.repo",
                    "",
                    "## Argument Default Placeholders",
                    "",
                    "- values: owner, repo, repository",
                ]
            ),
            encoding="utf-8",
        )
        self.old_policy_file = composio_mod.settings.composio_policy_file
        self.old_api_key = composio_mod.settings.composio_api_key
        self.old_user_id = composio_mod.settings.composio_user_id
        self.old_session_id = composio_mod.settings.composio_session_id
        self.old_timeout = composio_mod.settings.composio_default_timeout_ms
        self.old_env = {
            "KING_TEST_COMPOSIO_API_KEY": os.environ.get("KING_TEST_COMPOSIO_API_KEY"),
            "KING_TEST_COMPOSIO_USER_ID": os.environ.get("KING_TEST_COMPOSIO_USER_ID"),
            "KING_TEST_COMPOSIO_SESSION_ID": os.environ.get("KING_TEST_COMPOSIO_SESSION_ID"),
        }
        composio_mod.settings.composio_policy_file = str(self.policy_path)
        composio_mod.settings.composio_api_key = ""
        composio_mod.settings.composio_user_id = "king-test-user"
        composio_mod.settings.composio_session_id = ""
        composio_mod.settings.composio_default_timeout_ms = 20000
        for key in self.old_env:
            os.environ.pop(key, None)

    def tearDown(self):
        composio_mod.settings.composio_policy_file = self.old_policy_file
        composio_mod.settings.composio_api_key = self.old_api_key
        composio_mod.settings.composio_user_id = self.old_user_id
        composio_mod.settings.composio_session_id = self.old_session_id
        composio_mod.settings.composio_default_timeout_ms = self.old_timeout
        for key, value in self.old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.tmp.cleanup()

    def test_tool_is_registered(self):
        self.assertIsNotNone(get_tool("composio"))

    def test_github_remote_parser_extracts_owner_and_repo(self):
        https_result = composio_mod._github_owner_repo_from_remote("https://github.com/akyourowngames/friday.git")
        ssh_result = composio_mod._github_owner_repo_from_remote("git@github.com:akyourowngames/friday.git")

        self.assertEqual(https_result["owner"], "akyourowngames")
        self.assertEqual(https_result["repo"], "friday")
        self.assertEqual(ssh_result["owner"], "akyourowngames")
        self.assertEqual(ssh_result["repo"], "friday")

    def test_status_is_local_and_reports_missing_key(self):
        result = composio_mod.composio(response_format="structured")

        self.assertIn("result", result)
        self.assertFalse(result["result"]["api_key_present"])
        self.assertEqual(result["result"]["enabled_toolkits"], ["github"])
        self.assertIn("GITHUB_LIST_STARGAZERS", [item["slug"] for item in result["result"]["enabled_tools"]])

    def test_network_action_requires_api_key(self):
        result = composio_mod.composio(action="create_session", response_format="structured")

        self.assertEqual(result["error"]["code"], "MISSING_API_KEY")

    def test_create_session_uses_markdown_limits(self):
        os.environ["KING_TEST_COMPOSIO_API_KEY"] = "cmp_test"
        calls = []
        original = composio_mod.httpx.request

        def fake_request(method, url, headers=None, params=None, json=None, timeout=None):
            calls.append({"method": method, "url": url, "headers": headers, "params": params, "json": json, "timeout": timeout})
            return FakeResponse(
                201,
                {
                    "session_id": "trs_test",
                    "mcp": {"url": "https://app.composio.dev/tool_router/v3/trs_test/mcp"},
                    "tool_router_tools": ["COMPOSIO_SEARCH_TOOLS"],
                    "config": {"toolkits": {"enabled": ["github"]}},
                },
            )

        try:
            composio_mod.httpx.request = fake_request
            result = composio_mod.composio(action="create_session", response_format="structured")
        finally:
            composio_mod.httpx.request = original

        self.assertEqual(result["result"]["session_id"], "trs_test")
        self.assertEqual(calls[0]["method"], "POST")
        self.assertTrue(calls[0]["url"].endswith("/tool_router/session"))
        self.assertEqual(calls[0]["json"]["toolkits"], {"enable": ["github"]})
        self.assertEqual(calls[0]["json"]["tools"]["github"]["enable"], ["GITHUB_CREATE_AN_ISSUE", "GITHUB_GET_A_REPOSITORY", "GITHUB_LIST_STARGAZERS"])
        self.assertEqual(calls[0]["json"]["workbench"], {"enable": False})

    def test_tools_action_uses_tools_catalog_endpoint(self):
        os.environ["KING_TEST_COMPOSIO_API_KEY"] = "cmp_test"
        calls = []
        original = composio_mod.httpx.request

        def fake_request(method, url, headers=None, params=None, json=None, timeout=None):
            calls.append({"method": method, "url": url, "params": params, "json": json})
            return FakeResponse(200, {"items": [{"slug": "GITHUB_GET_A_REPOSITORY"}]})

        try:
            composio_mod.httpx.request = fake_request
            result = composio_mod.composio(action="tools", toolkit="github", query="repo details", response_format="structured")
        finally:
            composio_mod.httpx.request = original

        self.assertEqual(result["result"]["action"], "tools")
        self.assertEqual(result["result"]["items"][0]["slug"], "GITHUB_GET_A_REPOSITORY")
        self.assertTrue(calls[0]["url"].endswith("/tools"))
        self.assertEqual(calls[0]["params"]["toolkit_slug"], "github")
        self.assertEqual(calls[0]["params"]["query"], "repo details")

    def test_session_tools_action_uses_session_endpoint(self):
        os.environ["KING_TEST_COMPOSIO_API_KEY"] = "cmp_test"
        calls = []
        original = composio_mod.httpx.request

        def fake_request(method, url, headers=None, params=None, json=None, timeout=None):
            calls.append({"method": method, "url": url, "params": params, "json": json})
            return FakeResponse(200, {"tools": [{"slug": "GITHUB_GET_A_REPOSITORY"}]})

        try:
            composio_mod.httpx.request = fake_request
            result = composio_mod.composio(action="session_tools", session_id="trs_test", response_format="structured")
        finally:
            composio_mod.httpx.request = original

        self.assertEqual(result["result"]["action"], "session_tools")
        self.assertTrue(calls[0]["url"].endswith("/tool_router/session/trs_test/tools"))

    def test_execute_rejects_tool_not_in_markdown(self):
        os.environ["KING_TEST_COMPOSIO_API_KEY"] = "cmp_test"

        result = composio_mod.composio(action="execute", tool_slug="GMAIL_SEND_EMAIL", response_format="structured")

        self.assertEqual(result["error"]["code"], "TOOL_NOT_ALLOWED")

    def test_write_risk_requires_confirmation(self):
        os.environ["KING_TEST_COMPOSIO_API_KEY"] = "cmp_test"

        result = composio_mod.composio(action="execute", tool_slug="GITHUB_CREATE_AN_ISSUE", response_format="structured")

        self.assertEqual(result["error"]["code"], "CONFIRMATION_REQUIRED")
        self.assertEqual(result["error"]["risk"], "write")

    def test_execute_allowed_tool_uses_session_endpoint(self):
        os.environ["KING_TEST_COMPOSIO_API_KEY"] = "cmp_test"
        calls = []
        original = composio_mod.httpx.request

        def fake_request(method, url, headers=None, params=None, json=None, timeout=None):
            calls.append({"method": method, "url": url, "headers": headers, "params": params, "json": json, "timeout": timeout})
            return FakeResponse(200, {"data": {"stargazers": []}, "error": None, "log_id": "log_test"})

        try:
            composio_mod.httpx.request = fake_request
            result = composio_mod.composio(
                action="execute",
                tool_slug="GITHUB_LIST_STARGAZERS",
                arguments={"owner": "ComposioHQ", "repo": "composio", "page": 1, "per_page": 5},
                session_id="trs_test",
                response_format="structured",
            )
        finally:
            composio_mod.httpx.request = original

        self.assertEqual(result["result"]["tool_slug"], "GITHUB_LIST_STARGAZERS")
        self.assertEqual(result["result"]["session_id"], "trs_test")
        self.assertEqual(calls[0]["method"], "POST")
        self.assertTrue(calls[0]["url"].endswith("/tool_router/session/trs_test/execute"))
        self.assertEqual(calls[0]["json"]["tool_slug"], "GITHUB_LIST_STARGAZERS")
        self.assertEqual(calls[0]["json"]["arguments"]["owner"], "ComposioHQ")

    def test_execute_semantically_resolves_enabled_tool_slug(self):
        os.environ["KING_TEST_COMPOSIO_API_KEY"] = "cmp_test"
        calls = []
        original_request = composio_mod.httpx.request
        original_embed = composio_mod._embed_texts_local

        def fake_request(method, url, headers=None, params=None, json=None, timeout=None):
            calls.append({"method": method, "url": url, "headers": headers, "params": params, "json": json, "timeout": timeout})
            return FakeResponse(200, {"data": {"full_name": "akyourowngames/friday"}, "error": None, "log_id": "log_test"})

        def fake_embed(texts):
            vectors = []
            for index, _text in enumerate(texts):
                if index == 0:
                    vectors.append([1.0, 0.0, 0.0])
                elif "GITHUB_GET_A_REPOSITORY" in _text:
                    vectors.append([0.90, 0.0, 0.0])
                elif "GITHUB_LIST_STARGAZERS" in _text:
                    vectors.append([0.20, 0.0, 0.0])
                else:
                    vectors.append([0.10, 0.0, 0.0])
            return np.array(vectors, dtype=np.float32)

        try:
            composio_mod.httpx.request = fake_request
            composio_mod._embed_texts_local = fake_embed
            result = composio_mod.composio(
                action="execute",
                tool_slug="get_repo_details",
                arguments={"owner": "akyourowngames", "repo": "friday"},
                session_id="trs_test",
                response_format="structured",
            )
        finally:
            composio_mod.httpx.request = original_request
            composio_mod._embed_texts_local = original_embed

        self.assertEqual(result["result"]["tool_slug"], "GITHUB_GET_A_REPOSITORY")
        self.assertTrue(result["result"]["tool_resolution"]["resolved"])
        self.assertEqual(calls[0]["json"]["tool_slug"], "GITHUB_GET_A_REPOSITORY")

    def test_schema_returns_compact_input_schema(self):
        os.environ["KING_TEST_COMPOSIO_API_KEY"] = "cmp_test"
        original_request = composio_mod.httpx.request
        original_hint = composio_mod._local_repository_hint

        def fake_request(method, url, headers=None, params=None, json=None, timeout=None):
            return FakeResponse(
                200,
                {
                    "input_parameters": {
                        "properties": {
                            "owner": {"type": "string", "description": "Repository owner"},
                            "repo": {"type": "string", "description": "Repository name"},
                        },
                        "required": ["owner", "repo"],
                    },
                    "description": "schema payload",
                },
            )

        try:
            composio_mod.httpx.request = fake_request
            composio_mod._local_repository_hint = lambda: {"owner": "akyourowngames", "repo": "friday", "remote_url": "https://github.com/akyourowngames/friday.git"}
            result = composio_mod.composio(
                action="schema",
                tool_slug="GITHUB_LIST_STARGAZERS",
                response_format="structured",
            )
        finally:
            composio_mod.httpx.request = original_request
            composio_mod._local_repository_hint = original_hint

        self.assertEqual(result["result"]["required_arguments"], ["owner", "repo"])
        self.assertIn("owner", result["result"]["input_schema"]["properties"])
        self.assertEqual(result["result"]["argument_defaults"]["repo"], "friday")

    def test_execute_applies_markdown_argument_defaults(self):
        os.environ["KING_TEST_COMPOSIO_API_KEY"] = "cmp_test"
        calls = []
        original_request = composio_mod.httpx.request
        original_hint = composio_mod._local_repository_hint

        def fake_request(method, url, headers=None, params=None, json=None, timeout=None):
            calls.append({"method": method, "url": url, "headers": headers, "params": params, "json": json, "timeout": timeout})
            return FakeResponse(200, {"data": {"stargazers": []}, "error": None, "log_id": "log_test"})

        try:
            composio_mod.httpx.request = fake_request
            composio_mod._local_repository_hint = lambda: {"owner": "akyourowngames", "repo": "friday", "remote_url": "https://github.com/akyourowngames/friday.git"}
            result = composio_mod.composio(
                action="execute",
                tool_slug="GITHUB_LIST_STARGAZERS",
                arguments={"per_page": 5},
                session_id="trs_test",
                response_format="structured",
            )
        finally:
            composio_mod.httpx.request = original_request
            composio_mod._local_repository_hint = original_hint

        self.assertEqual(result["result"]["arguments"]["owner"], "akyourowngames")
        self.assertEqual(result["result"]["arguments"]["repo"], "friday")
        self.assertEqual(result["result"]["argument_defaults_applied"], {"owner": "akyourowngames", "repo": "friday"})
        self.assertEqual(calls[0]["json"]["arguments"]["owner"], "akyourowngames")

    def test_execute_replaces_markdown_placeholder_arguments(self):
        os.environ["KING_TEST_COMPOSIO_API_KEY"] = "cmp_test"
        calls = []
        original_request = composio_mod.httpx.request
        original_hint = composio_mod._local_repository_hint

        def fake_request(method, url, headers=None, params=None, json=None, timeout=None):
            calls.append({"method": method, "url": url, "headers": headers, "params": params, "json": json, "timeout": timeout})
            return FakeResponse(200, {"data": {"full_name": "akyourowngames/friday"}, "error": None, "log_id": "log_test"})

        try:
            composio_mod.httpx.request = fake_request
            composio_mod._local_repository_hint = lambda: {"owner": "akyourowngames", "repo": "friday", "remote_url": "https://github.com/akyourowngames/friday.git"}
            result = composio_mod.composio(
                action="execute",
                tool_slug="GITHUB_GET_A_REPOSITORY",
                arguments={"owner": "owner", "repo": "repo"},
                session_id="trs_test",
                response_format="structured",
            )
        finally:
            composio_mod.httpx.request = original_request
            composio_mod._local_repository_hint = original_hint

        self.assertEqual(result["result"]["arguments"], {"owner": "akyourowngames", "repo": "friday"})
        self.assertEqual(result["result"]["argument_defaults_applied"], {"owner": "akyourowngames", "repo": "friday"})
        self.assertEqual(calls[0]["json"]["arguments"], {"owner": "akyourowngames", "repo": "friday"})

    def test_link_autocreates_limited_session(self):
        os.environ["KING_TEST_COMPOSIO_API_KEY"] = "cmp_test"
        calls = []
        original = composio_mod.httpx.request

        def fake_request(method, url, headers=None, params=None, json=None, timeout=None):
            calls.append({"method": method, "url": url, "json": json})
            if url.endswith("/tool_router/session"):
                return FakeResponse(201, {"session_id": "trs_test", "mcp": {"url": "mcp-url"}})
            return FakeResponse(201, {"redirect_url": "https://app.composio.dev/link/lt_test", "connected_account_id": "ca_test"})

        try:
            composio_mod.httpx.request = fake_request
            result = composio_mod.composio(action="link", toolkit="github", response_format="structured")
        finally:
            composio_mod.httpx.request = original

        self.assertEqual(result["result"]["redirect_url"], "https://app.composio.dev/link/lt_test")
        self.assertTrue(result["result"]["session_created"])
        self.assertEqual(calls[0]["json"]["toolkits"], {"enable": ["github"]})
        self.assertTrue(calls[1]["url"].endswith("/tool_router/session/trs_test/link"))

    def test_invalid_json_arguments_are_rejected(self):
        os.environ["KING_TEST_COMPOSIO_API_KEY"] = "cmp_test"

        result = composio_mod.composio(
            action="execute",
            tool_slug="GITHUB_LIST_STARGAZERS",
            arguments="{bad json",
            session_id="trs_test",
            response_format="structured",
        )

        self.assertEqual(result["error"]["code"], "INVALID_ARGUMENTS_JSON")


if __name__ == "__main__":
    unittest.main()
