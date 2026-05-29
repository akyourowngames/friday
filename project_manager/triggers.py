"""Trigger engine: drift detection across projects.

Each trigger is a small pure function that compares computed project state
against a threshold from `tools/PROJECT_MANAGER_CONFIG.md` and, when the
condition holds, returns a structured alert. Alerts are neutral descriptions
(kind + subject + facts); phrasing for the user is left to KING, exactly like
the cognition proactive signals. No regex, no keyword tables, no canned
user-facing sentences.

An alert dict:
    {"kind": str, "project": slug, "severity": 0..1, "detail": str, "facts": {...}}
"""

from __future__ import annotations

from datetime import datetime

from . import config as pm_config
from . import model


def _alert(kind: str, project: dict, severity: float, detail: str, facts: dict) -> dict:
    return {
        "kind": kind,
        "project": project.get("id"),
        "project_name": project.get("name"),
        "severity": round(max(0.0, min(1.0, severity)), 3),
        "detail": detail,
        "facts": facts,
    }


def inactivity(project: dict, cfg: dict, now: datetime) -> dict | None:
    if not cfg.get("inactivity_enabled"):
        return None
    counts = model.task_counts(project)
    if counts["open"] + counts["blocked"] <= 0:
        return None
    idle = model.days_since_activity(project, now=now)
    threshold = float(cfg.get("inactivity_days") or 4)
    if idle is None or idle < threshold:
        return None
    blockers = model.open_blockers(project)
    last_blocker = blockers[-1]["description"] if blockers else ""
    return _alert(
        "inactivity",
        project,
        min(1.0, idle / (threshold * 2)),
        f"No activity for {idle:.1f} days.",
        {"idle_days": round(idle, 1), "last_blocker": last_blocker},
    )


def velocity_collapse(project: dict, cfg: dict, now: datetime) -> dict | None:
    if not cfg.get("velocity_collapse_enabled"):
        return None
    scoring = pm_config.section("scoring")
    short_window = float(scoring.get("momentum_window_days") or 7)
    long_window = float(scoring.get("velocity_window_days") or 14)
    recent = model.closes_in_window(project, short_window, now=now) / (short_window / 7.0)
    baseline = model.velocity_per_week(project, now=now)
    if baseline <= 0:
        return None
    ratio = recent / baseline
    if ratio > float(cfg.get("velocity_collapse_ratio") or 0.35):
        return None
    return _alert(
        "velocity_collapse",
        project,
        min(1.0, 1.0 - ratio),
        f"Recent close rate {recent:.1f}/wk vs usual {baseline:.1f}/wk.",
        {"recent_per_week": round(recent, 2), "baseline_per_week": round(baseline, 2)},
    )


def deadline_proximity(project: dict, cfg: dict, now: datetime) -> dict | None:
    if not cfg.get("deadline_proximity_enabled"):
        return None
    deadline = model.parse_iso(project.get("deadline"))
    if deadline is None:
        return None
    counts = model.task_counts(project)
    if counts["open"] + counts["blocked"] <= 0:
        return None
    eta = model.estimated_eta(project, now=now)
    if eta is None:
        return None
    warn_days = float(cfg.get("deadline_warn_days") or 10)
    slip_days = (eta - deadline).total_seconds() / 86400.0
    days_to_deadline = (deadline - now).total_seconds() / 86400.0
    if slip_days <= 0 or days_to_deadline > warn_days * 3:
        return None
    return _alert(
        "deadline_proximity",
        project,
        min(1.0, slip_days / max(1.0, warn_days)),
        f"At current pace you finish ~{slip_days:.0f} days past the deadline.",
        {
            "deadline": project.get("deadline"),
            "projected_eta": eta.date().isoformat(),
            "slip_days": round(slip_days, 1),
        },
    )


def blocker_age(project: dict, cfg: dict, now: datetime) -> list[dict]:
    if not cfg.get("blocker_age_enabled"):
        return []
    threshold = float(cfg.get("blocker_age_days") or 3)
    alerts = []
    for blocker in model.open_blockers(project):
        logged = model.parse_iso(blocker.get("logged_at"))
        if logged is None:
            continue
        age = (now - logged).total_seconds() / 86400.0
        if age < threshold:
            continue
        alerts.append(
            _alert(
                "blocker_age",
                project,
                min(1.0, age / (threshold * 3)),
                f"Blocker open {age:.1f} days: {blocker.get('description')}",
                {"description": blocker.get("description"), "age_days": round(age, 1)},
            )
        )
    return alerts


def health_drop(project: dict, cfg: dict, now: datetime) -> dict | None:
    if not cfg.get("health_drop_enabled"):
        return None
    history = project.get("health_history", [])
    if len(history) < 2:
        return None
    window_hours = float(cfg.get("health_drop_window_hours") or 48)
    current = project.get("health_score", 100)
    prior_score = None
    for point in reversed(history[:-1]):
        when = model.parse_iso(point.get("at"))
        if when is None:
            continue
        age_hours = (now - when).total_seconds() / 3600.0
        if age_hours <= window_hours:
            prior_score = point.get("score")
        else:
            if prior_score is None:
                prior_score = point.get("score")
            break
    if prior_score is None:
        return None
    drop = float(prior_score) - float(current)
    if drop < float(cfg.get("health_drop_points") or 15):
        return None
    return _alert(
        "health_drop",
        project,
        min(1.0, drop / 50.0),
        f"Health fell {drop:.0f} points to {current}.",
        {"from": prior_score, "to": current, "drop": round(drop, 1)},
    )


def scope_expansion(project: dict, cfg: dict, now: datetime) -> dict | None:
    if not cfg.get("scope_expansion_enabled"):
        return None
    baseline = int(project.get("creation_task_count") or 0)
    if baseline <= 0:
        return None
    counts = model.task_counts(project)
    ratio = counts["total"] / baseline
    if ratio < float(cfg.get("scope_growth_ratio") or 1.25):
        return None
    return _alert(
        "scope_expansion",
        project,
        min(1.0, (ratio - 1.0)),
        f"Task list grew from {baseline} to {counts['total']}.",
        {"baseline": baseline, "current": counts["total"], "ratio": round(ratio, 2)},
    )


def sentiment_deterioration(project: dict, cfg: dict, now: datetime) -> dict | None:
    if not cfg.get("sentiment_deterioration_enabled"):
        return None
    streak, worst = model.recent_sentiment_streak(project)
    threshold = int(cfg.get("sentiment_streak") or 3)
    if streak < threshold:
        return None
    return _alert(
        "sentiment_deterioration",
        project,
        min(1.0, streak / (threshold * 2)),
        f"{streak} updates in a row trending {worst}.",
        {"streak": streak, "worst": worst},
    )


def ghost_detection(project: dict, cfg: dict, now: datetime) -> dict | None:
    if not cfg.get("ghost_detection_enabled"):
        return None
    if str(project.get("status")) in ("complete", "ghost"):
        return None
    created = model.parse_iso(project.get("created_at"))
    if created is None:
        return None
    age_days = (now - created).total_seconds() / 86400.0
    if age_days < float(cfg.get("ghost_days") or 7):
        return None
    if len(project.get("updates", [])) > int(cfg.get("ghost_max_updates") or 1):
        return None
    if model.closes_in_window(project, age_days + 1, now=now) > 0:
        return None
    return _alert(
        "ghost",
        project,
        0.6,
        f"Created {age_days:.0f} days ago with almost no activity.",
        {"age_days": round(age_days, 1), "update_count": len(project.get("updates", []))},
    )


def evaluate_project(project: dict, now: datetime | None = None) -> list[dict]:
    """Run every per-project trigger and return the alerts that fired."""
    now = now or datetime.now()
    cfg = pm_config.section("triggers")
    alerts: list[dict] = []
    for single in (
        inactivity,
        velocity_collapse,
        deadline_proximity,
        health_drop,
        scope_expansion,
        sentiment_deterioration,
        ghost_detection,
    ):
        result = single(project, cfg, now)
        if result:
            alerts.append(result)
    alerts.extend(blocker_age(project, cfg, now))
    return alerts


def cross_project_conflict(projects: list[dict], now: datetime | None = None) -> list[dict]:
    """Flag deadline collisions between active projects in a shared window."""
    now = now or datetime.now()
    cfg = pm_config.section("triggers")
    if not cfg.get("cross_project_conflict_enabled"):
        return []
    window = float(cfg.get("conflict_window_days") or 3)
    dated = []
    for project in projects:
        if str(project.get("status")) not in ("active", "stalling", "blocked"):
            continue
        deadline = model.parse_iso(project.get("deadline"))
        counts = model.task_counts(project)
        if deadline is not None and (counts["open"] + counts["blocked"]) > 0:
            dated.append((deadline, project))
    alerts = []
    for i in range(len(dated)):
        for j in range(i + 1, len(dated)):
            gap = abs((dated[i][0] - dated[j][0]).total_seconds()) / 86400.0
            if gap > window:
                continue
            a, b = dated[i][1], dated[j][1]
            alerts.append(
                {
                    "kind": "cross_project_conflict",
                    "project": a.get("id"),
                    "project_name": a.get("name"),
                    "severity": round(max(0.0, min(1.0, 1.0 - gap / max(1.0, window))), 3),
                    "detail": f"'{a.get('name')}' and '{b.get('name')}' both land within {window:.0f} days.",
                    "facts": {
                        "projects": [a.get("id"), b.get("id")],
                        "deadlines": [a.get("deadline"), b.get("deadline")],
                        "gap_days": round(gap, 1),
                    },
                }
            )
    return alerts
