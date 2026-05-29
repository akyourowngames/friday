"""Cognition orchestration for the maintenance engine.

Runs one cognition pass:
1. Rebuild the cadence model from memory activity (graph nodes observed at each
   memory's timestamp).
2. Stitch memories into episodes.
3. Turn actionable cadence deviations into proactive candidates and enqueue them.
4. Persist cadence, episodes, and proactive state.

This module is the only place the cognition substrate touches the live Brain,
and it only reads from it. It never modifies agent core or routing. Candidate
phrasing is left to whatever surfaces the queue later; here we store a neutral
structured description, never a hardcoded user-facing sentence.
"""

from __future__ import annotations

from datetime import datetime

from .cadence import CadenceModel
from .config import load_cognition_config
from .episodes import stitch_episodes
from .proactive import Candidate, ProactiveEngine
from .state import load_state, save_state
from .util import combine_date_time, clamp01


def _node_ids_for_memory(memory: dict) -> list[str]:
    nodes = memory.get("graph_nodes")
    if isinstance(nodes, list) and nodes:
        return [str(node_id) for node_id in nodes if str(node_id).strip()]
    return []


def _build_cadence(memories: list[dict], previous: dict | None) -> CadenceModel:
    # Rebuild deterministically from the corpus so the model never drifts from
    # the source of truth. Observations are replayed in timestamp order.
    model = CadenceModel(state=None)
    ordered = sorted(memories, key=lambda m: (str(m.get("_date", "")), str(m.get("ts", ""))))
    for memory in ordered:
        when = combine_date_time(memory.get("_date", ""), memory.get("ts", ""))
        if when is None:
            continue
        for node_id in _node_ids_for_memory(memory):
            model.observe(node_id, when=when)
    return model


def _deviation_candidates(cadence: CadenceModel, embed_fn, now: datetime) -> list[Candidate]:
    candidates: list[Candidate] = []
    for report in cadence.deviations(now=now):
        node = report["node"]
        kind = report["kind"]
        # Neutral structured description; the surfacing layer phrases it.
        content = f"cadence:{kind} node={node} strength={report['strength']}"
        embedding = None
        if embed_fn is not None:
            try:
                import numpy as np

                vector = embed_fn(node)
                embedding = np.asarray(vector, dtype=np.float32).ravel().tolist()
            except Exception:
                embedding = None
        candidates.append(
            Candidate(
                content=content,
                source=f"cadence_{kind}",
                importance=clamp01(report["strength"]),
                relevance=clamp01(report["strength"]),
                node=str(node),
                embedding=embedding,
            )
        )
    return candidates


def run_cognition_pass(brain, embed_fn=None, now: datetime | None = None, persist: bool = True, deep: bool = False) -> dict:
    """Execute one cognition pass over the brain's current memories.

    When ``deep`` is True (nightly maintenance / explicit deep scan), the pass also
    runs the LLM-backed memory commitment extraction. Interactive callers leave it
    False so seeding the proactive queue on a user turn stays cheap and offline.

    Returns a structured evidence dict (suitable as a maintenance step result).
    """
    now = now or datetime.now()
    memories = list(getattr(brain, "memories", []) or [])
    config = load_cognition_config()
    state = load_state()

    cadence = _build_cadence(memories, state.get("cadence"))
    episodes = stitch_episodes(memories, embed_fn=embed_fn)

    engine = ProactiveEngine.from_dict(state.get("proactive") or {})
    new_candidates = _deviation_candidates(cadence, embed_fn, now)
    for candidate in new_candidates:
        engine.add_candidate(candidate)

    # Memory-driven signals: high-importance recall, unresolved commitments, and
    # live project-manager alerts. This makes proactivity grounded in what KING
    # knows, not just activity cadence. Config-gated; degrades to [] on failure.
    memory_candidates = []
    try:
        from .memory_signals import collect as collect_memory_signals

        memory_candidates = collect_memory_signals(memories, now=now, embed_fn=embed_fn, deep=deep)
        for candidate in memory_candidates:
            engine.add_candidate(candidate)
    except Exception:
        memory_candidates = []

    payload = {
        "updated_at": now.isoformat(timespec="seconds"),
        "cadence": cadence.to_dict(),
        "episodes": episodes,
        "proactive": engine.to_dict(),
    }
    state_path = ""
    if persist:
        state_path = save_state(payload)

    return {
        "memories_seen": len(memories),
        "cadence_nodes": len(cadence.nodes),
        "episodes": len(episodes),
        "actionable_deviations": len(new_candidates),
        "memory_signals": len(memory_candidates),
        "queue_size": engine.queue_size(),
        "budget_remaining": engine.budget_remaining(now),
        "config_loaded": bool(config),
        "state_path": state_path,
    }
