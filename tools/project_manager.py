"""Registry tools for KING's autonomous project manager.

These tools are the chat surface for the project manager. Intake is natural
language: `project_track` sends the raw user message through the LLM intake
parser, which decides what the message means and updates the durable store. The
other tools read computed state (brief, focus, deep-dive, decisions, alerts) and
manage lifecycle (archive autopsy, resurrection).

Behavior is config-driven via `tools/PROJECT_MANAGER_CONFIG.md`. No keyword
routing, no canned success/failure text: every result reports the structured
facts the project manager computed, and KING phrases them.
"""

import time

from tools.registry import tool
from tools.runtime import (
    coerce_bool,
    emit_trace,
    error_payload,
    make_trace,
    normalize_response_format,
    structured_error,
    structured_success,
    utc_now_iso,
)

_PM_VERSION = "1.0.0"


def _manager():
    from project_manager.manager import ProjectManager

    return ProjectManager()


def _trace(name, started_at, started, schema_valid, status, output_fields, error_code=None):
    return make_trace(
        name,
        _PM_VERSION,
        started_at,
        started,
        1,
        schema_valid,
        name,
        status,
        output_fields,
        {"count": 1, "systems": ["project_store"]},
        error_code,
    )


def _emit(name, started, started_at, trace_enabled, result=None, error=None, response_format="legacy", legacy="", status="SUCCESS"):
    valid = error is None
    trace = _trace(
        name,
        started_at,
        started,
        valid,
        status if valid else "FAILED",
        len(result) if isinstance(result, dict) else 1,
        None if valid else error["code"],
    )
    emit_trace(trace, trace_enabled)
    if response_format == "structured":
        if valid:
            return structured_success(name, _PM_VERSION, result, started, trace)
        return structured_error(name, _PM_VERSION, error, started, trace)
    return legacy


@tool(
    name="project_track",
    description=(
        "Send a natural-language project message to the autonomous project manager. "
        "Use for starting a project, giving an update, reporting finished work, naming a "
        "blocker, recording a decision, or cutting scope. The manager parses the message, "
        "updates the tracked project, recomputes health and momentum, and returns what it did."
    ),
    examples=[
        "track this: launch a waitlist landing page by end of month",
        "update on the api project: finished the auth endpoint, frontend still stuck",
        "I finished the payment integration",
        "the launch is blocked on the legal review",
        "we're cutting the mobile version for now",
    ],
    param_descriptions={
        "message": "The user's COMPLETE, VERBATIM message about a project, copied word for word including every detail about finished work, blockers, and decisions. Do not summarize, paraphrase, or shorten it — the project manager parses the full text itself.",
        "response_format": "legacy or structured",
        "trace_enabled": "When true, emit a machine-readable trace entry",
    },
)
def project_track(message: str, response_format: str = "legacy", trace_enabled: bool = False):
    started = time.perf_counter()
    started_at = utc_now_iso()
    response_format = normalize_response_format(response_format)
    trace_enabled = coerce_bool(trace_enabled)
    text = str(message or "").strip()
    if not text:
        err = error_payload(
            "EMPTY_MESSAGE",
            "message must not be empty.",
            "message",
            text,
            "a sentence about a project",
            False,
            "Say what happened on the project.",
        )
        return _emit("project_track", started, started_at, trace_enabled, error=err, response_format=response_format, legacy="Error: nothing to track", status="FAILED")

    try:
        report = _manager().ingest(text)
    except Exception as exc:  # noqa: BLE001
        err = error_payload(
            "PROJECT_INTAKE_FAILED",
            f"{type(exc).__name__}: {exc}",
            "project_manager",
            None,
            "working project store and intake",
            True,
            "Retry; if it persists check PROJECT_MANAGER_CONFIG.md and storage permissions.",
        )
        return _emit("project_track", started, started_at, trace_enabled, error=err, response_format=response_format, legacy="Error: project intake failed", status="FAILED")

    legacy = _summarize_report(report)
    return _emit("project_track", started, started_at, trace_enabled, result=report, response_format=response_format, legacy=legacy)


def _summarize_report(report: dict) -> str:
    action = report.get("action")
    if action == "create_project":
        tasks = ", ".join(report.get("seeded_tasks") or []) or "no tasks yet"
        return (
            f"Tracking '{report.get('name')}' (health {report.get('health')}). "
            f"Seeded tasks: {tasks}."
        )
    if action == "status":
        brief = report.get("brief") or {}
        return f"Brief ready across {brief.get('active_count', 0)} active projects."
    if action == "focus":
        ranking = report.get("ranking") or []
        top = ranking[0]["name"] if ranking else "nothing"
        return f"Top focus: {top}."
    if action in ("none",):
        return "Couldn't tell which project that was about. Name the project?"
    parts = []
    for key, label in (
        ("completed", "completed"),
        ("added", "added"),
        ("dropped", "dropped"),
        ("blocked", "blocked"),
        ("blockers", "new blockers"),
        ("resolved_blockers", "cleared blockers"),
        ("decisions", "decisions"),
    ):
        items = report.get(key) or []
        if items:
            parts.append(f"{label}: {', '.join(str(i) for i in items)}")
    inferred = report.get("inferred_tasks") or []
    body = "; ".join(parts) if parts else "logged the update"
    tail = f" Health {report.get('health')}, status {report.get('status')}."
    if inferred:
        tail += f" I also inferred these tasks — did I get that right? {', '.join(inferred)}."
    return f"Updated {report.get('project')}: {body}.{tail}"


@tool(
    name="project_status",
    description=(
        "Produce the autonomous project manager's war-room brief across all active projects: "
        "health summary, the single most important thing to do, live blockers, the project to "
        "worry about, an open decision, and any deadline conflicts. KING phrases it naturally."
    ),
    examples=[
        "king status",
        "give me the project brief",
        "how are my projects doing",
        "morning brief",
    ],
    param_descriptions={
        "response_format": "legacy or structured",
        "trace_enabled": "When true, emit a machine-readable trace entry",
    },
)
def project_status(response_format: str = "legacy", trace_enabled: bool = False):
    started = time.perf_counter()
    started_at = utc_now_iso()
    response_format = normalize_response_format(response_format)
    trace_enabled = coerce_bool(trace_enabled)
    try:
        brief = _manager().morning_brief()
    except Exception as exc:  # noqa: BLE001
        err = error_payload("BRIEF_FAILED", f"{type(exc).__name__}: {exc}", "project_manager", None, "working project store", True, "Retry or check storage.")
        return _emit("project_status", started, started_at, trace_enabled, error=err, response_format=response_format, legacy="Error: could not build brief", status="FAILED")

    if brief.get("active_count", 0) == 0:
        return _emit("project_status", started, started_at, trace_enabled, result=brief, response_format=response_format, legacy="No active projects right now.")

    lines = [f"{brief['active_count']} active project(s)."]
    for item in brief.get("health_summary", []):
        lines.append(f"- {item['name']}: health {item['health']} ({item['status']})")
    top = brief.get("top_priority")
    if top:
        lines.append(f"Most important: {top['name']} (urgency {top['urgency']}).")
    if brief.get("live_blockers"):
        lines.append(f"Live blockers: {len(brief['live_blockers'])}.")
    worry = brief.get("worry")
    if worry:
        lines.append(f"Watching: {worry['name']} (health {worry['health']}).")
    legacy = "\n".join(lines)
    return _emit("project_status", started, started_at, trace_enabled, result=brief, response_format=response_format, legacy=legacy)


@tool(
    name="project_focus",
    description=(
        "Rank the active projects by urgency (low health plus alert pressure) so KING can tell "
        "the user what to work on now. Returns the ordered list with health, momentum, open task "
        "count, and the alert kinds driving each rank."
    ),
    examples=[
        "what should I focus on today",
        "which project needs me most",
        "rank my projects by urgency",
    ],
    param_descriptions={
        "response_format": "legacy or structured",
        "trace_enabled": "When true, emit a machine-readable trace entry",
    },
)
def project_focus(response_format: str = "legacy", trace_enabled: bool = False):
    started = time.perf_counter()
    started_at = utc_now_iso()
    response_format = normalize_response_format(response_format)
    trace_enabled = coerce_bool(trace_enabled)
    try:
        ranking = _manager().focus_ranking()
    except Exception as exc:  # noqa: BLE001
        err = error_payload("FOCUS_FAILED", f"{type(exc).__name__}: {exc}", "project_manager", None, "working project store", True, "Retry or check storage.")
        return _emit("project_focus", started, started_at, trace_enabled, error=err, response_format=response_format, legacy="Error: could not rank projects", status="FAILED")

    result = {"ranking": ranking, "count": len(ranking)}
    if not ranking:
        return _emit("project_focus", started, started_at, trace_enabled, result=result, response_format=response_format, legacy="No active projects to rank.")
    lines = [f"{i+1}. {item['name']} — health {item['health']}, urgency {item['urgency']}, {item['open_tasks']} open" for i, item in enumerate(ranking)]
    return _emit("project_focus", started, started_at, trace_enabled, result=result, response_format=response_format, legacy="\n".join(lines))


@tool(
    name="project_detail",
    description=(
        "Deep dive on one project: goal, status, health, momentum, deadline, projected ETA from "
        "actual velocity, task breakdown, open tasks and blockers, recent updates, decisions, and "
        "live alerts. Accepts the project slug or a partial name."
    ),
    examples=[
        "status on the landing page project",
        "deep dive on the api project",
        "where is the waitlist project at",
    ],
    param_descriptions={
        "project": "Project slug or partial name to inspect",
        "response_format": "legacy or structured",
        "trace_enabled": "When true, emit a machine-readable trace entry",
    },
)
def project_detail(project: str, response_format: str = "legacy", trace_enabled: bool = False):
    started = time.perf_counter()
    started_at = utc_now_iso()
    response_format = normalize_response_format(response_format)
    trace_enabled = coerce_bool(trace_enabled)
    slug = _resolve_slug(project)
    if slug is None:
        err = error_payload("PROJECT_NOT_FOUND", "No project matched.", "project", project, "an existing project slug or name", False, "List projects with project_status.")
        return _emit("project_detail", started, started_at, trace_enabled, error=err, response_format=response_format, legacy=f"No project matching '{project}'.", status="FAILED")
    detail = _manager().project_detail(slug)
    counts = detail["task_counts"]
    lines = [
        f"{detail['name']} — {detail['status']}, health {detail['health']}, momentum {detail['momentum']}.",
        f"Tasks: {counts['done']} done / {counts['open']} open / {counts['blocked']} blocked / {counts['dropped']} dropped.",
    ]
    if detail.get("projected_eta"):
        lines.append(f"Projected finish: {detail['projected_eta']} (deadline {detail.get('deadline') or 'none'}).")
    if detail.get("open_blockers"):
        lines.append("Blockers: " + "; ".join(detail["open_blockers"]))
    if detail.get("alerts"):
        lines.append("Alerts: " + ", ".join(a["kind"] for a in detail["alerts"]))
    return _emit("project_detail", started, started_at, trace_enabled, result=detail, response_format=response_format, legacy="\n".join(lines))


@tool(
    name="project_alerts",
    description=(
        "Return every live drift alert the trigger engine raised across active projects "
        "(inactivity, velocity collapse, deadline slip, aging blockers, health drops, scope "
        "creep, sentiment decline, ghosts, cross-project conflicts), highest severity first."
    ),
    examples=[
        "any project warnings",
        "what is the project manager worried about",
        "show project alerts",
    ],
    param_descriptions={
        "response_format": "legacy or structured",
        "trace_enabled": "When true, emit a machine-readable trace entry",
    },
)
def project_alerts(response_format: str = "legacy", trace_enabled: bool = False):
    started = time.perf_counter()
    started_at = utc_now_iso()
    response_format = normalize_response_format(response_format)
    trace_enabled = coerce_bool(trace_enabled)
    alerts = _manager().all_alerts()
    result = {"alerts": alerts, "count": len(alerts)}
    if not alerts:
        return _emit("project_alerts", started, started_at, trace_enabled, result=result, response_format=response_format, legacy="Nothing flagged across your projects.")
    lines = [f"- [{a['kind']}] {a.get('project_name') or a.get('project')}: {a['detail']}" for a in alerts]
    return _emit("project_alerts", started, started_at, trace_enabled, result=result, response_format=response_format, legacy="\n".join(lines))


@tool(
    name="project_decisions",
    description=(
        "Replay the decision log. With a topic, returns matching decisions with their date and "
        "context across all projects; without one, returns the full log newest first. This is the "
        "answer to 'why did we decide X'."
    ),
    examples=[
        "what did we decide about mobile",
        "why did we cut the contractor",
        "show the decision log",
    ],
    param_descriptions={
        "topic": "Optional topic to filter decisions; empty returns all",
        "response_format": "legacy or structured",
        "trace_enabled": "When true, emit a machine-readable trace entry",
    },
)
def project_decisions(topic: str = "", response_format: str = "legacy", trace_enabled: bool = False):
    started = time.perf_counter()
    started_at = utc_now_iso()
    response_format = normalize_response_format(response_format)
    trace_enabled = coerce_bool(trace_enabled)
    manager = _manager()
    results = manager._query_decisions({"query": str(topic or "").strip()})
    result = {"topic": topic or None, "decisions": results, "count": len(results)}
    if not results:
        legacy = f"No decisions logged for '{topic}'." if topic else "No decisions logged yet."
        return _emit("project_decisions", started, started_at, trace_enabled, result=result, response_format=response_format, legacy=legacy)
    lines = [f"- {d.get('at', '?')} [{d.get('project')}]: {d.get('decision')}" for d in results]
    return _emit("project_decisions", started, started_at, trace_enabled, result=result, response_format=response_format, legacy="\n".join(lines))


def _resolve_slug(query: str):
    """Resolve a slug or partial name to a project slug, or None."""
    from project_manager.store import ProjectStore

    text = str(query or "").strip().lower()
    if not text:
        return None
    store = ProjectStore()
    projects = store.all_projects()
    for project in projects:
        if str(project.get("id")).lower() == text:
            return project["id"]
    matches = [p for p in projects if text in str(p.get("name", "")).lower() or text in str(p.get("id", "")).lower()]
    return matches[0]["id"] if len(matches) == 1 else None


@tool(
    name="project_archive",
    description=(
        "Archive a project and generate its autopsy: original goal vs outcome, planned vs actual "
        "timeline, dropped tasks, blockers encountered, average velocity, and the sentiment arc "
        "from start to finish. Accepts the project slug or a partial name."
    ),
    examples=[
        "archive the landing page project",
        "we're done with the api project, close it out",
        "wrap up and post-mortem the waitlist project",
    ],
    param_descriptions={
        "project": "Project slug or partial name to archive",
        "response_format": "legacy or structured",
        "trace_enabled": "When true, emit a machine-readable trace entry",
    },
)
def project_archive(project: str, response_format: str = "legacy", trace_enabled: bool = False):
    started = time.perf_counter()
    started_at = utc_now_iso()
    response_format = normalize_response_format(response_format)
    trace_enabled = coerce_bool(trace_enabled)
    slug = _resolve_slug(project)
    if slug is None:
        err = error_payload("PROJECT_NOT_FOUND", "No project matched.", "project", project, "an existing project slug or name", False, "List projects with project_status.")
        return _emit("project_archive", started, started_at, trace_enabled, error=err, response_format=response_format, legacy=f"No project matching '{project}'.", status="FAILED")
    autopsy = _manager().archive(slug)
    counts = autopsy["task_summary"]
    lines = [
        f"Archived '{autopsy['name']}'. Goal: {autopsy['original_goal'] or 'n/a'}.",
        f"Shipped {counts['done']} of {counts['total']} tasks over {autopsy.get('elapsed_days', '?')} days.",
        f"Sentiment arc: {autopsy['sentiment_arc']['start']} → {autopsy['sentiment_arc']['end']}.",
    ]
    if autopsy.get("dropped_tasks"):
        lines.append("Dropped: " + ", ".join(autopsy["dropped_tasks"]))
    return _emit("project_archive", started, started_at, trace_enabled, result=autopsy, response_format=response_format, legacy="\n".join(lines))


@tool(
    name="project_resurrect",
    description=(
        "Build a resurrection brief for a cold project so picking it back up costs no mental "
        "effort: where it left off, the next three concrete moves from open tasks, how much was "
        "already done, open blockers, and the last updates. Accepts slug or partial name."
    ),
    examples=[
        "what was that landing page project again",
        "help me pick the api project back up",
        "resurrect the waitlist project",
    ],
    param_descriptions={
        "project": "Project slug or partial name to revive",
        "response_format": "legacy or structured",
        "trace_enabled": "When true, emit a machine-readable trace entry",
    },
)
def project_resurrect(project: str, response_format: str = "legacy", trace_enabled: bool = False):
    started = time.perf_counter()
    started_at = utc_now_iso()
    response_format = normalize_response_format(response_format)
    trace_enabled = coerce_bool(trace_enabled)
    slug = _resolve_slug(project)
    if slug is None:
        err = error_payload("PROJECT_NOT_FOUND", "No project matched.", "project", project, "an existing project slug or name", False, "List projects with project_status.")
        return _emit("project_resurrect", started, started_at, trace_enabled, error=err, response_format=response_format, legacy=f"No project matching '{project}'.", status="FAILED")
    brief = _manager().resurrection_brief(slug)
    lines = [
        f"'{brief['name']}' — idle {brief.get('idle_days', '?')} days. Goal: {brief.get('original_goal') or 'n/a'}.",
        f"Done so far: {brief.get('tasks_completed_so_far', 0)} tasks.",
    ]
    if brief.get("next_moves"):
        lines.append("Next moves: " + "; ".join(brief["next_moves"]))
    if brief.get("open_blockers"):
        lines.append("Still blocked on: " + "; ".join(brief["open_blockers"]))
    return _emit("project_resurrect", started, started_at, trace_enabled, result=brief, response_format=response_format, legacy="\n".join(lines))


def _desktop_notify(title: str, message: str) -> bool:
    """Best-effort local desktop toast. Returns True only when proven sent."""
    try:
        from winotify import Notification  # type: ignore

        Notification(app_id="KING", title=title, msg=message[:250]).show()
        return True
    except Exception:
        return False


def _brief_headline(brief: dict) -> str:
    if brief.get("active_count", 0) == 0:
        return "No active projects to brief on."
    parts = [f"{brief['active_count']} active."]
    top = brief.get("top_priority")
    if top:
        parts.append(f"Focus: {top['name']}.")
    if brief.get("live_blockers"):
        parts.append(f"{len(brief['live_blockers'])} blocker(s).")
    worry = brief.get("worry")
    if worry:
        parts.append(f"Watch {worry['name']} (health {worry['health']}).")
    return " ".join(parts)


def _next_daily(hhmm: str, now=None):
    """Next datetime at the given HH:MM local time, today if still ahead else tomorrow."""
    from datetime import datetime, timedelta

    now = now or datetime.now()
    text = str(hhmm or "").strip()
    hour, _, minute = text.partition(":")
    try:
        h = int(hour)
        m = int(minute) if minute else 0
    except ValueError:
        h, m = 8, 0
    if not (0 <= h <= 23 and 0 <= m <= 59):
        h, m = 8, 0
    target = now.replace(hour=h, minute=m, second=0, microsecond=0)
    if target <= now:
        target = target + timedelta(days=1)
    return target


def _brief_actions():
    from scheduler.config import load_config

    cfg = load_config(".")
    return set(cfg.action_whitelist) | {"project_brief_fire"}


@tool(
    name="project_brief_fire",
    description=(
        "Internal action invoked by the scheduler to deliver the autonomous morning war-room "
        "brief. Recomputes the brief, pushes a desktop notification, and re-schedules itself for "
        "the next day at the same time. Not for direct manual use."
    ),
    examples=["fire the scheduled morning project brief"],
    param_descriptions={
        "at_time": "Daily HH:MM local time to keep firing the brief",
        "response_format": "legacy or structured",
        "trace_enabled": "When true, emit a machine-readable trace entry",
    },
)
def project_brief_fire(at_time: str = "08:00", response_format: str = "legacy", trace_enabled: bool = False):
    started = time.perf_counter()
    started_at = utc_now_iso()
    response_format = normalize_response_format(response_format)
    trace_enabled = coerce_bool(trace_enabled)

    brief = _manager().morning_brief()
    headline = _brief_headline(brief)
    notified = _desktop_notify("KING — Morning Brief", headline) if brief.get("active_count", 0) else False

    rescheduled = None
    try:
        from scheduler.engine import build_scheduler

        scheduler = build_scheduler(allowed_actions=_brief_actions())
        nxt = _next_daily(at_time)
        record = scheduler.schedule(
            title="Morning project brief",
            action="project_brief_fire",
            scheduled_for=nxt.isoformat(timespec="seconds"),
            arguments={"at_time": at_time},
            tags=["project_brief"],
        )
        rescheduled = record.get("scheduled_for")
    except Exception:
        rescheduled = None

    result = {"brief": brief, "headline": headline, "desktop_notified": notified, "next_fire": rescheduled}
    legacy = f"Morning brief delivered. {headline}"
    return _emit("project_brief_fire", started, started_at, trace_enabled, result=result, response_format=response_format, legacy=legacy)


@tool(
    name="project_brief_schedule",
    description=(
        "Turn on the unprompted daily morning war-room brief at a chosen local time (HH:MM). "
        "Schedules the recurring brief through the scheduler engine; it then re-arms itself each "
        "day. Requires the scheduler loop to be running (nightly/maintenance service)."
    ),
    examples=[
        "send me a project brief every morning at 8",
        "turn on the daily morning brief at 07:30",
        "wake me with a project briefing at 9am",
    ],
    param_descriptions={
        "at_time": "Local time of day as HH:MM, e.g. 08:00",
        "response_format": "legacy or structured",
        "trace_enabled": "When true, emit a machine-readable trace entry",
    },
)
def project_brief_schedule(at_time: str = "08:00", response_format: str = "legacy", trace_enabled: bool = False):
    started = time.perf_counter()
    started_at = utc_now_iso()
    response_format = normalize_response_format(response_format)
    trace_enabled = coerce_bool(trace_enabled)

    try:
        from scheduler.engine import build_scheduler

        scheduler = build_scheduler(allowed_actions=_brief_actions())
        nxt = _next_daily(at_time)
        record = scheduler.schedule(
            title="Morning project brief",
            action="project_brief_fire",
            scheduled_for=nxt.isoformat(timespec="seconds"),
            arguments={"at_time": at_time},
            tags=["project_brief"],
        )
    except ValueError as exc:
        err = error_payload("SCHEDULE_REJECTED", str(exc), "scheduler", None, "valid scheduler input", False, "Confirm project_brief_fire is whitelisted in SCHEDULER_CONFIG.md.")
        return _emit("project_brief_schedule", started, started_at, trace_enabled, error=err, response_format=response_format, legacy=f"Error: {exc}", status="FAILED")
    except Exception as exc:  # noqa: BLE001
        err = error_payload("SCHEDULE_FAILED", f"{type(exc).__name__}: {exc}", "scheduler", None, "working scheduler", True, "Retry or check the scheduler store.")
        return _emit("project_brief_schedule", started, started_at, trace_enabled, error=err, response_format=response_format, legacy="Error: could not schedule the brief", status="FAILED")

    result = {"scheduled_for": record.get("scheduled_for"), "at_time": at_time, "item_id": record.get("id")}
    legacy = f"Morning brief armed for {record.get('scheduled_for')} and daily after."
    return _emit("project_brief_schedule", started, started_at, trace_enabled, result=result, response_format=response_format, legacy=legacy)


@tool(
    name="project_export",
    description=(
        "Export all projects to the Obsidian vault as portable markdown: one page per project "
        "plus an index and a paste-ready 'Project Context Brief'. Use when the user wants their "
        "project memory in Obsidian or wants context to hand another AI assistant when switching "
        "models, so they don't have to re-explain what they are building."
    ),
    examples=[
        "export my projects to obsidian",
        "save all project memory to my vault",
        "give me a context brief to paste into another assistant",
        "sync my projects to obsidian",
    ],
    param_descriptions={
        "response_format": "legacy or structured",
        "trace_enabled": "When true, emit a machine-readable trace entry",
    },
)
def project_export(response_format: str = "legacy", trace_enabled: bool = False):
    started = time.perf_counter()
    started_at = utc_now_iso()
    response_format = normalize_response_format(response_format)
    trace_enabled = coerce_bool(trace_enabled)
    try:
        from project_manager.obsidian_export import export_all

        result = export_all()
    except Exception as exc:  # noqa: BLE001
        err = error_payload("EXPORT_FAILED", f"{type(exc).__name__}: {exc}", "obsidian_export", None, "writable vault", True, "Check the Obsidian vault path and permissions.")
        return _emit("project_export", started, started_at, trace_enabled, error=err, response_format=response_format, legacy="Error: could not export projects", status="FAILED")

    if result.get("status") == "disabled":
        return _emit("project_export", started, started_at, trace_enabled, result=result, response_format=response_format, legacy="Obsidian export is disabled in PROJECT_MANAGER_CONFIG.md.")
    legacy = (
        f"Exported {result.get('written', 0)} project page(s) to {result.get('directory')}. "
        + ("A paste-ready Project Context Brief is included." if result.get("context_brief") else "")
    )
    return _emit("project_export", started, started_at, trace_enabled, result=result, response_format=response_format, legacy=legacy)
