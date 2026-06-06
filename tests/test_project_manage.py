from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from assistant_cli.langchain_memory import JsonlChatMessageHistory
from assistant_cli.project_context import project_prompt_context
from assistant_cli.project_store import ProjectStore, project_db_path
from assistant_cli.tools import build_default_registry
from test_tools import make_settings


class ProjectStoreTests(unittest.TestCase):
    def test_project_reference_can_resolve_a_unique_natural_alias(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = ProjectStore(Path(tmp) / "projects.sqlite3")
            store.create_project("Friday")

            project = store.resolve_project("my friday project")

        self.assertEqual(project["name"], "Friday")

    def test_project_task_crud_persists_across_store_instances(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = make_settings(root)
            db_path = project_db_path(settings, root)

            store = ProjectStore(db_path)
            project = store.create_project("Friday", description="Local assistant")["project"]
            task = store.create_task(
                "Friday",
                "fix voice latency",
                priority="high",
                due="tomorrow",
                tags=["voice"],
            )["task"]
            subtask = store.add_subtask(task["id"], "test Sarvam playback", project="Friday")["task"]
            note = store.add_note(task["id"], "TTS must start quickly")["note"]
            completed = store.set_task_status(task["id"], "done")

            reloaded = ProjectStore(db_path)
            summary = reloaded.summary(project="Friday")
            events = reloaded.activity(project="Friday", limit=10)
            db_exists = db_path.exists()

        self.assertEqual(project["name"], "Friday")
        self.assertEqual(completed["status"], "done")
        self.assertEqual(subtask["parent_task_id"], task["id"])
        self.assertEqual(note["task_id"], task["id"])
        self.assertTrue(db_exists)
        self.assertEqual(summary["projects"][0]["name"], "Friday")
        self.assertIn("test Sarvam playback", {item["title"] for item in summary["open_tasks"]})
        self.assertGreaterEqual(len(events), 5)

    def test_project_prompt_context_is_concise(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = make_settings(root)
            store = ProjectStore(project_db_path(settings, root))
            store.create_task("Friday", "test project context", priority="urgent")

            context = project_prompt_context(settings, root)

        self.assertIn("Local project management context", context)
        self.assertIn("Friday", context)
        self.assertIn("test project context", context)


class ProjectManageToolTests(unittest.TestCase):
    def test_registered_tool_handles_projects_tasks_notes_and_errors(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = make_settings(root)
            registry = build_default_registry(settings, root)

            created_project = registry.execute("project_manage", {"action": "project_create", "name": "Friday"})
            archive_lab = registry.execute("project_manage", {"action": "project_create", "name": "ArchiveLab"})
            updated_project = registry.execute(
                "project_manage",
                {"action": "project_update", "project": "Friday", "description": "Friday task board"},
            )
            archived_project = registry.execute(
                "project_manage",
                {"action": "project_archive", "project": "ArchiveLab"},
            )
            projects = registry.execute(
                "project_manage",
                {"action": "project_list", "include_archived": True},
            )
            project_alias = registry.execute("project_manage", {"action": "show_project"})
            pending_alias = registry.execute(
                "project_manage",
                {"action": "list_pending_tasks", "project": "Friday"},
            )
            dict_shaped_task = registry.execute(
                "project_manage",
                {
                    "action": "task_create",
                    "project": {"id": created_project.data["project"]["id"], "name": "Friday"},
                    "title": {"title": "dict shaped task"},
                },
            )
            created_task = registry.execute(
                "project_manage",
                {
                    "action": "task_create",
                    "project": "Friday",
                    "title": "test project management",
                    "priority": "high",
                    "due": "tomorrow",
                    "tags": ["cli"],
                },
            )
            task_id = created_task.data["task"]["id"]
            second_task = registry.execute(
                "project_manage",
                {"action": "task_create", "project": "Friday", "title": "bulk completion target"},
            )
            created_many = registry.execute(
                "project_manage",
                {
                    "action": "task_create_many",
                    "project": "Friday",
                    "tasks": [
                        {"title": "dated high priority", "priority": "high", "due": "tomorrow 5pm"},
                        {"title": "weekly audit", "recurrence": "every Friday"},
                    ],
                },
            )
            bulk_updated = registry.execute(
                "project_manage",
                {
                    "action": "task_bulk_update",
                    "project": "Friday",
                    "task_ids": ["dated high priority", "weekly audit"],
                    "priority": "urgent",
                    "due": "next Friday at 6pm",
                },
            )
            updated_task = registry.execute(
                "project_manage",
                {"action": "task_update", "task_id": task_id, "status": "blocked", "priority": "urgent"},
            )
            pending = registry.execute("project_manage", {"action": "task_pending", "task_id": task_id})
            subtask = registry.execute(
                "project_manage",
                {"action": "subtask_add", "project": "Friday", "task_id": task_id, "title": "write tests"},
            )
            note = registry.execute(
                "project_manage",
                {"action": "note_add", "task_id": task_id, "note": "verify JSONL tool records"},
            )
            activity = registry.execute("project_manage", {"action": "activity", "project": "Friday"})
            completed = registry.execute("project_manage", {"action": "task_complete", "task_id": task_id})
            completed_again = registry.execute("project_manage", {"action": "task_complete", "task_id": task_id})
            bulk_pending = registry.execute("project_manage", {"action": "task_pending_all", "project": "Friday"})
            bulk_done = registry.execute("project_manage", {"action": "task_complete_all", "project": "Friday"})
            deleted = registry.execute(
                "project_manage",
                {"action": "task_delete", "task_id": subtask.data["task"]["id"]},
            )
            open_only = registry.execute("project_manage", {"action": "task_list", "project": "Friday"})
            listed = registry.execute("project_manage", {"action": "task_list", "project": "Friday", "include_done": True})
            summary = registry.execute("project_manage", {"action": "summary"})
            missing = registry.execute("project_manage", {"action": "task_complete", "task_id": "task_missing"})
            registry.close()

        self.assertTrue(created_project.ok, created_project.text)
        self.assertTrue(archive_lab.ok, archive_lab.text)
        self.assertTrue(updated_project.ok, updated_project.text)
        self.assertEqual(updated_project.data["project"]["description"], "Friday task board")
        self.assertTrue(archived_project.ok, archived_project.text)
        self.assertEqual(archived_project.data["project"]["status"], "archived")
        self.assertTrue(projects.ok, projects.text)
        self.assertIn("ArchiveLab", projects.text)
        self.assertTrue(project_alias.ok, project_alias.text)
        self.assertIn("Friday", project_alias.text)
        self.assertTrue(pending_alias.ok, pending_alias.text)
        self.assertEqual(pending_alias.data["action"], "task_list")
        self.assertTrue(dict_shaped_task.ok, dict_shaped_task.text)
        self.assertEqual(dict_shaped_task.data["task"]["title"], "dict shaped task")
        self.assertEqual(dict_shaped_task.data["task"]["project_name"], "Friday")
        self.assertTrue(created_task.ok, created_task.text)
        self.assertEqual(created_task.data["task"]["priority"], "high")
        self.assertTrue(second_task.ok, second_task.text)
        self.assertTrue(created_many.ok, created_many.text)
        self.assertEqual(created_many.data["created_count"], 2)
        self.assertTrue(bulk_updated.ok, bulk_updated.text)
        self.assertEqual(bulk_updated.data["updated_count"], 2)
        self.assertTrue(all(task["priority"] == "urgent" for task in bulk_updated.data["updated_tasks"]))
        self.assertTrue(all(task["due_at"] for task in bulk_updated.data["updated_tasks"]))
        self.assertEqual(
            next(task for task in created_many.data["created_tasks"] if task["title"] == "weekly audit")["recurrence"],
            "every Friday",
        )
        self.assertTrue(updated_task.ok, updated_task.text)
        self.assertEqual(updated_task.data["task"]["status"], "blocked")
        self.assertEqual(updated_task.data["task"]["priority"], "urgent")
        self.assertTrue(pending.ok, pending.text)
        self.assertEqual(pending.data["task"]["status"], "pending")
        self.assertTrue(subtask.ok, subtask.text)
        self.assertEqual(subtask.data["task"]["parent_task_id"], task_id)
        self.assertTrue(note.ok, note.text)
        self.assertTrue(activity.ok, activity.text)
        self.assertIn("task_create", activity.text)
        self.assertTrue(completed.ok, completed.text)
        self.assertEqual(completed.data["task"]["status"], "done")
        self.assertTrue(completed_again.ok, completed_again.text)
        self.assertEqual(completed_again.data["task"]["completed_at"], completed.data["task"]["completed_at"])
        self.assertTrue(bulk_pending.ok, bulk_pending.text)
        self.assertGreaterEqual(bulk_pending.data["updated_count"], 1)
        self.assertTrue(bulk_done.ok, bulk_done.text)
        self.assertGreaterEqual(bulk_done.data["updated_count"], 3)
        self.assertEqual(bulk_done.data["projects"][0]["open_tasks"], 0)
        self.assertTrue(deleted.ok, deleted.text)
        self.assertTrue(deleted.data["deleted"])
        self.assertTrue(open_only.ok, open_only.text)
        self.assertNotIn("test project management", open_only.text)
        self.assertTrue(listed.ok, listed.text)
        self.assertIn("test project management", listed.text)
        self.assertTrue(summary.ok, summary.text)
        self.assertIn("Project summary", summary.text)
        self.assertFalse(missing.ok)
        self.assertIn("Task not found", missing.text)

    def test_project_tool_result_can_be_recorded_in_jsonl_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = make_settings(root)
            registry = build_default_registry(settings, root)
            history = JsonlChatMessageHistory(settings.session_dir)

            result = registry.execute(
                "project_manage",
                {"action": "task_create", "project": "Friday", "title": "record tool result"},
            )
            history.add_tool_message("project_manage", json.dumps(result.as_dict(), ensure_ascii=False))
            rows = history.records()
            registry.close()

        self.assertTrue(result.ok, result.text)
        self.assertEqual(rows[-1]["role"], "tool")
        self.assertEqual(rows[-1]["tool"], "project_manage")
        self.assertIn("record tool result", rows[-1]["content"])


if __name__ == "__main__":
    unittest.main()
