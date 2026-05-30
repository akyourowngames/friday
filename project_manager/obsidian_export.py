"""Export project state to the Obsidian vault as LLM-portable context.

Goal: when the user switches the coding assistant (GPT <-> Claude, etc.) they can
hand the new model a single project markdown file and it has the full picture —
goal, status, health, every task, blockers, decisions, and recent updates — with
no need to re-gather context.

Each project becomes one deterministic markdown file under
`<vault>/<subfolder>/`, plus an index and a portable "context brief" the user can
paste straight into another model. Writes are atomic and idempotent; stale
project files are cleaned up. Reuses the same vault root as the memory worker
(`settings.memory_obsidian_vault_dir`). No regex, no hardcoded content.
"""

from __future__ import annotations

import os
import tempfile
from datetime import datetime
from pathlib import Path

from config import settings

from . import config as pm_config
from . import model


def _vault_root() -> Path:
    path = Path(settings.memory_obsidian_vault_dir)
    if not path.is_absolute():
        path = Path(__file__).resolve().parent.parent / path
    return path


def _projects_dir() -> Path:
    sub = str(pm_config.value("obsidian_export", "subfolder") or "Projects").strip() or "Projects"
    return _vault_root() / sub


def _safe_filename(text: str) -> str:
    parts = []
    for ch in str(text or "")[:80]:
        if ch.isalnum() or ch in ("-", "_", " "):
            parts.append(ch)
        else:
            parts.append(" ")
    return " ".join("".join(parts).split()).strip() or "project"


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def _fmt_date(value) -> str:
    return str(value or "").split("T")[0]


def _project_page(project: dict, now: datetime) -> str:
    """One self-contained, LLM-portable markdown page for a project."""
    counts = model.task_counts(project)
    eta = model.estimated_eta(project, now=now)
    open_blockers = model.open_blockers(project)
    velocity = round(model.velocity_per_week(project, now=now), 2)

    lines = [
        "---",
        "type: project",
        f"slug: \"{project.get('id')}\"",
        f"name: \"{project.get('name')}\"",
        f"status: \"{project.get('status')}\"",
        f"health: {project.get('health_score', 0)}",
        f"updated: \"{now.strftime('%Y-%m-%d %H:%M')}\"",
        "---",
        "",
        f"# {project.get('name')}",
        "",
        f"> **Goal:** {project.get('goal') or '(not set)'}",
        "",
        "## Snapshot",
        "",
        f"- Status: **{project.get('status')}**  |  Health: **{project.get('health_score', 0)}/100**  |  Momentum: **{project.get('momentum', 0.0)}**",
        f"- Created: {_fmt_date(project.get('created_at'))}  |  Deadline: {project.get('deadline') or 'none'}  |  Projected finish: {eta.date().isoformat() if eta else 'n/a'}",
        f"- Tasks: {counts['done']} done / {counts['open']} open / {counts['blocked']} blocked / {counts['dropped']} dropped (of {counts['total']})",
        f"- Velocity: {velocity} task closes/week",
        "",
    ]

    # Tasks grouped by status.
    lines.append("## Tasks")
    lines.append("")
    by_status = {"open": [], "blocked": [], "done": [], "dropped": []}
    for task in project.get("tasks", []):
        by_status.setdefault(str(task.get("status", "open")), []).append(task)
    for status, label in (("open", "Open"), ("blocked", "Blocked"), ("done", "Done"), ("dropped", "Dropped")):
        items = by_status.get(status) or []
        if not items:
            continue
        lines.append(f"### {label}")
        for task in items:
            mark = {"done": "x", "dropped": "~", "blocked": "!", "open": " "}.get(status, " ")
            lines.append(f"- [{mark}] {task.get('title')}")
        lines.append("")

    # Blockers.
    lines.append("## Blockers")
    lines.append("")
    if open_blockers:
        for blocker in open_blockers:
            lines.append(f"- 🔴 {blocker.get('description')} (since {_fmt_date(blocker.get('logged_at'))})")
    else:
        lines.append("- None open")
    lines.append("")

    # Decisions (the why-did-we-do-this log).
    lines.append("## Decisions")
    lines.append("")
    decisions = project.get("decisions", [])
    if decisions:
        for decision in decisions:
            ctx = f" — _{decision.get('context')}_" if decision.get("context") else ""
            lines.append(f"- {_fmt_date(decision.get('at'))}: {decision.get('decision')}{ctx}")
    else:
        lines.append("- None logged")
    lines.append("")

    # Recent updates.
    lines.append("## Recent Updates")
    lines.append("")
    updates = project.get("updates", [])[-10:]
    if updates:
        for update in updates:
            lines.append(f"- {_fmt_date(update.get('at'))} [{update.get('sentiment', 'neutral')}]: {update.get('text')}")
    else:
        lines.append("- None yet")
    lines.append("")

    # Live alerts.
    alerts = project.get("alerts", [])
    if alerts:
        lines.append("## Active Alerts")
        lines.append("")
        for alert in alerts:
            lines.append(f"- ⚠️ [{alert.get('kind')}] {alert.get('detail')}")
        lines.append("")

    lines.append("---")
    lines.append("[[Projects Index|All Projects]]")
    lines.append("")
    return "\n".join(lines)


def build_context_brief(projects: list[dict], now: datetime | None = None) -> str:
    """A dense, model-agnostic context block to paste into another assistant.

    Plain prose + structure, no KING-specific jargon, so GPT/Claude/etc. can pick
    up full project context cold. This is the "switch LLM without re-explaining"
    payload.
    """
    now = now or datetime.now()
    active = [p for p in projects if str(p.get("status")) != "complete"]
    lines = [
        "---",
        "type: context-brief",
        f"updated: \"{now.strftime('%Y-%m-%d %H:%M')}\"",
        "---",
        "",
        "# Project Context Brief",
        "",
        "Paste this into another AI assistant to give it full context on what is being built, "
        "so you do not have to re-explain. Generated by KING.",
        "",
        f"_As of {now.strftime('%Y-%m-%d %H:%M')}. {len(active)} active project(s)._",
        "",
    ]
    for project in active:
        counts = model.task_counts(project)
        open_tasks = [t["title"] for t in project.get("tasks", []) if t.get("status") == "open"]
        blocked = [t["title"] for t in project.get("tasks", []) if t.get("status") == "blocked"]
        blockers = [b["description"] for b in model.open_blockers(project)]
        decisions = [d["decision"] for d in project.get("decisions", [])]
        lines.append(f"## {project.get('name')}")
        lines.append("")
        lines.append(f"**Goal:** {project.get('goal') or '(not set)'}")
        lines.append("")
        lines.append(
            f"**Status:** {project.get('status')} (health {project.get('health_score', 0)}/100). "
            f"Deadline {project.get('deadline') or 'none'}. "
            f"{counts['done']} of {counts['total']} tasks done."
        )
        lines.append("")
        if open_tasks:
            lines.append("**Still to do:** " + "; ".join(open_tasks))
            lines.append("")
        if blocked or blockers:
            blk = "; ".join(blocked + blockers)
            lines.append("**Blocked on:** " + blk)
            lines.append("")
        if decisions:
            lines.append("**Key decisions made:** " + "; ".join(decisions))
            lines.append("")
    if not active:
        lines.append("_No active projects._")
        lines.append("")
    return "\n".join(lines)


def _index_page(projects: list[dict], archived: list[dict], now: datetime) -> str:
    lines = [
        "---",
        "type: projects-index",
        f"updated: \"{now.strftime('%Y-%m-%d %H:%M')}\"",
        f"active: {len([p for p in projects if str(p.get('status')) != 'complete'])}",
        "---",
        "",
        "# All Projects",
        "",
        "See [[Project Context Brief]] for a paste-ready summary to hand another assistant.",
        "",
        "## Active",
        "",
    ]
    active = [p for p in projects if str(p.get("status")) != "complete"]
    if active:
        lines.append("| Project | Status | Health | Open | Deadline |")
        lines.append("|---------|--------|--------|------|----------|")
        for p in active:
            counts = model.task_counts(p)
            link = f"[[{_safe_filename(p.get('name'))}|{p.get('name')}]]"
            lines.append(
                f"| {link} | {p.get('status')} | {p.get('health_score', 0)} | "
                f"{counts['open']} | {p.get('deadline') or '—'} |"
            )
    else:
        lines.append("_None._")
    lines.append("")

    if archived:
        lines.append("## Archived")
        lines.append("")
        for p in archived:
            lines.append(f"- {p.get('name')} ({p.get('status')})")
        lines.append("")
    return "\n".join(lines)


def _cleanup_stale(directory: Path, expected: set[Path]) -> int:
    removed = 0
    if not directory.exists():
        return 0
    for md_file in directory.glob("*.md"):
        if md_file not in expected:
            md_file.unlink(missing_ok=True)
            removed += 1
    return removed


def export_all(store=None, now: datetime | None = None) -> dict:
    """Write every project (and optionally archived ones) to the Obsidian vault.

    Idempotent: same state produces the same files; stale project files are
    removed. Returns an evidence dict. No-op when disabled.
    """
    cfg = pm_config.section("obsidian_export")
    if not cfg.get("enabled", True):
        return {"status": "disabled"}
    now = now or datetime.now()

    if store is None:
        from .store import ProjectStore

        store = ProjectStore()

    projects = store.all_projects()
    archived = store.all_archived() if cfg.get("include_archived", True) else []

    directory = _projects_dir()
    directory.mkdir(parents=True, exist_ok=True)

    expected: set[Path] = set()
    written = 0
    for project in projects:
        page = _project_page(project, now)
        path = directory / f"{_safe_filename(project.get('name'))}.md"
        _atomic_write(path, page)
        expected.add(path.resolve())
        written += 1

    index_path = directory / "Projects Index.md"
    _atomic_write(index_path, _index_page(projects, archived, now))
    expected.add(index_path.resolve())

    brief_written = False
    if cfg.get("context_brief", True):
        brief_path = directory / "Project Context Brief.md"
        _atomic_write(brief_path, build_context_brief(projects, now))
        expected.add(brief_path.resolve())
        brief_written = True

    removed = _cleanup_stale(directory, expected)

    return {
        "status": "ok",
        "written": written,
        "archived_listed": len(archived),
        "context_brief": brief_written,
        "stale_removed": removed,
        "directory": str(directory),
    }
