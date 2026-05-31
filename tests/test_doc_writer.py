"""Tests for the document writer tool."""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.doc_writer import (
    _sanitize_filename,
    _unique_path,
    _plan_document,
    _generate_content,
    _get_project_context,
    _load_policy,
    doc_write,
)


def _fake_llm(responses):
    """Fake OpenAI client returning given strings in order, last repeats."""
    state = {"i": 0}

    class _Completions:
        def create(self, **kwargs):
            idx = min(state["i"], len(responses) - 1)
            state["i"] += 1
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content=responses[idx]))]
            )

    return SimpleNamespace(client=SimpleNamespace(chat=SimpleNamespace(completions=_Completions())))


class SanitizeFilenameTests(unittest.TestCase):
    def test_basic_conversion(self):
        self.assertEqual(_sanitize_filename("Database Schema"), "database-schema")

    def test_special_chars_removed(self):
        result = _sanitize_filename("My File! @#$% Name")
        self.assertNotIn("!", result)
        self.assertNotIn("@", result)

    def test_long_text_truncated(self):
        result = _sanitize_filename("a" * 100)
        self.assertLessEqual(len(result), 60)

    def test_empty_becomes_document(self):
        self.assertEqual(_sanitize_filename(""), "document")

    def test_kebab_case(self):
        self.assertEqual(_sanitize_filename("hello world test"), "hello-world-test")


class UniquePathTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def test_no_conflict(self):
        path = _unique_path(self.dir, "schema", ".sql")
        self.assertEqual(path.name, "schema.sql")

    def test_avoids_existing(self):
        (self.dir / "schema.sql").write_text("existing")
        path = _unique_path(self.dir, "schema", ".sql")
        self.assertEqual(path.name, "schema-2.sql")

    def test_multiple_conflicts(self):
        for i in range(1, 5):
            (self.dir / f"schema-{i}.sql").write_text("x")
        (self.dir / "schema.sql").write_text("x")
        path = _unique_path(self.dir, "schema", ".sql")
        self.assertEqual(path.name, "schema-5.sql")


class LoadPolicyTests(unittest.TestCase):
    def test_policy_loads(self):
        policy = _load_policy()
        self.assertIn("Document Types", policy)
        self.assertIn("Delivery Rules", policy)

    def test_policy_has_format_rules(self):
        policy = _load_policy()
        self.assertIn(".sql", policy)
        self.assertIn(".md", policy)


class PlanDocumentTests(unittest.TestCase):
    @patch("tools.doc_writer._llm_call")
    def test_returns_plan_dict(self, mock_llm):
        mock_llm.return_value = json.dumps({
            "doc_type": "database_schema",
            "format": ".sql",
            "filename": "budget-tracker-schema",
            "title": "Budget Tracker Database Schema",
            "sections": ["users", "transactions", "categories"],
            "summary": "SQL DDL for the budget tracker database",
        })
        plan = _plan_document("draft a database schema for the budget tracker")
        self.assertIsNotNone(plan)
        self.assertEqual(plan["doc_type"], "database_schema")
        self.assertEqual(plan["format"], ".sql")
        self.assertEqual(plan["filename"], "budget-tracker-schema")

    @patch("tools.doc_writer._llm_call")
    def test_returns_none_on_llm_failure(self, mock_llm):
        mock_llm.return_value = None
        self.assertIsNone(_plan_document("draft something"))

    @patch("tools.doc_writer._llm_call")
    def test_returns_none_on_invalid_json(self, mock_llm):
        mock_llm.return_value = "not json"
        self.assertIsNone(_plan_document("draft something"))

    @patch("tools.doc_writer._llm_call")
    def test_returns_none_on_missing_fields(self, mock_llm):
        mock_llm.return_value = json.dumps({"doc_type": "schema"})
        self.assertIsNone(_plan_document("draft something"))


class GenerateContentTests(unittest.TestCase):
    @patch("tools.doc_writer._llm_call")
    def test_generates_content(self, mock_llm):
        mock_llm.return_value = "CREATE TABLE users (id SERIAL PRIMARY KEY);"
        plan = {
            "doc_type": "database_schema",
            "format": ".sql",
            "sections": ["users"],
        }
        content = _generate_content(plan, "draft a schema")
        self.assertIsNotNone(content)
        self.assertIn("CREATE TABLE", content)

    @patch("tools.doc_writer._llm_call")
    def test_returns_none_on_failure(self, mock_llm):
        mock_llm.return_value = None
        content = _generate_content({}, "draft something")
        self.assertIsNone(content)


class GetProjectContextTests(unittest.TestCase):
    def test_empty_project_returns_empty(self):
        self.assertEqual(_get_project_context(""), "")

    @patch("project_manager.store.ProjectStore")
    def test_returns_project_details(self, mock_store_cls):
        mock_store = MagicMock()
        mock_store.all_projects.return_value = [
            {
                "name": "Budget Tracker",
                "id": "budget-tracker",
                "goal": "Build a budget app",
                "status": "active",
                "health": 100,
                "tasks": [{"title": "Design schema", "status": "open"}],
                "blockers": [],
            }
        ]
        mock_store_cls.return_value = mock_store
        ctx = _get_project_context("budget tracker")
        self.assertIn("Budget Tracker", ctx)
        self.assertIn("Build a budget app", ctx)
        self.assertIn("Design schema", ctx)


class DocWriteToolTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.docs_dir = Path(self.tmp.name) / "docs"

    @patch("tools.doc_writer._docs_dir")
    @patch("tools.doc_writer._deliver")
    @patch("tools.doc_writer._generate_content")
    @patch("tools.doc_writer._plan_document")
    def test_full_flow(self, mock_plan, mock_gen, mock_deliver, mock_docs):
        mock_docs.return_value = self.docs_dir
        mock_plan.return_value = {
            "doc_type": "database_schema",
            "format": ".sql",
            "filename": "test-schema",
            "title": "Test Schema",
            "sections": ["users"],
            "summary": "A test schema",
        }
        mock_gen.return_value = "CREATE TABLE users (id SERIAL PRIMARY KEY);"
        mock_deliver.return_value = "Opened test-schema.sql"

        result = doc_write("draft a schema")
        self.assertIn("Test Schema", result)
        self.assertIn("Opened test-schema.sql", result)
        self.assertTrue((self.docs_dir / "test-schema.sql").exists())

    @patch("tools.doc_writer._docs_dir")
    def test_empty_request_returns_error(self, mock_docs):
        mock_docs.return_value = self.docs_dir
        result = doc_write("")
        self.assertIn("Error", result)

    @patch("tools.doc_writer._plan_document")
    def test_plan_failure_returns_error(self, mock_plan):
        mock_plan.return_value = None
        result = doc_write("draft something impossible")
        self.assertIn("Error", result)

    @patch("tools.doc_writer._generate_content")
    @patch("tools.doc_writer._plan_document")
    def test_generate_failure_returns_error(self, mock_plan, mock_gen):
        mock_plan.return_value = {"doc_type": "other", "format": ".md", "filename": "test"}
        mock_gen.return_value = None
        result = doc_write("draft something that fails to generate")
        self.assertIn("Error", result)


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "test.md"

    def test_code_extension_opens_locally(self):
        from tools.doc_writer import _deliver

        sql_path = Path(self.tmp.name) / "test.sql"
        sql_path.write_text("CREATE TABLE test (id INT);")
        result = _deliver(sql_path, "CREATE TABLE test (id INT);", "auto", "database_schema")
        self.assertIn("test.sql", result)

    def test_short_md_shows_in_terminal(self):
        from tools.doc_writer import _deliver

        self.path.write_text("short content")
        result = _deliver(self.path, "short content", "auto", "project_outline")
        self.assertIn("=== test.md", result)
        self.assertIn("short content", result)

    def test_explicit_open_overrides(self):
        from tools.doc_writer import _deliver

        self.path.write_text("content")
        result = _deliver(self.path, "content", "open", "other")
        self.assertIn("test.md", result)

    def test_explicit_terminal_overrides(self):
        from tools.doc_writer import _deliver

        self.path.write_text("content")
        result = _deliver(self.path, "content", "terminal", "other")
        self.assertIn("=== test.md", result)


class StripMarkdownFencesTests(unittest.TestCase):
    def test_strips_bare_fences(self):
        from tools.doc_writer import _strip_markdown_fences
        ticks = chr(96) * 3
        text = ticks + chr(10) + "# Hello" + chr(10) + ticks
        self.assertEqual(_strip_markdown_fences(text), "# Hello")

    def test_strips_lang_tagged_fences(self):
        from tools.doc_writer import _strip_markdown_fences
        ticks = chr(96) * 3
        text = ticks + "sql" + chr(10) + "CREATE TABLE t;" + chr(10) + ticks
        self.assertEqual(_strip_markdown_fences(text), "CREATE TABLE t;")

    def test_strips_markdown_fence(self):
        from tools.doc_writer import _strip_markdown_fences
        ticks = chr(96) * 3
        text = ticks + "markdown" + chr(10) + "# Title" + chr(10) + "Body" + chr(10) + ticks
        self.assertEqual(_strip_markdown_fences(text), "# Title\nBody")

    def test_no_fences_returns_unchanged(self):
        from tools.doc_writer import _strip_markdown_fences
        text = "# Just a heading" + chr(10) + "No fences here"
        self.assertEqual(_strip_markdown_fences(text), text)

    def test_empty_returns_empty(self):
        from tools.doc_writer import _strip_markdown_fences
        self.assertEqual(_strip_markdown_fences(""), "")
        self.assertEqual(_strip_markdown_fences("  "), "")

    def test_single_line_no_strip(self):
        from tools.doc_writer import _strip_markdown_fences
        self.assertEqual(_strip_markdown_fences("just one line"), "just one line")


if __name__ == "__main__":
    unittest.main()
