import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from project_manager import config as pm_config
from project_manager import model, triggers
from project_manager.store import ProjectStore
from project_manager.manager import ProjectManager


def _future(days: int) -> str:
    return (datetime.now() + timedelta(days=days)).date().isoformat()


class ConfigTests(unittest.TestCase):
    def test_sections_have_typed_defaults(self):
        triggers_cfg = pm_config.section("triggers")
        self.assertIsInstance(triggers_cfg["inactivity_days"], int)
        self.assertIsInstance(triggers_cfg["velocity_collapse_ratio"], float)
        self.assertIsInstance(triggers_cfg["inactivity_enabled"], bool)

    def test_unknown_key_is_ignored(self):
        scoring = pm_config.section("scoring")
        self.assertIn("health_blocker_penalty", scoring)
        self.assertNotIn("not_a_real_key", scoring)


class ModelTests(unittest.TestCase):
    def test_slugify_is_safe_and_unique(self):
        self.assertEqual(model.slugify("Launch Landing Page!", set()), "launch-landing-page")
        self.assertEqual(model.slugify("Test", {"test"}), "test-2")
        self.assertEqual(model.slugify("***", set()), "project")

    def test_task_lifecycle_and_counts(self):
        project = model.new_project("p", "P", "goal")
        model.add_task(project, "Design")
        model.add_task(project, "Build")
        model.set_task_status(project, "Design", "done")
        counts = model.task_counts(project)
        self.assertEqual(counts["done"], 1)
        self.assertEqual(counts["open"], 1)
        self.assertEqual(counts["total"], 2)

    def test_blocker_add_resolve_and_dedup(self):
        project = model.new_project("p", "P", "goal")
        model.add_blocker(project, "waiting on api key")
        model.add_blocker(project, "waiting on api key")  # dedup
        self.assertEqual(len(model.open_blockers(project)), 1)
        model.resolve_blocker(project, "api key")
        self.assertEqual(len(model.open_blockers(project)), 0)

    def test_health_drops_with_blockers_and_overdue(self):
        project = model.new_project("p", "P", "goal", deadline=_future(-5))
        model.add_task(project, "Build")
        healthy = model.compute_health(project)
        model.add_blocker(project, "stuck")
        worse = model.compute_health(project)
        self.assertLess(worse, healthy)

    def test_eta_uses_actual_velocity(self):
        project = model.new_project("p", "P", "goal")
        now = datetime.now()
        # two closes in last 14 days, two still open
        for i in range(4):
            model.add_task(project, f"task {i}")
        for i in range(2):
            project["tasks"][i]["status"] = "done"
            project["tasks"][i]["closed_at"] = (now - timedelta(days=3)).isoformat(timespec="seconds")
        eta = model.estimated_eta(project, now=now)
        self.assertIsNotNone(eta)
        self.assertGreater(eta, now)


class TriggerTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 5, 29, 9, 0, 0)
        self.cfg = pm_config.section("triggers")

    def test_inactivity_fires_after_threshold(self):
        project = model.new_project("p", "P", "goal")
        model.add_task(project, "Build")
        project["created_at"] = (self.now - timedelta(days=10)).isoformat(timespec="seconds")
        alert = triggers.inactivity(project, self.cfg, self.now)
        self.assertIsNotNone(alert)
        self.assertEqual(alert["kind"], "inactivity")

    def test_inactivity_silent_when_no_open_work(self):
        project = model.new_project("p", "P", "goal")
        model.add_task(project, "Build", status="done")
        project["created_at"] = (self.now - timedelta(days=10)).isoformat(timespec="seconds")
        self.assertIsNone(triggers.inactivity(project, self.cfg, self.now))

    def test_blocker_age_fires(self):
        project = model.new_project("p", "P", "goal")
        model.add_blocker(project, "waiting")
        project["blockers"][0]["logged_at"] = (self.now - timedelta(days=6)).isoformat(timespec="seconds")
        alerts = triggers.blocker_age(project, self.cfg, self.now)
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]["kind"], "blocker_age")

    def test_scope_expansion_fires(self):
        project = model.new_project("p", "P", "goal")
        for i in range(4):
            model.add_task(project, f"t{i}")
        project["creation_task_count"] = 2
        alert = triggers.scope_expansion(project, self.cfg, self.now)
        self.assertIsNotNone(alert)
        self.assertEqual(alert["kind"], "scope_expansion")

    def test_sentiment_streak_fires(self):
        project = model.new_project("p", "P", "goal")
        for _ in range(3):
            model.add_update(project, "ugh", "stressed")
        alert = triggers.sentiment_deterioration(project, self.cfg, self.now)
        self.assertIsNotNone(alert)
        self.assertEqual(alert["facts"]["streak"], 3)

    def test_ghost_detection_fires(self):
        project = model.new_project("p", "P", "goal")
        project["created_at"] = (self.now - timedelta(days=9)).isoformat(timespec="seconds")
        alert = triggers.ghost_detection(project, self.cfg, self.now)
        self.assertIsNotNone(alert)
        self.assertEqual(alert["kind"], "ghost")

    def test_disabled_trigger_stays_silent(self):
        project = model.new_project("p", "P", "goal")
        model.add_task(project, "Build")
        project["created_at"] = (self.now - timedelta(days=30)).isoformat(timespec="seconds")
        disabled = dict(self.cfg)
        disabled["inactivity_enabled"] = False
        self.assertIsNone(triggers.inactivity(project, disabled, self.now))

    def test_cross_project_conflict(self):
        a = model.new_project("a", "A", "goal", deadline="2026-06-10")
        b = model.new_project("b", "B", "goal", deadline="2026-06-11")
        model.add_task(a, "x")
        model.add_task(b, "y")
        alerts = triggers.cross_project_conflict([a, b], now=self.now)
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]["kind"], "cross_project_conflict")


def _intent(action, **kw):
    base = {
        "action": action,
        "project": None,
        "project_name": None,
        "goal": None,
        "deadline": None,
        "new_tasks": [],
        "completed_tasks": [],
        "dropped_tasks": [],
        "blocked_tasks": [],
        "blockers": [],
        "resolved_blockers": [],
        "decisions": [],
        "inferred_tasks": [],
        "sentiment": "neutral",
        "query": None,
    }
    base.update(kw)
    return base


class ManagerIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.store = ProjectStore(store_path=root / "projects.json", log_path=root / "log.jsonl")
        self.mgr = ProjectManager(store=self.store)

    def test_create_seeds_tasks_and_persists(self):
        report = self.mgr.apply_intent(
            "track this",
            _intent("create_project", project_name="Landing Page", goal="ship it",
                    new_tasks=["A", "B"], inferred_tasks=["C"]),
        )
        self.assertEqual(report["action"], "create_project")
        slug = report["project"]
        stored = self.store.get(slug)
        self.assertIsNotNone(stored)
        self.assertEqual(len(stored["tasks"]), 3)
        self.assertEqual(stored["creation_task_count"], 3)

    def test_update_completes_task_and_logs_blocker(self):
        created = self.mgr.apply_intent(
            "track this",
            _intent("create_project", project_name="API", goal="build api", new_tasks=["Auth", "Frontend"]),
        )
        slug = created["project"]
        report = self.mgr.apply_intent(
            "update",
            _intent("log_update", project=slug, completed_tasks=["Auth"],
                    blocked_tasks=["Frontend"], blockers=["waiting on key"], sentiment="stressed"),
        )
        self.assertIn("Auth", report["completed"])
        stored = self.store.get(slug)
        counts = model.task_counts(stored)
        self.assertEqual(counts["done"], 1)
        self.assertEqual(counts["blocked"], 1)
        self.assertEqual(len(model.open_blockers(stored)), 1)

    def test_single_active_project_resolves_without_slug(self):
        self.mgr.apply_intent("track this", _intent("create_project", project_name="Solo", goal="g", new_tasks=["X"]))
        report = self.mgr.apply_intent("done with X", _intent("log_update", completed_tasks=["X"]))
        self.assertEqual(report.get("completed"), ["X"])

    def test_decision_log_query(self):
        self.mgr.apply_intent(
            "track this",
            _intent("create_project", project_name="App", goal="g", new_tasks=["X"]),
        )
        self.mgr.apply_intent(
            "we cut mobile",
            _intent("log_decision", decisions=["dropped the mobile version"]),
        )
        results = self.mgr._query_decisions({"query": "mobile"})
        self.assertEqual(len(results), 1)
        self.assertIn("mobile", results[0]["decision"])

    def test_audit_refreshes_alerts(self):
        created = self.mgr.apply_intent(
            "track this",
            _intent("create_project", project_name="Old", goal="g", new_tasks=["X"]),
        )
        slug = created["project"]
        data = self.store.load()
        data["projects"][slug]["created_at"] = (datetime.now() - timedelta(days=12)).isoformat(timespec="seconds")
        self.store.save(data)
        result = self.mgr.audit()
        self.assertEqual(result["audited"], 1)
        self.assertGreaterEqual(result["alerts"], 1)

    def test_archive_produces_autopsy_and_moves_project(self):
        created = self.mgr.apply_intent(
            "track this",
            _intent("create_project", project_name="Done", goal="ship", new_tasks=["X"]),
        )
        slug = created["project"]
        self.mgr.apply_intent("finished X", _intent("log_update", project=slug, completed_tasks=["X"]))
        autopsy = self.mgr.archive(slug)
        self.assertEqual(autopsy["project"], slug)
        self.assertIsNone(self.store.get(slug))
        self.assertEqual(len(self.store.all_archived()), 1)

    def test_morning_brief_and_focus(self):
        self.mgr.apply_intent("track this", _intent("create_project", project_name="One", goal="g", new_tasks=["A"]))
        self.mgr.apply_intent("track this", _intent("create_project", project_name="Two", goal="g", new_tasks=["B"]))
        brief = self.mgr.morning_brief()
        self.assertEqual(brief["active_count"], 2)
        ranking = self.mgr.focus_ranking()
        self.assertEqual(len(ranking), 2)

    def test_resurrection_brief_lists_next_moves(self):
        created = self.mgr.apply_intent(
            "track this",
            _intent("create_project", project_name="Cold", goal="g", new_tasks=["A", "B", "C", "D"]),
        )
        slug = created["project"]
        brief = self.mgr.resurrection_brief(slug)
        self.assertEqual(len(brief["next_moves"]), 3)


if __name__ == "__main__":
    unittest.main()
