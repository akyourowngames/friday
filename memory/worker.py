"""Memory Worker — enriches and organizes the Obsidian memory vault.

Triggered asynchronously when KING stores a new memory. Uses the LLM to:
1. Categorize the memory (person, fact, event, preference, relationship)
2. Extract entities and relationships
3. Write rich Obsidian notes with full context, backlinks, and metadata

The vault contains ONLY personal memories — no project nodes, no system state,
no tool metadata. Each note is a human-readable page about a person, fact, or
event that KING knows about the user.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime
from pathlib import Path

from config import settings

_VAULT_DIR = None


def _vault_root() -> Path:
    """Always read from settings so test isolation works."""
    path = Path(settings.memory_obsidian_vault_dir)
    if not path.is_absolute():
        path = Path(__file__).resolve().parent.parent / path
    return path


def _memory_vault() -> Path:
    return _vault_root() / "Memory"


def _atomic_write(path: Path, content: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def _safe_filename(text: str) -> str:
    parts = []
    for ch in text[:80]:
        if ch.isalnum() or ch in ("-", "_", " "):
            parts.append(ch)
        else:
            parts.append("_")
    return " ".join("".join(parts).split()).strip() or "untitled"


def _categorize_memory(text: str, graph_edges: list[dict]) -> str:
    """Simple heuristic categorization without LLM call for speed."""
    lower = text.lower()
    # Check graph edges for relationship indicators
    relations = [str(e.get("relation", "")) for e in graph_edges]
    if any(r in ("name", "lives_in", "age", "crush", "girlfriend", "boyfriend") for r in relations):
        return "person"
    if any(r in ("likes", "prefers", "dislikes", "favorite") for r in relations):
        return "preference"
    if any(r in ("preparing_for", "working_on", "building", "studying") for r in relations):
        return "activity"
    if any(word in lower for word in ("tomorrow", "today", "yesterday", "interview", "call", "meeting", "birthday")):
        return "event"
    if any(word in lower for word in ("is my", "is a", "has a", "named")):
        return "fact"
    return "fact"


def _extract_people(text: str, graph: dict, edges: list[dict]) -> list[str]:
    """Extract person names from the memory text and graph."""
    people = set()
    nodes = graph.get("nodes", {})
    for edge in edges:
        for key in ("source", "target"):
            node_id = edge.get(key, "")
            node = nodes.get(node_id, {})
            if node.get("type") == "person" and node.get("name"):
                people.add(node["name"])
    return sorted(people)


def _build_person_page(name: str, facts: list[dict], graph: dict) -> str:
    """Build a rich Obsidian page for a person."""
    now = datetime.now().isoformat(timespec="seconds")
    lines = [
        "---",
        "type: person",
        f"name: {name}",
        f"updated: {now}",
        "---",
        "",
        f"# {name}",
        "",
    ]

    # Collect all facts about this person
    relationships = []
    attributes = []
    events = []

    for fact in facts:
        text = fact.get("text", "")
        edges = fact.get("_resolved_edges", [])
        date = fact.get("_date", "")
        categorized = False
        for edge in edges:
            relation = edge.get("relation", "")
            if relation in ("crush", "girlfriend", "boyfriend", "friend", "sibling", "parent"):
                relationships.append(f"- **{relation}**: {text}")
                categorized = True
            elif relation in ("name", "age", "lives_in", "health_status"):
                attributes.append(f"- **{relation}**: {text}")
                categorized = True
        if not categorized:
            if date:
                events.append(f"- {text} *({date})*")
            else:
                events.append(f"- {text}")

    if attributes:
        lines.extend(["## About", ""])
        lines.extend(attributes)
        lines.append("")

    if relationships:
        lines.extend(["## Relationships", ""])
        lines.extend(relationships)
        lines.append("")

    if events:
        lines.extend(["## Known Facts", ""])
        lines.extend(events)
        lines.append("")

    return "\n".join(lines)


def _build_fact_page(memory: dict) -> str:
    """Build an Obsidian page for a standalone fact."""
    text = memory.get("text", "")
    date = memory.get("_date", "")
    time = memory.get("ts", "")
    importance = memory.get("importance", 0.5)
    category = _categorize_memory(text, memory.get("_resolved_edges", []))
    now = datetime.now().isoformat(timespec="seconds")

    lines = [
        "---",
        f"type: {category}",
        f"date: {date}",
        f"importance: {importance}",
        f"updated: {now}",
        "---",
        "",
        f"# {text}",
        "",
        f"- Stored: {date} {time}".strip(),
        f"- Importance: {importance}",
        "",
    ]

    # Add connections
    edges = memory.get("_resolved_edges", [])
    if edges:
        lines.extend(["## Connections", ""])
        for edge in edges:
            source = edge.get("source", "")
            target = edge.get("target", "")
            relation = edge.get("relation", "related_to")
            lines.append(f"- [[{_safe_filename(source)}]] → *{relation}* → [[{_safe_filename(target)}]]")
        lines.append("")

    return "\n".join(lines)


def _build_index(memories: list[dict], graph: dict) -> str:
    """Build the vault index page."""
    now = datetime.now().isoformat(timespec="seconds")
    people = set()
    nodes = graph.get("nodes", {})
    for node_id, node in nodes.items():
        if node.get("type") == "person" and node.get("name"):
            people.add(node["name"])

    lines = [
        "---",
        "type: index",
        f"updated: {now}",
        f"total_memories: {len(memories)}",
        "---",
        "",
        "# KING Memory",
        "",
        f"Total memories: **{len(memories)}**",
        "",
    ]

    if people:
        lines.extend(["## People", ""])
        for name in sorted(people):
            lines.append(f"- [[{_safe_filename(name)}]]")
        lines.append("")

    # Recent memories
    recent = sorted(memories, key=lambda m: (m.get("_date", ""), m.get("ts", "")), reverse=True)[:10]
    if recent:
        lines.extend(["## Recent", ""])
        for m in recent:
            text = m.get("text", "")[:80]
            date = m.get("_date", "")
            lines.append(f"- {text} *({date})*")
        lines.append("")

    return "\n".join(lines)


def sync_vault(memories: list[dict], graph: dict) -> dict:
    """Rebuild the Obsidian memory vault from current memories and graph.

    Only writes personal memory content — no project nodes, no system state.
    Called after memory storage to keep the vault current.
    """
    if not settings.memory_obsidian_sync_enabled:
        return {"status": "disabled"}

    vault = _memory_vault()
    vault.mkdir(parents=True, exist_ok=True)

    # Build edge lookup from graph
    edge_lookup = {}
    for edge in graph.get("edges", []):
        if edge.get("active", True):
            edge_id = str(edge.get("id", ""))
            if edge_id:
                edge_lookup[edge_id] = edge

    nodes = graph.get("nodes", {})

    # Resolve full edge objects for each memory
    def _resolve_edges(memory: dict) -> list[dict]:
        edge_ids = memory.get("graph_edges", [])
        if not isinstance(edge_ids, list):
            return []
        resolved = []
        for eid in edge_ids:
            if isinstance(eid, dict):
                resolved.append(eid)
            elif isinstance(eid, str) and eid in edge_lookup:
                resolved.append(edge_lookup[eid])
        return resolved

    # Group facts by person
    person_facts: dict[str, list[dict]] = {}
    standalone_facts: list[dict] = []

    for memory in memories:
        edges = _resolve_edges(memory)
        people_in_memory = set()
        for edge in edges:
            for key in ("source", "target"):
                node_id = edge.get(key, "")
                node = nodes.get(node_id, {})
                if node.get("type") == "person" and node.get("name"):
                    people_in_memory.add(node["name"])

        # Also check graph_nodes directly
        for node_id in memory.get("graph_nodes", []):
            node = nodes.get(node_id, {})
            if node.get("type") == "person" and node.get("name"):
                people_in_memory.add(node["name"])

        if people_in_memory:
            for person in people_in_memory:
                person_facts.setdefault(person, []).append(memory)
        else:
            standalone_facts.append(memory)

    written = 0

    # Write person pages
    people_dir = vault / "People"
    people_dir.mkdir(parents=True, exist_ok=True)
    for name, facts in person_facts.items():
        # Resolve edges for each fact for the page builder
        enriched_facts = []
        for f in facts:
            enriched = dict(f)
            enriched["_resolved_edges"] = _resolve_edges(f)
            enriched_facts.append(enriched)
        content = _build_person_page(name, enriched_facts, graph)
        _atomic_write(people_dir / f"{_safe_filename(name)}.md", content)
        written += 1

    # Write standalone fact pages
    facts_dir = vault / "Facts"
    facts_dir.mkdir(parents=True, exist_ok=True)
    for memory in standalone_facts:
        text = memory.get("text", "")
        if not text.strip():
            continue
        enriched = dict(memory)
        enriched["_resolved_edges"] = _resolve_edges(memory)
        content = _build_fact_page(enriched)
        filename = _safe_filename(text[:60])
        _atomic_write(facts_dir / f"{filename}.md", content)
        written += 1

    # Write index
    _atomic_write(vault / "Index.md", _build_index(memories, graph))
    written += 1

    return {
        "status": "synced",
        "vault": str(vault),
        "people": len(person_facts),
        "facts": len(standalone_facts),
        "files_written": written,
    }


def on_memory_stored(memory: dict, all_memories: list[dict], graph: dict):
    """Trigger point: called when KING stores a new memory.

    Runs the vault sync to keep Obsidian current. This is called from the
    brain's background executor so it doesn't block the conversation.
    """
    return sync_vault(all_memories, graph)
