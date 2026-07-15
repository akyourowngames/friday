"""Single retrieval entry point for user-aware Ares responses."""

from __future__ import annotations

import re
from typing import Any

from ares.actions import extract_since_reference, has_reference_language
from ares.context_blend import build_context_prompt, get_model_budgets


_CONVERSATION_SCOPE_RE = re.compile(r"^(?:conversation|telegram)-(\d+)$")


def _conversation_id_from_scope(session_id: str | None) -> int | None:
    match = _CONVERSATION_SCOPE_RE.match(str(session_id or ""))
    return int(match.group(1)) if match else None


def _without_history_duplicates(
    records: list[dict[str, Any]], history: list[dict] | None,
) -> list[dict[str, Any]]:
    known = {
        (str(item.get("role") or ""), " ".join(str(item.get("content") or "").split()))
        for item in history or []
        if item.get("content")
    }
    return [
        record for record in records
        if (
            str(record.get("role") or ""),
            " ".join(str(record.get("content") or "").split()),
        ) not in known
    ]


def build_user_context(
    user_input: str,
    *,
    config: Any,
    soul_manager: Any,
    profile_manager: Any,
    project_context: Any,
    memory_store: Any,
    conversation_store: Any | None = None,
    session_store: Any | None = None,
    session_id: str | None = None,
    people_store: Any | None = None,
    action_ledger: Any | None = None,
    task_store: Any | None = None,
    goal_store: Any | None = None,
    commitment_store: Any | None = None,
    conversation_history: list[dict] | None = None,
) -> str:
    """Retrieve and render all durable user context for one response.

    Ares is currently a single-user local application, so the current session
    scope replaces a multi-tenant ``user_id``.  Every response path calls this
    function through ``Agent.get_context``.
    """
    budgets = get_model_budgets(config.model)
    token_budget = budgets["context_token_budget"]
    max_retrieval = budgets["max_memory_retrieval"]

    soul_budget = max(200, token_budget // 10)
    profile_budget = max(400, token_budget // 5)
    project_budget = max(400, token_budget // 5)
    soul_context = soul_manager.get_context(token_budget=soul_budget)
    profile_context = profile_manager.get_context(token_budget=profile_budget)
    project_text = ""
    if getattr(config, "project_context_enabled", False):
        project_text = project_context.get_context(token_budget=project_budget)

    search_scope = "session" if session_id else "all"
    memories = memory_store.search(
        user_input,
        limit=max_retrieval,
        scope=search_scope,
        session_id=session_id,
        recent_sessions=getattr(config, "memory_session_scope", 3),
    )

    people: list[dict[str, Any]] = []
    if people_store is not None:
        named = people_store.mentioned_in(user_input, limit=4)
        recent = people_store.recent_for_context(limit=max(3, min(max_retrieval, 8)))
        seen: set[int] = set()
        for person in [*named, *recent]:
            person_id = int(person.get("person_id", 0) or 0)
            if person_id and person_id not in seen:
                seen.add(person_id)
                people.append(person)

    recent_actions = action_ledger.recent(limit=5) if action_ledger is not None else []
    active_goals = goal_store.list_all(statuses=["active"], limit=8) if goal_store is not None else []
    if goal_store is not None and active_goals:
        active_goals = goal_store.contextualize_goals(
            active_goals,
            max_age_hours=48,
            max_surfaced=3,
            per_goal=3,
            mark_surfaced=False,
        )
    goals_due_soon = goal_store.due_soon(within_days=7) if goal_store is not None else []
    goals_overdue = goal_store.overdue() if goal_store is not None else []

    pending_tasks: list[dict[str, Any]] = []
    if task_store is not None:
        pending_tasks = task_store.list_tasks(
            statuses=["pending", "running", "awaiting_confirmation", "failed"],
            limit=8,
        )
    pending_commitments = commitment_store.list_pending(limit=8) if commitment_store is not None else []

    current_conversation_id = _conversation_id_from_scope(session_id)
    recent_conversations: list[dict[str, Any]] = []
    if conversation_store is not None:
        recent_conversations = conversation_store.get_recent_context_messages(
            limit=15,
            exclude_conversation_id=current_conversation_id,
            ended_only=current_conversation_id is None,
        )
        recent_conversations = _without_history_duplicates(recent_conversations, conversation_history)

    relevant_actions: list[dict[str, Any]] = []
    conversation_recall: list[dict[str, Any]] = []
    since = None
    explicit_recall = has_reference_language(user_input)
    if explicit_recall:
        try:
            since = extract_since_reference(user_input)
        except ValueError:
            since = None

    recall_limit = max(3, min(max_retrieval, 8))
    if action_ledger is not None and explicit_recall:
        try:
            relevant_actions = action_ledger.search(user_input, since=since, limit=recall_limit)
            fallback = action_ledger.search(since=since, limit=recall_limit)
            known_ids = {item.get("action_id") for item in relevant_actions}
            relevant_actions.extend(item for item in fallback if item.get("action_id") not in known_ids)
            relevant_actions = relevant_actions[:recall_limit]
        except ValueError:
            relevant_actions = []

    if conversation_store is not None and explicit_recall and not session_id:
        try:
            conversation_recall = conversation_store.search_recall(
                user_input, since=since, limit=recall_limit,
            )
            fallback = conversation_store.search_recall(since=since, limit=recall_limit)
            known_ids = {item.get("id") for item in conversation_recall}
            conversation_recall.extend(item for item in fallback if item.get("id") not in known_ids)
            conversation_recall = conversation_recall[:recall_limit]
        except ValueError:
            conversation_recall = []

    if session_store is not None and explicit_recall:
        try:
            scan_limit = min(100, recall_limit * 3) if session_id else recall_limit
            session_recall = session_store.search_recall(user_input, since=since, limit=scan_limit)
            fallback = session_store.search_recall(since=since, limit=scan_limit)
            if session_id:
                session_recall = [item for item in session_recall if item.get("session_id") != session_id]
                fallback = [item for item in fallback if item.get("session_id") != session_id]
            session_recall = session_recall[:recall_limit]
            known_sources = {item.get("source_id") for item in session_recall}
            session_recall.extend(item for item in fallback if item.get("source_id") not in known_sources)
            for record in conversation_recall:
                record.setdefault(
                    "source_id",
                    f"conversation:{record.get('conversation_id')}:message:{record.get('id')}",
                )
            known_sources = {item.get("source_id") for item in conversation_recall}
            conversation_recall.extend(
                item for item in session_recall if item.get("source_id") not in known_sources
            )
            conversation_recall = conversation_recall[:recall_limit]
        except ValueError:
            pass

    file_action_types = {
        "file_created", "file_edited", "file_deleted", "file_moved", "file_copied",
        "directory_created", "files_batch_changed", "image_generated", "image_edited",
        "export_created",
    }
    recent_file_actions = [
        item for item in [*relevant_actions, *recent_actions]
        if item.get("action_type") in file_action_types
    ][:3]

    summaries = []
    if conversation_store is not None and not session_id:
        summaries = conversation_store.get_recent_summaries(limit=5)
    previous_summary = None
    if session_id and session_store is not None:
        previous_summary = session_store.get_previous_summary(
            session_id,
            block=getattr(config, "block_session_context", False),
        )

    prepared = build_context_prompt(
        soul_context=soul_context,
        profile_context=profile_context,
        project_context=project_text,
        memories=memories,
        people=people,
        goals=active_goals,
        goals_due_soon=goals_due_soon,
        goals_overdue=goals_overdue,
        recent_actions=recent_actions,
        relevant_actions=relevant_actions,
        recent_file_actions=recent_file_actions,
        conversation_summaries=summaries,
        recent_conversations=recent_conversations,
        conversation_recall=conversation_recall,
        previous_session_summary=previous_summary,
        pending_tasks=pending_tasks,
        pending_commitments=pending_commitments,
        token_budget=token_budget,
    )
    if goal_store is not None and active_goals:
        surfaced_ids = [
            int(signal["signal_id"])
            for goal in active_goals
            for signal in goal.get("watcher_signals") or []
            if f"signal #{signal.get('signal_id')} " in prepared
        ]
        goal_store.mark_watcher_signals_surfaced(surfaced_ids)
    return prepared


__all__ = ["build_user_context"]
