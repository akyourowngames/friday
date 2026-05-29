"""Memory consolidation engine — the "god tier" nightly worker.

Turns the memory store from a growing log into a curated knowledge base. Three
jobs, all config-gated via `memory/MEMORY_CONSOLIDATION.md` + `config.py`:

1. dedup    — merge near-duplicate memories (embedding similarity + LLM judgement)
2. insights — distill clusters of related memories into higher-order facts
3. decay    — gently fade stale, low-value, non-identity memories

It operates on a live `Brain` through the brain's own mutation methods so the
graph, embeddings, vector store, and Obsidian vault stay consistent. It runs only
in the nightly maintenance pass, never on an interactive turn. Every step
degrades to a no-op on any failure and is skipped when the LLM is unavailable.

No regex, no keyword tables: duplicate and insight decisions are made by
embeddings + a single LLM judgement that reads the facts and returns JSON.
"""

from __future__ import annotations

import json

from config import settings


def _llm_json(system: str, user_content: str, max_tokens: int):
    """One-shot LLM call returning parsed JSON, or None on any failure.

    Skips when no API key is configured or the Obsidian vault path is a temp dir
    (the same test guard the worker uses), so unit tests never hit the network.
    """
    if not settings.nim_api_key or not settings.nim_api_key.strip():
        return None
    vault_path = str(settings.memory_obsidian_vault_dir)
    if "tmp" in vault_path.lower() or "temp" in vault_path.lower():
        return None
    try:
        from openai import OpenAI

        client = OpenAI(
            base_url=settings.nim_base_url,
            api_key=settings.nim_api_key,
            timeout=15,
            max_retries=0,
        )
        resp = client.chat.completions.create(
            model=settings.model_name,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user_content},
            ],
            temperature=0,
            max_tokens=max_tokens,
        )
        text = resp.choices[0].message.content.strip()
        if text.startswith("```"):
            lines = text.splitlines()[1:]
            if lines and lines[-1].strip().startswith("```"):
                lines = lines[:-1]
            text = "\n".join(lines).strip()
        return json.loads(text)
    except Exception:
        return None


def _duplicate_pairs(brain, similarity: float, max_pairs: int) -> list[tuple[int, int, float]]:
    """Find candidate duplicate memory pairs by cosine similarity of embeddings.

    Pure arithmetic over the brain's existing embedding matrix; returns index
    pairs above the similarity threshold, highest first.
    """
    import numpy as np

    embeddings = getattr(brain, "_embeddings", None)
    memories = getattr(brain, "memories", [])
    if embeddings is None or getattr(embeddings, "size", 0) == 0:
        return []
    if embeddings.ndim != 2 or embeddings.shape[0] != len(memories) or len(memories) < 2:
        return []

    # Normalize rows so a dot product is cosine similarity.
    norms = np.linalg.norm(embeddings, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    unit = embeddings / norms
    sims = np.dot(unit, unit.T)

    pairs: list[tuple[int, int, float]] = []
    n = len(memories)
    for i in range(n):
        for j in range(i + 1, n):
            score = float(sims[i, j])
            if score >= similarity:
                pairs.append((i, j, score))
    pairs.sort(key=lambda triple: triple[2], reverse=True)
    return pairs[:max_pairs]


_DEDUP_SYSTEM_PROMPT = (
    "You decide whether two short personal memory facts express the SAME underlying fact "
    "(one is a duplicate or restatement of the other), and if so, which phrasing to keep. "
    "Return ONLY JSON (no prose, no code fence):\n"
    '{"same": true|false, "merged": "the single best phrasing to keep"}\n'
    "Rules: same=true only when they truly refer to the same fact about the same subject. "
    "If they are different facts (even if related), same=false. Judge by meaning, not by "
    "shared words. When same=true, merged should be the clearest, most complete phrasing. "
    "Return strictly valid JSON."
)


def run_dedup(brain, cfg: dict) -> dict:
    """Merge near-duplicate memories. Returns evidence dict."""
    if not cfg.get("dedup_enabled", True):
        return {"merged": 0, "checked": 0, "skipped": "disabled"}
    similarity = float(cfg.get("dedup_similarity") or 0.86)
    max_pairs = int(cfg.get("dedup_max_pairs") or 20)
    max_tokens = int(cfg.get("max_tokens") or 600)

    pairs = _duplicate_pairs(brain, similarity, max_pairs)
    if not pairs:
        return {"merged": 0, "checked": 0}

    merged = 0
    checked = 0
    # Work from the original snapshot; collect texts to remove then re-commit the
    # merged phrasing so brain mutation paths stay authoritative.
    removals: list[str] = []
    additions: list[tuple[str, float]] = []
    handled_texts: set[str] = set()
    memories = list(getattr(brain, "memories", []))
    for i, j, _score in pairs:
        if i >= len(memories) or j >= len(memories):
            continue
        a = memories[i]
        b = memories[j]
        a_text = str(a.get("text", "")).strip()
        b_text = str(b.get("text", "")).strip()
        if not a_text or not b_text:
            continue
        if a_text in handled_texts or b_text in handled_texts:
            continue
        checked += 1
        verdict = _llm_json(
            _DEDUP_SYSTEM_PROMPT,
            f"Fact A: {a_text}\nFact B: {b_text}",
            max_tokens,
        )
        if not isinstance(verdict, dict) or not verdict.get("same"):
            continue
        merged_text = str(verdict.get("merged") or a_text).strip() or a_text
        importance = max(
            float(a.get("importance", 0.5) or 0.5),
            float(b.get("importance", 0.5) or 0.5),
        )
        removals.extend([a_text, b_text])
        additions.append((merged_text, importance))
        handled_texts.update([a_text, b_text])
        merged += 1

    if merged:
        for text in removals:
            try:
                brain.forget(text, reason="consolidation_dedup")
            except Exception:
                pass
        for text, importance in additions:
            try:
                brain.commit(text, importance=importance)
            except Exception:
                pass
    return {"merged": merged, "checked": checked}


def _node_clusters(brain, min_cluster: int) -> list[tuple[str, list[dict]]]:
    """Group memories by shared graph node. Returns (node_id, memories) for any
    cluster at least `min_cluster` large, biggest first."""
    clusters: dict[str, list[dict]] = {}
    for memory in getattr(brain, "memories", []):
        for node_id in memory.get("graph_nodes") or []:
            clusters.setdefault(str(node_id), []).append(memory)
    sized = [(node, mems) for node, mems in clusters.items() if len(mems) >= min_cluster]
    sized.sort(key=lambda pair: len(pair[1]), reverse=True)
    return sized


_INSIGHT_SYSTEM_PROMPT = (
    "You read several related personal memory facts about one subject and distill ONE "
    "higher-order insight that ties them together — a durable generalization the facts "
    "support. Return ONLY JSON (no prose, no code fence):\n"
    '{"insight": "one sentence higher-order fact", "worth_storing": true|false}\n'
    "Rules: worth_storing=false unless the insight adds something beyond restating a single "
    "fact. The insight must be supported by the facts; do not invent. Keep it under 20 words. "
    "Judge by meaning. Return strictly valid JSON."
)


def run_insights(brain, cfg: dict) -> dict:
    """Distill clusters of related memories into higher-order insight memories."""
    if not cfg.get("insights_enabled", True):
        return {"insights": 0, "clusters": 0, "skipped": "disabled"}
    min_cluster = int(cfg.get("insight_min_cluster") or 3)
    max_insights = int(cfg.get("insight_max") or 5)
    importance = float(cfg.get("insight_importance") or 0.75)
    max_tokens = int(cfg.get("max_tokens") or 600)

    clusters = _node_clusters(brain, min_cluster)
    if not clusters:
        return {"insights": 0, "clusters": 0}

    graph_nodes = getattr(brain, "_graph", {}).get("nodes", {})
    created = 0
    examined = 0
    for node_id, mems in clusters:
        if created >= max_insights:
            break
        examined += 1
        node_name = str(graph_nodes.get(node_id, {}).get("name") or node_id).replace("_", " ")
        facts = []
        seen = set()
        for memory in mems:
            text = str(memory.get("text", "")).strip()
            if text and text not in seen:
                seen.add(text)
                facts.append(text)
        if len(facts) < min_cluster:
            continue
        verdict = _llm_json(
            _INSIGHT_SYSTEM_PROMPT,
            f"Subject: {node_name}\nFacts:\n- " + "\n- ".join(facts),
            max_tokens,
        )
        if not isinstance(verdict, dict) or not verdict.get("worth_storing"):
            continue
        insight = str(verdict.get("insight") or "").strip()
        if not insight:
            continue
        try:
            if brain.commit(insight, importance=importance):
                created += 1
        except Exception:
            continue
    return {"insights": created, "clusters": examined}


def run_decay(brain, cfg: dict) -> dict:
    """Lower importance of stale, low-value, non-identity memories.

    Pure arithmetic, no LLM. A memory decays only when it is older than the
    threshold, not already at the floor, and not graph-backed identity (graph
    tier facts are protected). Never deletes — capacity trimming handles removal.
    """
    if not cfg.get("decay_enabled", True):
        return {"decayed": 0, "skipped": "disabled"}
    from datetime import date as _date

    after_days = int(cfg.get("decay_after_days") or 45)
    rate = float(cfg.get("decay_rate") or 0.05)
    floor = float(cfg.get("decay_floor") or 0.1)
    fallback_relation = str(settings.memory_graph_fallback_relation or "remembers").strip().casefold()
    auto_relation = "associated_with"

    today = _date.today()
    decayed = 0
    dirty_dates: set[str] = set()
    for memory in getattr(brain, "memories", []):
        # Protect identity facts: a memory backed by a real relation rule (name,
        # lives_in, crush, ...) is durable. Only memories whose graph edges are
        # nothing but the generic fallback/auto relation may decay.
        try:
            relations = brain.memory_relations(memory)
        except Exception:
            relations = set()
        meaningful = {r for r in relations if r not in (fallback_relation, auto_relation)}
        if meaningful:
            continue
        date_str = str(memory.get("_date", "")).strip()
        if not date_str:
            continue
        try:
            age_days = (today - _date.fromisoformat(date_str)).days
        except ValueError:
            continue
        if age_days < after_days:
            continue
        current = float(memory.get("importance", 0.5) or 0.5)
        if current <= floor:
            continue
        memory["importance"] = round(max(floor, current - rate), 4)
        dirty_dates.add(date_str)
        decayed += 1

    if decayed:
        try:
            brain._persist_changes(dirty_dates)
        except Exception:
            pass
    return {"decayed": decayed}


def consolidate(brain=None) -> dict:
    """Run the full nightly consolidation pass. Returns combined evidence.

    Safe to call with no brain (constructs one). Every sub-step is independently
    gated and degrades to a no-op on failure.
    """
    if not settings.memory_consolidation_enabled:
        return {"status": "disabled"}
    if brain is None:
        from memory.brain import Brain

        brain = Brain()

    cfg = {
        "dedup_enabled": settings.memory_consolidation_dedup_enabled,
        "dedup_similarity": settings.memory_consolidation_dedup_similarity,
        "dedup_max_pairs": settings.memory_consolidation_dedup_max_pairs,
        "insights_enabled": settings.memory_consolidation_insights_enabled,
        "insight_min_cluster": settings.memory_consolidation_insight_min_cluster,
        "insight_max": settings.memory_consolidation_insight_max,
        "insight_importance": settings.memory_consolidation_insight_importance,
        "decay_enabled": settings.memory_consolidation_decay_enabled,
        "decay_after_days": settings.memory_consolidation_decay_after_days,
        "decay_rate": settings.memory_consolidation_decay_rate,
        "decay_floor": settings.memory_consolidation_decay_floor,
        "max_tokens": settings.memory_consolidation_max_tokens,
    }

    result = {"status": "ok"}
    try:
        result["dedup"] = run_dedup(brain, cfg)
    except Exception as exc:  # noqa: BLE001
        result["dedup"] = {"error": f"{type(exc).__name__}: {exc}"}
    try:
        result["insights"] = run_insights(brain, cfg)
    except Exception as exc:  # noqa: BLE001
        result["insights"] = {"error": f"{type(exc).__name__}: {exc}"}
    try:
        result["decay"] = run_decay(brain, cfg)
    except Exception as exc:  # noqa: BLE001
        result["decay"] = {"error": f"{type(exc).__name__}: {exc}"}
    return result
