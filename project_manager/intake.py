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
from datetime import datetime

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
        "How to choose the action (decide by meaning, never by matching words):\n"
        "- create_project: the user is starting, planning, or asking to track something new "
        "that is not already in the tracked list. Phrases that introduce a new effort, a new goal, "
        "or ask you to begin tracking are create_project. Set project_name and goal, and infer "
        + f"{min_tasks} to {max_tasks} likely sub-tasks into new_tasks.\n"
        "- log_update: progress on an EXISTING tracked project (work done, started, or stalled).\n"
        "- complete_task: the user reports finishing something. Put the finished work in "
        "completed_tasks even if no project is named (the manager resolves the project).\n"
        "- add_blocker / resolve_blocker: something is in the way, or a known blocker cleared.\n"
        "- log_decision: the user made or implied a choice (cutting scope, changing direction, "
        "picking an option). Put it in decisions.\n"
        "- status: the user wants an overview of everything. status_project: overview of one project.\n"
        "- focus: the user asks what to work on. archive_project: the project is done or abandoned.\n"
        "- query_decisions: the user asks what was decided about a topic; put the topic in query.\n"
        "- none: the message has nothing to do with projects.\n\n"
        "Rules:\n"
        "- When in doubt between create_project and log_update: if the subject is NOT in the tracked "
        "list, prefer create_project. Only use an existing slug when the message clearly concerns it.\n"
        "- Always fill completed_tasks/blocked_tasks/blockers/decisions whenever the message implies "
        "them, regardless of the chosen action.\n"
        "- inferred_tasks are shadow tasks you deduce from context (a dependency the user implies must "
        "happen first). Keep them short. sentiment reflects the tone of THIS message only.\n"
        "- Use [] for empty lists and null for empty scalars. Never invent a project slug that is not "
        "in the provided list. Return strictly valid JSON.\n\n"
        "Examples (these teach the JSON shape; generalize, do not pattern-match the words):\n"
        + _few_shot_examples(min_tasks, max_tasks)
    )


def _few_shot_examples(min_tasks: int, max_tasks: int) -> str:
    """Worked exemplars so a smaller model anchors to the contract. These are
    illustrations of the mapping, not a keyword table: the model generalizes the
    meaning, and none of this text is matched literally at runtime."""
    examples = [
        (
            "Tracked projects:\n(no projects tracked yet)\n\nUser message:\n"
            "track this: I need to launch a waitlist landing page by end of month",
            {
                "action": "create_project",
                "project": None,
                "project_name": "Waitlist Landing Page",
                "goal": "Launch a waitlist landing page by end of month",
                "deadline": None,
                "new_tasks": ["Design the page", "Build the signup form", "Set up email capture", "Deploy and test"],
                "completed_tasks": [],
                "dropped_tasks": [],
                "blocked_tasks": [],
                "blockers": [],
                "resolved_blockers": [],
                "decisions": [],
                "inferred_tasks": ["Choose an email provider"],
                "sentiment": "positive",
                "query": None,
            },
        ),
        (
            "Tracked projects:\n- slug=payment-system | name=Payment System | status=active | goal=Ship checkout\n\n"
            "User message:\nI finished the payment integration",
            {
                "action": "complete_task",
                "project": "payment-system",
                "project_name": None,
                "goal": None,
                "deadline": None,
                "new_tasks": [],
                "completed_tasks": ["Payment integration"],
                "dropped_tasks": [],
                "blocked_tasks": [],
                "blockers": [],
                "resolved_blockers": [],
                "decisions": [],
                "inferred_tasks": [],
                "sentiment": "positive",
                "query": None,
            },
        ),
        (
            "Tracked projects:\n- slug=mobile-app | name=Mobile App | status=active | goal=Ship v1\n\n"
            "User message:\nwe're killing the dark mode feature for now",
            {
                "action": "log_decision",
                "project": "mobile-app",
                "project_name": None,
                "goal": None,
                "deadline": None,
                "new_tasks": [],
                "completed_tasks": [],
                "dropped_tasks": ["Dark mode"],
                "blocked_tasks": [],
                "blockers": [],
                "resolved_blockers": [],
                "decisions": ["Dropped dark mode for now"],
                "inferred_tasks": [],
                "sentiment": "neutral",
                "query": None,
            },
        ),
    ]
    blocks = []
    for prompt_text, answer in examples:
        blocks.append(prompt_text + "\n=>\n" + json.dumps(answer))
    return "\n\n".join(blocks)



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
    retries = int(intake_cfg.get("intake_retries") or 0)

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
            "content": (
                f"Today's date is {datetime.now().date().isoformat()}. "
                "Resolve any relative deadline (e.g. 'by end of month', 'next Friday') against "
                "this date and emit an absolute YYYY-MM-DD in the correct year.\n\n"
                f"Tracked projects:\n{context}\n\nUser message:\n{message}"
            ),
        },
    ]

    # The small model occasionally returns a structurally valid but empty intent
    # for a message that clearly carries content. Retry a bounded number of times
    # until the parse captures something, then keep the best result. Deterministic
    # at temperature 0 means a retry only helps when the first call genuinely
    # missed; identical good parses are idempotent.
    best = _normalize({})
    attempts = max(1, retries + 1)
    for _ in range(attempts):
        intent = _single_parse(llm_client, messages, max_tokens)
        if not _is_empty_intent(intent, message):
            return intent
        best = intent
    return best


def _single_parse(llm_client, messages: list, max_tokens: int) -> dict:
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


def _is_empty_intent(intent: dict, message: str) -> bool:
    """True when the parse captured nothing actionable for a non-trivial message.

    A read-only action (status/focus/query) is never "empty". For write actions,
    emptiness means no project resolved and no change fields populated, even
    though the user clearly said something — the signal to retry.
    """
    action = intent.get("action")
    if action in ("status", "focus", "query_decisions", "none"):
        return False
    if action == "create_project" and (intent.get("project_name") or intent.get("goal")):
        return False
    if intent.get("project"):
        return False
    for field in (
        "new_tasks",
        "completed_tasks",
        "dropped_tasks",
        "blocked_tasks",
        "blockers",
        "resolved_blockers",
        "decisions",
        "inferred_tasks",
    ):
        if intent.get(field):
            return False
    # Nothing captured. Only treat as empty (retry-worthy) if the message had real content.
    return len(str(message or "").split()) >= 3


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
