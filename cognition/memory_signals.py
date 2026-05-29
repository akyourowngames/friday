"""Memory-driven proactive signals.

Turns the brain's memory into proactive candidates so KING's nudges are grounded
in what it actually knows about you, not just activity cadence. Three sources,
all config-gated via the `memory_signals` section of `cognition/COGNITION_CONFIG.md`:

1. high-importance recent memories that may deserve a check-in (pure arithmetic;
   importance + freshness, no keyword matching),
2. unresolved time-bound commitments the user mentioned (one LLM extraction call,
   mirroring the intake parsers — the model reads the facts and returns JSON; no
   regex, no phrase tables),
3. live project-manager alerts (drift the project audit already computed).

Each source yields neutral `Candidate` objects; phrasing is left to KING. The
module never modifies agent core or routing and degrades to an empty list on any
failure.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from .config import section_values
from .proactive import Candidate
from .util import clamp01, parse_iso

_DEFAULTS = {
    "enabled": True,
    "importance_floor": 0.7,
    "recent_days": 14,
    "max_candidates": 5,
    "commitment_extraction_enabled": True,
    "commitment_max_tokens": 500,
    "commitment_lookback": 40,
    "project_alerts_enabled": True,
    "project_alert_severity_floor": 0.4,
    "project_alert_max": 5,
}


def _config() -> dict:
    return section_values("memory_signals", _DEFAULTS)


def _memory_when(memory: dict):
    date_part = str(memory.get("_date", "")).strip()
    time_part = str(memory.get("ts", "")).strip() or "00:00:00"
    if not date_part:
        return None
    return parse_iso(f"{date_part}T{time_part}")


def high_importance_candidates(memories: list[dict], cfg: dict, now: datetime) -> list[Candidate]:
    """Recent, high-importance memories become low-pressure check-in candidates.

    Pure arithmetic: a memory qualifies when its importance clears the floor and
    it falls inside the recent window. The candidate is neutral; KING decides
    whether and how to raise it.
    """
    floor = float(cfg.get("importance_floor") or 0.7)
    recent_days = float(cfg.get("recent_days") or 14)
    cutoff = now - timedelta(days=recent_days)
    scored: list[tuple[float, Candidate]] = []
    for memory in memories:
        importance = clamp01(memory.get("importance", 0.5))
        if importance < floor:
            continue
        when = _memory_when(memory)
        if when is None or when < cutoff:
            continue
        text = str(memory.get("text", "")).strip()
        if not text:
            continue
        nodes = memory.get("graph_nodes") or []
        node = str(nodes[0]) if nodes else ""
        scored.append(
            (
                importance,
                Candidate(
                    content=f"memory:high_importance {text}",
                    source="memory_high_importance",
                    importance=importance,
                    relevance=importance,
                    created_at=when.isoformat(timespec="seconds"),
                    node=node,
                ),
            )
        )
    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [candidate for _, candidate in scored]


_COMMITMENT_SYSTEM_PROMPT = (
    "You read a list of personal memory facts about a user and extract unresolved "
    "COMMITMENTS: things the user said they intend to do, plan to do, need to do, or "
    "are waiting on, that sound like they are not finished yet. Return ONLY a JSON array "
    "(no prose, no code fence). Each item:\n"
    "{\n"
    '  "commitment": short description of what the user intends to do,\n'
    '  "urgency": 0.0 to 1.0 (higher when time-bound or overdue),\n'
    '  "due_hint": any timing the user mentioned (e.g. "by Friday", "next week"), or null\n'
    "}\n"
    "Rules:\n"
    "- Only include things that read as open/unfinished intentions. Skip completed actions, "
    "stable facts (name, location, preferences), and feelings.\n"
    "- Judge by meaning, not by matching specific words. Return [] if nothing qualifies.\n"
    "- Keep each commitment under 12 words. Return strictly valid JSON."
)


def commitment_candidates(memories: list[dict], cfg: dict, now: datetime, llm_client=None) -> list[Candidate]:
    """Extract unresolved time-bound commitments from recent memory via one LLM call.

    This is the heart of memory-driven proactivity: KING notices what you said you
    would do and can surface it before you forget. Degrades to [] on any failure.
    """
    if not cfg.get("commitment_extraction_enabled", True):
        return []
    lookback = int(cfg.get("commitment_lookback") or 40)
    recent_days = float(cfg.get("recent_days") or 14)
    cutoff = now - timedelta(days=recent_days)

    facts = []
    for memory in memories:
        when = _memory_when(memory)
        if when is None or when < cutoff:
            continue
        text = str(memory.get("text", "")).strip()
        if text:
            facts.append(text)
    facts = facts[-lookback:]
    if not facts:
        return []

    if llm_client is None:
        try:
            from agent.llm import NIMClient

            llm_client = NIMClient()
        except Exception:
            return []

    try:
        import json

        from config import settings

        response = llm_client.client.chat.completions.create(
            model=settings.model_name,
            messages=[
                {"role": "system", "content": _COMMITMENT_SYSTEM_PROMPT},
                {"role": "user", "content": "Memory facts:\n- " + "\n- ".join(facts)},
            ],
            temperature=0,
            max_tokens=int(cfg.get("commitment_max_tokens") or 500),
        )
        text = response.choices[0].message.content.strip()
        if text.startswith("```"):
            lines = text.splitlines()[1:]
            if lines and lines[-1].strip().startswith("```"):
                lines = lines[:-1]
            text = "\n".join(lines).strip()
        parsed = json.loads(text)
    except Exception:
        return []

    if not isinstance(parsed, list):
        return []

    candidates: list[Candidate] = []
    for entry in parsed:
        if not isinstance(entry, dict):
            continue
        commitment = str(entry.get("commitment") or "").strip()
        if not commitment:
            continue
        urgency = clamp01(entry.get("urgency", 0.5))
        due_hint = str(entry.get("due_hint") or "").strip()
        detail = commitment if not due_hint else f"{commitment} ({due_hint})"
        candidates.append(
            Candidate(
                content=f"memory:commitment {detail}",
                source="memory_commitment",
                importance=max(0.5, urgency),
                relevance=max(0.5, urgency),
                created_at=now.isoformat(timespec="seconds"),
            )
        )
    return candidates


def project_alert_candidates(cfg: dict, now: datetime) -> list[Candidate]:
    """Live project-manager drift alerts become proactive candidates.

    Reads the project manager's already-computed alerts (from the night audit or
    the last interaction) and turns the most severe into candidates so the things
    going off track actually reach the user. Degrades to [] if the manager or its
    store is unavailable.
    """
    if not cfg.get("project_alerts_enabled", True):
        return []
    floor = float(cfg.get("project_alert_severity_floor") or 0.4)
    limit = int(cfg.get("project_alert_max") or 5)
    try:
        from project_manager.manager import ProjectManager

        alerts = ProjectManager().all_alerts(now=now)
    except Exception:
        return []

    candidates: list[Candidate] = []
    for alert in alerts:
        severity = clamp01(alert.get("severity", 0.0))
        if severity < floor:
            continue
        kind = str(alert.get("kind", "alert"))
        name = str(alert.get("project_name") or alert.get("project") or "a project")
        detail = str(alert.get("detail") or "").strip()
        content = f"project:{kind} {name}: {detail}".strip()
        candidates.append(
            Candidate(
                content=content,
                source=f"project_{kind}",
                importance=severity,
                relevance=severity,
                created_at=now.isoformat(timespec="seconds"),
                node=str(alert.get("project") or ""),
            )
        )
        if len(candidates) >= limit:
            break
    return candidates


def collect(memories: list[dict], now: datetime | None = None, embed_fn=None, llm_client=None, deep: bool = False) -> list[Candidate]:
    """Gather memory-driven proactive candidates from every enabled source.

    Caps the total to `max_candidates`, prioritizing project alerts and
    commitments (actionable) over passive high-importance recall. Attaches an
    embedding when `embed_fn` is provided so novelty suppression works.

    The LLM-backed commitment extraction only runs when ``deep`` is True (nightly
    maintenance or an explicit deep scan), so interactive callers stay fast and
    offline.
    """
    cfg = _config()
    if not cfg.get("enabled", True):
        return []
    now = now or datetime.now()

    ordered: list[Candidate] = []
    ordered.extend(project_alert_candidates(cfg, now))
    if deep:
        ordered.extend(commitment_candidates(memories, cfg, now, llm_client=llm_client))
    ordered.extend(high_importance_candidates(memories, cfg, now))

    max_candidates = int(cfg.get("max_candidates") or 5)
    selected = ordered[:max_candidates] if max_candidates > 0 else ordered

    if embed_fn is not None:
        for candidate in selected:
            try:
                import numpy as np

                vector = embed_fn(candidate.content)
                candidate.embedding = np.asarray(vector, dtype=np.float32).ravel().tolist()
            except Exception:
                candidate.embedding = None
    return selected
