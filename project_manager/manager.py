"""ProjectManager: the living process that runs projects.

Responsibilities:
- Apply a parsed intent to the durable store (create projects, log updates,
  open/close tasks, track blockers and decisions).
- Recompute health, momentum, and status after every change and on the nightly
  audit, recording a health-history point so drift triggers have a baseline.
- Run the trigger engine and persist the alerts each project earned.
- Package the morning war-room brief, project deep-dives, focus ranking,
  autopsy (on archive), and resurrection briefs.

This module never touches agent core or routing. It reads thresholds and weights
from `tools/PROJECT_MANAGER_CONFIG.md` via `project_manager.config`.
"""

from __future__ import annotations

from datetime import datetime

from . import config as pm_config
from . import model, triggers
from .intake import parse_message
from .store import ProjectStore


class ProjectManager:
    def __init__(self, store: ProjectStore | None = None, llm_client=None):
        self.store = store or ProjectStore()
        self._llm = llm_client

    # --- intake -------------------------------------------------------------

    def ingest(self, message: str, now: datetime | None = None) -> dict:
        """Parse a natural-language message and apply it. Returns a report."""
        now = now or datetime.now()
        projects = self.store.all_projects()
        intent = parse_message(message, projects, llm_client=self._llm)
        return self.apply_intent(message, intent, now=now)

    def apply_intent(self, message: str, intent: dict, now: datetime | None = None) -> dict:
        now = now or datetime.now()
        action = intent.get("action") or "log_update"

        if action == "create_project":
            return self._create_from_intent(message, intent, now)
        if action == "status":
            return {"action": "status", "brief": self.morning_brief(now=now)}
        if action == "focus":
            return {"action": "focus", "ranking": self.focus_ranking(now=now)}
        if action == "query_decisions":
            return {"action": "query_decisions", "results": self._query_decisions(intent)}

        project = self._resolve_project(intent, message)
        if project is None:
            # Nothing to attach to; record nothing destructive, report cleanly.
            return {"action": "none", "reason": "no_matching_project", "intent": intent}

        if action == "status_project":
            return {"action": "status_project", "detail": self.project_detail(project["id"], now=now)}
        if action == "archive_project":
            return {"action": "archive_project", "autopsy": self.archive(project["id"], now=now)}

        report = self._apply_changes(project, message, intent, now)
        self._recompute_and_save(project, now, log_event="intake")
        report["project"] = project["id"]
        report["health"] = project["health_score"]
        report["status"] = project["status"]
        report["alerts"] = project.get("alerts", [])
        return report

    def _create_from_intent(self, message: str, intent: dict, now: datetime) -> dict:
        name = intent.get("project_name") or intent.get("goal") or message
        existing = {p["id"] for p in self.store.all_projects()}
        slug = model.slugify(name, existing)
        project = model.new_project(slug, name, intent.get("goal") or "", intent.get("deadline"))
        seed_tasks = list(intent.get("new_tasks") or []) + list(intent.get("inferred_tasks") or [])
        for title in seed_tasks:
            model.add_task(project, title)
        project["creation_task_count"] = len(project["tasks"])
        if message.strip():
            model.add_update(project, message.strip(), intent.get("sentiment", "neutral"))
        for decision in intent.get("decisions") or []:
            model.add_decision(project, decision, context="at project creation")
        self._recompute_and_save(project, now, log_event="created")
        self.store.append_log({"event": "created", "project": slug, "name": project["name"]})
        return {
            "action": "create_project",
            "project": slug,
            "name": project["name"],
            "goal": project["goal"],
            "deadline": project["deadline"],
            "seeded_tasks": [t["title"] for t in project["tasks"]],
            "inferred_tasks": list(intent.get("inferred_tasks") or []),
            "health": project["health_score"],
            "status": project["status"],
        }

    def _apply_changes(self, project: dict, message: str, intent: dict, now: datetime) -> dict:
        changed = {
            "action": intent.get("action"),
            "completed": [],
            "added": [],
            "dropped": [],
            "blocked": [],
            "blockers": [],
            "resolved_blockers": [],
            "decisions": [],
            "inferred_tasks": [],
        }

        for title in intent.get("completed_tasks") or []:
            task = model.set_task_status(project, title, "done")
            if task is None:
                task = model.add_task(project, title, status="done")
            changed["completed"].append(task.get("title"))

        for title in intent.get("new_tasks") or []:
            if model.find_task(project, title) is None:
                changed["added"].append(model.add_task(project, title).get("title"))

        for title in intent.get("dropped_tasks") or []:
            task = model.set_task_status(project, title, "dropped")
            if task is not None:
                changed["dropped"].append(task.get("title"))

        for title in intent.get("blocked_tasks") or []:
            task = model.set_task_status(project, title, "blocked")
            if task is None:
                task = model.add_task(project, title, status="blocked")
            changed["blocked"].append(task.get("title"))

        for description in intent.get("blockers") or []:
            blocker = model.add_blocker(project, description)
            if blocker:
                changed["blockers"].append(blocker.get("description"))

        for description in intent.get("resolved_blockers") or []:
            resolved = model.resolve_blocker(project, description)
            if resolved is not None:
                changed["resolved_blockers"].append(resolved.get("description"))

        for decision in intent.get("decisions") or []:
            recorded = model.add_decision(project, decision, context=message.strip())
            if recorded:
                changed["decisions"].append(recorded.get("decision"))

        # Shadow-task inference: surface inferred tasks for confirmation; we add
        # them so they are tracked, marked as inferred via a leading note in the
        # report so KING can ask "did I get that right?".
        for title in intent.get("inferred_tasks") or []:
            if model.find_task(project, title) is None:
                model.add_task(project, title)
                changed["inferred_tasks"].append(title)

        if message.strip():
            model.add_update(project, message.strip(), intent.get("sentiment", "neutral"))

        return changed

    def _resolve_project(self, intent: dict, message: str) -> dict | None:
        slug = intent.get("project")
        if slug:
            project = self.store.get(slug)
            if project is not None:
                return project
        # Single active project is an unambiguous target.
        active = [p for p in self.store.all_projects() if str(p.get("status")) != "complete"]
        if len(active) == 1:
            return active[0]
        return None

    # --- recompute + persist ------------------------------------------------

    def _recompute_and_save(self, project: dict, now: datetime, log_event: str) -> None:
        previous_health = project.get("health_score", 100)
        project["health_score"] = model.compute_health(project, now=now)
        project["momentum"] = round(model.momentum(project, now=now), 3)
        if project["health_score"] != previous_health or not project.get("health_history"):
            history = project.setdefault("health_history", [])
            history.append({"at": now.isoformat(timespec="seconds"), "score": project["health_score"]})
            keep = int(pm_config.value("runtime", "history_keep_points") or 60)
            if keep > 0 and len(history) > keep:
                project["health_history"] = history[-keep:]
        # Evaluate triggers against the prior status so a freshly-detected ghost
        # still earns one alert before we relabel it; later passes see the ghost
        # status and the ghost trigger stays quiet (no repeat nagging).
        project["alerts"] = triggers.evaluate_project(project, now=now)
        keep_alerts = int(pm_config.value("runtime", "alerts_keep") or 40)
        if keep_alerts > 0 and len(project["alerts"]) > keep_alerts:
            project["alerts"] = project["alerts"][:keep_alerts]
        project["status"] = self._derive_status(project, now)
        self.store.upsert(project)
        if log_event:
            self.store.append_log(
                {
                    "event": log_event,
                    "project": project["id"],
                    "health": project["health_score"],
                    "status": project["status"],
                    "alert_kinds": [a["kind"] for a in project["alerts"]],
                }
            )

    def _derive_status(self, project: dict, now: datetime) -> str:
        if str(project.get("status")) in ("complete", "paused"):
            return project["status"]
        thresholds = pm_config.section("status_thresholds")
        triggers_cfg = pm_config.section("triggers")
        counts = model.task_counts(project)
        remaining = counts["open"] + counts["blocked"]
        if counts["total"] > 0 and remaining == 0:
            return "complete"

        created = model.parse_iso(project.get("created_at"))
        age_days = ((now - created).total_seconds() / 86400.0) if created else 0.0
        ghost_days = float(triggers_cfg.get("ghost_days") or 7)
        if (
            triggers_cfg.get("ghost_detection_enabled")
            and age_days >= ghost_days
            and len(project.get("updates", [])) <= int(triggers_cfg.get("ghost_max_updates") or 1)
            and model.closes_in_window(project, age_days + 1, now=now) == 0
        ):
            return "ghost"

        if model.open_blockers(project) and counts["blocked"] >= counts["open"] and remaining > 0:
            return "blocked"

        health = project.get("health_score", 100)
        if health < int(thresholds.get("stalling_health_below") or 60):
            return "stalling"
        return "active"

    # --- nightly audit ------------------------------------------------------

    def audit(self, now: datetime | None = None) -> dict:
        """Recompute every active project and refresh alerts. Returns evidence."""
        now = now or datetime.now()
        projects = self.store.all_projects()
        audited = 0
        total_alerts = 0
        for project in projects:
            if str(project.get("status")) == "complete":
                continue
            project["last_audit_at"] = now.isoformat(timespec="seconds")
            self._recompute_and_save(project, now, log_event="audit")
            total_alerts += len(project.get("alerts", []))
            audited += 1
        conflicts = triggers.cross_project_conflict(self.store.all_projects(), now=now)
        return {
            "audited": audited,
            "total_projects": len(projects),
            "alerts": total_alerts,
            "cross_project_conflicts": conflicts,
            "audited_at": now.isoformat(timespec="seconds"),
        }

    def all_alerts(self, now: datetime | None = None) -> list[dict]:
        """Every live alert across active projects, plus cross-project ones."""
        now = now or datetime.now()
        alerts: list[dict] = []
        for project in self.store.all_projects():
            if str(project.get("status")) == "complete":
                continue
            alerts.extend(project.get("alerts", []))
        alerts.extend(triggers.cross_project_conflict(self.store.all_projects(), now=now))
        alerts.sort(key=lambda a: a.get("severity", 0), reverse=True)
        return alerts

    # --- briefs -------------------------------------------------------------

    def focus_ranking(self, now: datetime | None = None) -> list[dict]:
        """Rank active projects by urgency: low health + alert pressure first."""
        now = now or datetime.now()
        ranked = []
        for project in self.store.all_projects():
            if str(project.get("status")) in ("complete", "paused"):
                continue
            alert_pressure = sum(a.get("severity", 0) for a in project.get("alerts", []))
            urgency = (100 - project.get("health_score", 100)) / 100.0 + alert_pressure
            ranked.append(
                {
                    "project": project["id"],
                    "name": project["name"],
                    "health": project.get("health_score", 100),
                    "status": project.get("status"),
                    "momentum": project.get("momentum", 0.0),
                    "urgency": round(urgency, 3),
                    "open_tasks": model.task_counts(project)["open"],
                    "alerts": [a["kind"] for a in project.get("alerts", [])],
                }
            )
        ranked.sort(key=lambda item: item["urgency"], reverse=True)
        return ranked

    def morning_brief(self, now: datetime | None = None) -> dict:
        """Package the war-room brief KING phrases for the user."""
        now = now or datetime.now()
        brief_cfg = pm_config.section("brief")
        ranking = self.focus_ranking(now=now)
        active = [p for p in self.store.all_projects() if str(p.get("status")) != "complete"]
        live_blockers = []
        for project in active:
            for blocker in model.open_blockers(project):
                live_blockers.append({"project": project["id"], "description": blocker.get("description")})

        worry = None
        worry_below = int(brief_cfg.get("worry_health_below") or 55)
        for item in ranking:
            if item["health"] < worry_below or item["alerts"]:
                worry = item
                break

        open_decisions = []
        for project in active:
            for blocker in model.open_blockers(project):
                open_decisions.append(
                    {"project": project["id"], "prompt": f"How to clear: {blocker.get('description')}"}
                )

        top_n = int(brief_cfg.get("focus_top_n") or 3)
        most_important = ranking[0] if ranking else None
        return {
            "generated_at": now.isoformat(timespec="seconds"),
            "active_count": len(active),
            "health_summary": [
                {"project": item["project"], "name": item["name"], "health": item["health"], "status": item["status"]}
                for item in ranking[:top_n]
            ],
            "top_priority": most_important,
            "live_blockers": live_blockers,
            "worry": worry,
            "open_decision": open_decisions[0] if open_decisions else None,
            "conflicts": triggers.cross_project_conflict(self.store.all_projects(), now=now),
        }

    def project_detail(self, slug: str, now: datetime | None = None) -> dict | None:
        now = now or datetime.now()
        project = self.store.get(slug)
        if project is None:
            return None
        counts = model.task_counts(project)
        eta = model.estimated_eta(project, now=now)
        return {
            "project": project["id"],
            "name": project["name"],
            "goal": project["goal"],
            "status": project["status"],
            "health": project["health_score"],
            "momentum": project["momentum"],
            "deadline": project["deadline"],
            "projected_eta": eta.date().isoformat() if eta else None,
            "task_counts": counts,
            "open_tasks": [t["title"] for t in project["tasks"] if t.get("status") == "open"],
            "blocked_tasks": [t["title"] for t in project["tasks"] if t.get("status") == "blocked"],
            "open_blockers": [b["description"] for b in model.open_blockers(project)],
            "recent_updates": project["updates"][-5:],
            "decisions": project["decisions"][-10:],
            "alerts": project.get("alerts", []),
            "velocity_per_week": round(model.velocity_per_week(project, now=now), 2),
        }

    # --- decisions ----------------------------------------------------------

    def _query_decisions(self, intent: dict) -> list[dict]:
        query = str(intent.get("query") or "").strip().lower()
        results = []
        for project in self.store.all_projects():
            for decision in project.get("decisions", []):
                text = str(decision.get("decision", "")).lower()
                context = str(decision.get("context", "")).lower()
                if not query or query in text or query in context:
                    results.append({"project": project["id"], **decision})
        results.sort(key=lambda d: str(d.get("at", "")), reverse=True)
        return results

    # --- lifecycle ----------------------------------------------------------

    def archive(self, slug: str, now: datetime | None = None) -> dict | None:
        now = now or datetime.now()
        project = self.store.get(slug)
        if project is None:
            return None
        autopsy = self._autopsy(project, now)
        project["status"] = "complete" if model.task_counts(project)["open"] == 0 else "archived"
        project["autopsy"] = autopsy
        keep = int(pm_config.value("runtime", "archive_keep") or 50)
        self.store.archive(project, keep)
        self.store.append_log({"event": "archived", "project": slug})
        return autopsy

    def _autopsy(self, project: dict, now: datetime) -> dict:
        counts = model.task_counts(project)
        created = model.parse_iso(project.get("created_at"))
        elapsed_days = ((now - created).total_seconds() / 86400.0) if created else None
        sentiments = [u.get("sentiment", "neutral") for u in project.get("updates", [])]
        arc = {
            "start": sentiments[0] if sentiments else "neutral",
            "end": sentiments[-1] if sentiments else "neutral",
        }
        dropped = [t["title"] for t in project["tasks"] if t.get("status") == "dropped"]
        blockers = [b["description"] for b in project.get("blockers", [])]
        return {
            "project": project["id"],
            "name": project["name"],
            "original_goal": project.get("goal"),
            "elapsed_days": round(elapsed_days, 1) if elapsed_days is not None else None,
            "deadline": project.get("deadline"),
            "task_summary": counts,
            "dropped_tasks": dropped,
            "blockers_encountered": blockers,
            "avg_velocity_per_week": round(model.velocity_per_week(project, now=now), 2),
            "sentiment_arc": arc,
            "decisions": project.get("decisions", []),
        }

    def resurrection_brief(self, slug: str, now: datetime | None = None) -> dict | None:
        """Package context so a cold project can be picked back up with no effort."""
        now = now or datetime.now()
        project = self.store.get(slug)
        if project is None:
            return None
        open_tasks = [t["title"] for t in project["tasks"] if t.get("status") in ("open", "blocked")]
        invested = model.closes_in_window(project, 3650, now=now)
        idle = model.days_since_activity(project, now=now)
        return {
            "project": project["id"],
            "name": project["name"],
            "original_goal": project.get("goal"),
            "idle_days": round(idle, 1) if idle is not None else None,
            "next_moves": open_tasks[:3],
            "tasks_completed_so_far": invested,
            "open_blockers": [b["description"] for b in model.open_blockers(project)],
            "last_updates": project["updates"][-3:],
        }
