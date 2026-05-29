"""Natural-language intake for the project manager.

A single LLM call turns a user message (plus the list of known projects) into a
structured intent object. There is no keyword table and no phrase matching: the
model reads the message and the project context and returns JSON describing the
action, the target project, tasks created/completed/dropped/blocked, resolved
blockers, inferred decisions, and the emotional tone of the message.

This mirrors `NIMClient.extract_facts`: strict JSON-only system prompt,
temperature 0, and a defensive `json.loads` with a safe fallback so a parsing
failure degrades to "log this as an update" rather than crashing.
"""

from __future__ import annotations

import json

from . import config as pm_config

_ACTIONS = (
    "create_project",
    "log_update",
    "complete_task",
    "add_task",
    "drop_task",
    "add_blocker",
    "resolve_blocker",
    "log_decision",
    "status",
    "status_project",
    "focus",
    "archive_project",
    "query_decisions",
    "none",
)

_SENTIMENTS = ("positive", "neutral", "stressed", "defeated")


def _system_prompt(min_tasks: int, max_tasks: int) -> str:
    return (
        "You are the intake parser for an autonomous project manager. You are given a "
        "user message and the list of currently tracked projects. Return ONLY a single "
        "JSON object (no prose, no code fence) describing what the message means for "
        "project tracking.\n\n"
        "Schema:\n"
        "{\n"
        '  "action": one of '
        + json.dumps(list(_ACTIONS))
        + ",\n"
        '  "project": slug of an existing project this concerns, or null,\n'
        '  "project_name": a short human name when creating a new project, or null,\n'
        '  "goal": one sentence describing success for a new project, or null,\n'
        '  "deadline": ISO date (YYYY-MM-DD) if a deadline is stated or clearly implied, else null,\n'
        '  "new_tasks": [task titles to create],\n'
        '  "completed_tasks": [task titles the user says are finished],\n'
        '  "dropped_tasks": [task titles being cut or abandoned],\n'
        '  "blocked_tasks": [task titles that are now blocked],\n'
        '  "blockers": [short descriptions of things blocking progress],\n'
        '  "resolved_blockers": [descriptions of blockers the user says are cleared],\n'
        '  "decisions": [explicit or clearly implied decisions the user made],\n'
        '  "inferred_tasks": [tasks you infer are required but the user did not state outright],\n'
        '  "sentiment": one of '
        + json.dumps(list(_SENTIMENTS))
        + ",\n"
        '  "query": the topic when the user is asking what was decided, else null\n'
        "}\n\n"
        "Rules:\n"
        "- Pick the single best action. Use the existing project slug when the message clearly "
        "concerns one of the listed projects; resolve references by meaning, not by keyword.\n"
        "- For create_project, infer "
        + f"{min_tasks} to {max_tasks}"
        + " likely sub-tasks from the goal and put them in new_tasks.\n"
        "- inferred_tasks are shadow tasks you deduce from context (for example, "
        'a dependency the user implies must happen first). Keep them short.\n'
        "- sentiment reflects the emotional tone of THIS message only.\n"
        "- Use [] for empty lists and null for empty scalars. Never invent a project slug "
        "that is not in the provided list; to start a new one use create_project with project_name.\n"
        "- Return strictly valid JSON."
    )


def _empty_intent() -> dict:
    return {
        "action": "log_update",
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


def _normalize(raw: dict) -> dict:
    intent = _empty_intent()
    if not isinstance(raw, dict):
        return intent
    action = str(raw.get("action") or "").strip()
    if action in _ACTIONS:
        intent["action"] = action
    for scalar in ("project", "project_name", "goal", "deadline", "query"):
        val = raw.get(scalar)
        intent[scalar] = str(val).strip() if isinstance(val, str) and val.strip() else None
    for listed in (
        "new_tasks",
        "completed_tasks",
        "dropped_tasks",
        "blocked_tasks",
        "blockers",
        "resolved_blockers",
        "decisions",
        "inferred_tasks",
    ):
        value = raw.get(listed)
        if isinstance(value, list):
            intent[listed] = [str(item).strip() for item in value if str(item).strip()]
    sentiment = str(raw.get("sentiment") or "").strip().lower()
    if sentiment in _SENTIMENTS:
        intent["sentiment"] = sentiment
    return intent


def parse_message(message: str, projects: list[dict], llm_client=None) -> dict:
    """Parse one user message into a normalized intent dict.

    `projects` is the list of known project dicts (used to give the model the
    available slugs and goals). On any LLM or JSON failure the function returns a
    safe `log_update` intent so the caller still records the message.
    """
    intake_cfg = pm_config.section("intake")
    min_tasks = int(intake_cfg.get("infer_tasks_min") or 3)
    max_tasks = int(intake_cfg.get("infer_tasks_max") or 5)
    max_tokens = int(intake_cfg.get("intake_max_tokens") or 700)

    context_lines = []
    for project in projects:
        context_lines.append(
            f"- slug={project.get('id')} | name={project.get('name')} | "
            f"status={project.get('status')} | goal={project.get('goal')}"
        )
    context = "\n".join(context_lines) if context_lines else "(no projects tracked yet)"

    if llm_client is None:
        try:
            from agent.llm import NIMClient

            llm_client = NIMClient()
        except Exception:
            return _normalize({})

    messages = [
        {"role": "system", "content": _system_prompt(min_tasks, max_tasks)},
        {
            "role": "user",
            "content": f"Tracked projects:\n{context}\n\nUser message:\n{message}",
        },
    ]
    try:
        from config import settings

        response = llm_client.client.chat.completions.create(
            model=settings.model_name,
            messages=messages,
            temperature=0,
            max_tokens=max_tokens,
        )
        text = response.choices[0].message.content.strip()
        return _normalize(json.loads(_strip_fence(text)))
    except Exception:
        return _normalize({})


def _strip_fence(text: str) -> str:
    """Remove a leading/trailing markdown code fence if the model added one."""
    stripped = text.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        if lines:
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        stripped = "\n".join(lines).strip()
    return stripped
