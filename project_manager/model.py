"""Project data core: skeleton creation, slugging, and derived metrics.

A project is a plain dict so it serializes straight to JSON. This module holds
pure helpers that build and measure projects. Health, momentum, velocity, and
ETA are computed from the recorded facts (tasks, updates, blockers) using the
weights in `tools/PROJECT_MANAGER_CONFIG.md`. No regex, no hardcoded thresholds.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from . import config as pm_config

_VALID_TASK_STATUS = ("open", "done", "blocked", "dropped")
_SENTIMENT_SCORE = {"positive": 1.0, "neutral": 0.5, "stressed": 0.2, "defeated": 0.0}


# --- time helpers -----------------------------------------------------------

def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def parse_iso(value):
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        # Tolerate a bare date.
        try:
            return datetime.fromisoformat(text + "T00:00:00")
        except ValueError:
            return None


def _days_between(earlier, later) -> float | None:
    a = parse_iso(earlier)
    b = parse_iso(later)
    if a is None or b is None:
        return None
    return (b - a).total_seconds() / 86400.0


# --- slug -------------------------------------------------------------------

def slugify(name: str, existing: set[str] | None = None) -> str:
    """Build a filesystem-safe, unique slug from a name without regex."""
    allowed = "abcdefghijklmnopqrstuvwxyz0123456789"
    lowered = str(name or "").strip().lower()
    chars = []
    for ch in lowered:
        if ch in allowed:
            chars.append(ch)
        elif ch in " -_/\t":
            chars.append("-")
        # drop everything else
    slug = "".join(chars)
    while "--" in slug:
        slug = slug.replace("--", "-")
    slug = slug.strip("-") or "project"
    if existing is None:
        return slug
    candidate = slug
    suffix = 2
    while candidate in existing:
        candidate = f"{slug}-{suffix}"
        suffix += 1
    return candidate


# --- skeleton ---------------------------------------------------------------

def new_project(slug: str, name: str, goal: str, deadline: str | None = None) -> dict:
    stamp = now_iso()
    return {
        "id": slug,
        "name": str(name or slug).strip(),
        "goal": str(goal or "").strip(),
        "created_at": stamp,
        "deadline": str(deadline).strip() if deadline else None,
        "status": "active",
        "tasks": [],
        "updates": [],
        "blockers": [],
        "decisions": [],
        "health_score": 100,
        "momentum": 0.0,
        "health_history": [{"at": stamp, "score": 100}],
        "creation_task_count": 0,
        "dna": {},
        "alerts": [],
        "last_audit_at": None,
    }


def next_task_id(project: dict) -> int:
    existing = [int(t.get("id") or 0) for t in project.get("tasks", [])]
    return (max(existing) + 1) if existing else 1


def add_task(project: dict, title: str, effort: float | None = None, status: str = "open") -> dict:
    title = str(title or "").strip()
    if not title:
        return {}
    status = status if status in _VALID_TASK_STATUS else "open"
    task = {
        "id": next_task_id(project),
        "title": title,
        "status": status,
        "estimated_effort": effort,
        "actual_effort": None,
        "created_at": now_iso(),
        "closed_at": now_iso() if status in ("done", "dropped") else None,
    }
    project.setdefault("tasks", []).append(task)
    return task


def find_task(project: dict, title: str) -> dict | None:
    """Resolve a task by exact title, then unique case-insensitive substring."""
    wanted = str(title or "").strip().lower()
    if not wanted:
        return None
    for task in project.get("tasks", []):
        if str(task.get("title", "")).strip().lower() == wanted:
            return task
    matches = [t for t in project.get("tasks", []) if wanted in str(t.get("title", "")).lower()]
    return matches[0] if len(matches) == 1 else None


def set_task_status(project: dict, title: str, status: str) -> dict | None:
    task = find_task(project, title)
    if task is None:
        return None
    status = status if status in _VALID_TASK_STATUS else task.get("status", "open")
    task["status"] = status
    if status in ("done", "dropped"):
        task["closed_at"] = now_iso()
    else:
        task["closed_at"] = None
    return task


def add_update(project: dict, text: str, sentiment: str = "neutral") -> dict:
    sentiment = sentiment if sentiment in _SENTIMENT_SCORE else "neutral"
    update = {"text": str(text or "").strip(), "at": now_iso(), "sentiment": sentiment}
    project.setdefault("updates", []).append(update)
    keep = int(pm_config.value("runtime", "updates_keep") or 200)
    if keep > 0 and len(project["updates"]) > keep:
        project["updates"] = project["updates"][-keep:]
    return update


def add_blocker(project: dict, description: str) -> dict:
    description = str(description or "").strip()
    if not description:
        return {}
    # Re-open or skip a matching live blocker instead of duplicating.
    for existing in project.get("blockers", []):
        if str(existing.get("description", "")).strip().lower() == description.lower() and not existing.get("resolved"):
            return existing
    blocker = {"description": description, "logged_at": now_iso(), "resolved": False, "resolved_at": None}
    project.setdefault("blockers", []).append(blocker)
    return blocker


def resolve_blocker(project: dict, description: str) -> dict | None:
    wanted = str(description or "").strip().lower()
    if not wanted:
        return None
    for blocker in project.get("blockers", []):
        desc = str(blocker.get("description", "")).strip().lower()
        if (desc == wanted or wanted in desc) and not blocker.get("resolved"):
            blocker["resolved"] = True
            blocker["resolved_at"] = now_iso()
            return blocker
    return None


def add_decision(project: dict, text: str, context: str = "") -> dict:
    text = str(text or "").strip()
    if not text:
        return {}
    decision = {"decision": text, "context": str(context or "").strip(), "at": now_iso()}
    project.setdefault("decisions", []).append(decision)
    return decision


# --- derived metrics --------------------------------------------------------

def open_blockers(project: dict) -> list[dict]:
    return [b for b in project.get("blockers", []) if not b.get("resolved")]


def task_counts(project: dict) -> dict:
    counts = {"open": 0, "done": 0, "blocked": 0, "dropped": 0}
    for task in project.get("tasks", []):
        status = str(task.get("status", "open"))
        if status in counts:
            counts[status] += 1
    counts["total"] = len(project.get("tasks", []))
    return counts


def closes_in_window(project: dict, days: float, now: datetime | None = None) -> int:
    now = now or datetime.now()
    cutoff = now - timedelta(days=days)
    count = 0
    for task in project.get("tasks", []):
        if str(task.get("status")) != "done":
            continue
        closed = parse_iso(task.get("closed_at"))
        if closed is not None and closed >= cutoff:
            count += 1
    return count


def updates_in_window(project: dict, days: float, now: datetime | None = None) -> int:
    now = now or datetime.now()
    cutoff = now - timedelta(days=days)
    count = 0
    for update in project.get("updates", []):
        when = parse_iso(update.get("at"))
        if when is not None and when >= cutoff:
            count += 1
    return count


def last_activity_at(project: dict):
    """Most recent timestamp across updates and task closes, else created_at."""
    stamps = [parse_iso(project.get("created_at"))]
    for update in project.get("updates", []):
        stamps.append(parse_iso(update.get("at")))
    for task in project.get("tasks", []):
        stamps.append(parse_iso(task.get("closed_at")))
    stamps = [s for s in stamps if s is not None]
    return max(stamps) if stamps else None


def days_since_activity(project: dict, now: datetime | None = None) -> float | None:
    now = now or datetime.now()
    last = last_activity_at(project)
    if last is None:
        return None
    return (now - last).total_seconds() / 86400.0


def recent_sentiment_streak(project: dict) -> tuple[int, str]:
    """Length and label of the trailing run of negative-trending sentiment.

    A run counts updates whose sentiment is stressed or defeated. Returns
    (streak_length, worst_label).
    """
    streak = 0
    worst = "neutral"
    for update in reversed(project.get("updates", [])):
        sentiment = str(update.get("sentiment", "neutral"))
        if sentiment in ("stressed", "defeated"):
            streak += 1
            if sentiment == "defeated":
                worst = "defeated"
            elif worst != "defeated":
                worst = "stressed"
        else:
            break
    return streak, worst


def velocity_per_week(project: dict, now: datetime | None = None) -> float:
    """Average task closes per week over the velocity window."""
    window = float(pm_config.value("scoring", "velocity_window_days") or 14)
    if window <= 0:
        return 0.0
    closes = closes_in_window(project, window, now=now)
    return closes / (window / 7.0)


def momentum(project: dict, now: datetime | None = None) -> float:
    """0..1 rolling momentum from task closes + update cadence in the window."""
    scoring = pm_config.section("scoring")
    window = float(scoring.get("momentum_window_days") or 7)
    expected = float(scoring.get("momentum_expected_per_week") or 4)
    if window <= 0 or expected <= 0:
        return 0.0
    weeks = window / 7.0
    closes = closes_in_window(project, window, now=now)
    updates = updates_in_window(project, window, now=now)
    close_rate = closes / weeks
    update_rate = updates / weeks
    cw = float(scoring.get("momentum_close_weight") or 0.6)
    uw = float(scoring.get("momentum_update_weight") or 0.4)
    raw = (cw * (close_rate / expected)) + (uw * min(1.0, update_rate / expected))
    return max(0.0, min(1.0, raw))


def estimated_eta(project: dict, now: datetime | None = None):
    """Project completion date from actual velocity, or None if not computable."""
    now = now or datetime.now()
    counts = task_counts(project)
    remaining = counts["open"] + counts["blocked"]
    if remaining <= 0:
        return None
    weekly = velocity_per_week(project, now=now)
    if weekly <= 0:
        return None
    weeks_needed = remaining / weekly
    return now + timedelta(days=weeks_needed * 7.0)


def compute_health(project: dict, now: datetime | None = None) -> int:
    """Recompute the 0..100 health score from current project facts.

    Starts at 100 and subtracts weighted penalties for live blockers, overdue
    deadline, stalling, negative sentiment streaks, and scope expansion. Pure
    arithmetic over recorded facts; every weight comes from the markdown.
    """
    now = now or datetime.now()
    scoring = pm_config.section("scoring")
    triggers = pm_config.section("triggers")
    score = 100.0

    # Live blockers.
    blockers = open_blockers(project)
    if blockers:
        score -= float(scoring.get("health_blocker_penalty") or 12) * min(3, len(blockers))

    # Overdue or imminent deadline with work remaining.
    deadline = parse_iso(project.get("deadline"))
    counts = task_counts(project)
    remaining = counts["open"] + counts["blocked"]
    if deadline is not None and remaining > 0:
        days_left = (deadline - now).total_seconds() / 86400.0
        if days_left < 0:
            score -= float(scoring.get("health_overdue_penalty") or 25)
        else:
            eta = estimated_eta(project, now=now)
            if eta is not None and eta > deadline:
                score -= float(scoring.get("health_overdue_penalty") or 25) * 0.6

    # Stalling: no activity beyond the inactivity threshold.
    idle = days_since_activity(project, now=now)
    inactivity_days = float(triggers.get("inactivity_days") or 4)
    if idle is not None and idle > inactivity_days and remaining > 0:
        score -= float(scoring.get("health_stall_penalty") or 20)

    # Negative sentiment streak.
    streak, _ = recent_sentiment_streak(project)
    if streak >= int(triggers.get("sentiment_streak") or 3):
        score -= float(scoring.get("health_sentiment_penalty") or 18)

    # Scope expansion past creation baseline.
    baseline = int(project.get("creation_task_count") or 0)
    growth_ratio = float(triggers.get("scope_growth_ratio") or 1.25)
    if baseline > 0 and counts["total"] > baseline * growth_ratio:
        score -= float(scoring.get("health_scope_penalty") or 10)

    return int(max(0, min(100, round(score))))
