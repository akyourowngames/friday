"""Memory Worker — builds and maintains the Obsidian memory vault.

Triggered when KING stores a new memory. Writes a clean personal knowledge
graph to the vault with rich Obsidian pages, wiki-link connections, and
proper backlinks so the graph view shows meaningful relationships.

Vault structure:
    People/         Person pages with all known facts grouped by type
    Facts/          Standalone facts not tied to a specific person
    Timeline/       Date-grouped memory entries for chronological browsing
    Index.md        Dashboard with people, stats, and recent memories

Design:
- Only personal memories. No project nodes, no system state, no tool metadata.
- Every person and fact page uses [[wiki links]] so Obsidian graph view shows
  the actual relationship web between entities.
- Idempotent: calling sync_vault twice with the same data produces the same
  output. Stale files from removed memories are cleaned up.
"""

from __future__ import annotations

import os
import tempfile
from datetime import datetime
from pathlib import Path

from config import settings


def _vault_root() -> Path:
    path = Path(settings.memory_obsidian_vault_dir)
    if not path.is_absolute():
        path = Path(__file__).resolve().parent.parent / path
    return path


def _memory_vault() -> Path:
    return _vault_root()


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
    """Convert text to a safe filename preserving readability."""
    parts = []
    for ch in text[:80]:
        if ch.isalnum() or ch in ("-", "_", " "):
            parts.append(ch)
        elif ch in ("'", "'"):
            continue
        else:
            parts.append(" ")
    result = " ".join("".join(parts).split()).strip()
    return result or "untitled"


def _wiki_link(text: str, folder: str = "") -> str:
    """Create an Obsidian wiki link. Folder prefix for cross-folder links."""
    name = _safe_filename(text)
    if folder:
        return f"[[{folder}/{name}|{name}]]"
    return f"[[{name}]]"


def _capitalize(text: str) -> str:
    return text[:1].upper() + text[1:] if text else text


# ─── Page builders ──────────────────────────────────────────────────────────


def _build_person_page(name: str, facts: list[dict], graph: dict, all_people: set) -> str:
    """Build a rich person page with sections and cross-links."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    fact_count = len(facts)

    lines = [
        "---",
        f"type: person",
        f"name: \"{name}\"",
        f"facts: {fact_count}",
        f"updated: \"{now}\"",
        "---",
        "",
        f"# {_capitalize(name)}",
        "",
    ]

    # Categorize facts into sections
    identity = []      # name, age, location
    relationships = [] # crush, girlfriend, friend
    preferences = []   # likes, prefers, dislikes
    activities = []    # working_on, preparing_for, studying
    general = []       # everything else

    seen_texts = set()
    for fact in facts:
        text = fact.get("text", "").strip()
        if not text or text in seen_texts:
            continue
        seen_texts.add(text)
        edges = fact.get("_resolved_edges", [])
        date = fact.get("_date", "")
        categorized = False

        for edge in edges:
            relation = str(edge.get("relation", ""))
            target_id = str(edge.get("target", ""))
            source_id = str(edge.get("source", ""))

            # Build cross-links to other people mentioned
            other_person = None
            nodes = graph.get("nodes", {})
            for nid in (source_id, target_id):
                node = nodes.get(nid, {})
                node_name = node.get("name", "")
                if node.get("type") == "person" and node_name and node_name != name:
                    other_person = node_name

            link_suffix = f" → {_wiki_link(other_person, 'People')}" if other_person else ""

            if relation in ("name", "age", "lives_in", "health_status", "username"):
                identity.append(f"- **{_capitalize(relation.replace('_', ' '))}**: {text}{link_suffix}")
                categorized = True
            elif relation in ("crush", "girlfriend", "boyfriend", "friend", "sibling", "parent", "partner"):
                relationships.append(f"- **{_capitalize(relation)}**: {text}{link_suffix}")
                categorized = True
            elif relation in ("likes", "prefers", "dislikes", "favorite"):
                preferences.append(f"- {text}")
                categorized = True
            elif relation in ("preparing_for", "working_on", "building", "studying", "in_class"):
                activities.append(f"- {text}" + (f" *({date})*" if date else ""))
                categorized = True

        if not categorized:
            entry = f"- {text}"
            if date:
                entry += f" *({date})*"
            general.append(entry)

    if identity:
        lines.extend(["## Identity", ""])
        lines.extend(sorted(set(identity)))
        lines.append("")

    if relationships:
        lines.extend(["## Relationships", ""])
        lines.extend(sorted(set(relationships)))
        lines.append("")

    if preferences:
        lines.extend(["## Preferences", ""])
        lines.extend(sorted(set(preferences)))
        lines.append("")

    if activities:
        lines.extend(["## Activities", ""])
        lines.extend(activities)
        lines.append("")

    if general:
        lines.extend(["## Notes", ""])
        lines.extend(general)
        lines.append("")

    # Backlink to index
    lines.extend(["---", f"*{fact_count} memories* · [[Index]]", ""])

    return "\n".join(lines)


def _build_fact_page(memory: dict, graph: dict) -> str:
    """Build a standalone fact page with connections."""
    text = memory.get("text", "")
    date = memory.get("_date", "")
    time_str = memory.get("ts", "")
    importance = memory.get("importance", 0.5)
    edges = memory.get("_resolved_edges", [])
    now = datetime.now().strftime("%Y-%m-%d %H:%M")

    # Determine category from edges
    category = "fact"
    relations = [str(e.get("relation", "")) for e in edges]
    if any(r in ("likes", "prefers", "dislikes", "favorite") for r in relations):
        category = "preference"
    elif any(r in ("preparing_for", "working_on", "building", "studying") for r in relations):
        category = "activity"
    elif any(word in text.lower() for word in ("tomorrow", "today", "yesterday", "interview", "call", "meeting", "birthday")):
        category = "event"

    lines = [
        "---",
        f"type: {category}",
        f"date: \"{date}\"",
        f"importance: {importance}",
        f"updated: \"{now}\"",
        "---",
        "",
        f"# {text}",
        "",
    ]

    if date or time_str:
        lines.append(f"Stored: **{date}** {time_str}".strip())
        lines.append("")

    # Cross-link to people mentioned
    nodes = graph.get("nodes", {})
    people_linked = []
    if edges:
        lines.extend(["## Connections", ""])
        for edge in edges:
            source_id = edge.get("source", "")
            target_id = edge.get("target", "")
            relation = edge.get("relation", "related_to")
            source_node = nodes.get(source_id, {})
            target_node = nodes.get(target_id, {})
            source_name = source_node.get("name", source_id)
            target_name = target_node.get("name", target_id)

            # Use wiki links for people
            source_link = _wiki_link(source_name, "People") if source_node.get("type") == "person" else source_name
            target_link = _wiki_link(target_name, "People") if target_node.get("type") == "person" else target_name

            lines.append(f"- {source_link} → *{relation.replace('_', ' ')}* → {target_link}")

            for nid, node in ((source_id, source_node), (target_id, target_node)):
                if node.get("type") == "person" and node.get("name"):
                    people_linked.append(node["name"])
        lines.append("")

    # Backlink
    lines.extend(["---", "[[Index]]", ""])

    return "\n".join(lines)


def _build_timeline_page(date_str: str, day_memories: list[dict]) -> str:
    """Build a daily timeline page."""
    lines = [
        "---",
        f"type: timeline",
        f"date: \"{date_str}\"",
        f"count: {len(day_memories)}",
        "---",
        "",
        f"# {date_str}",
        "",
    ]

    sorted_mems = sorted(day_memories, key=lambda m: m.get("ts", ""))
    for m in sorted_mems:
        text = m.get("text", "")
        time_str = m.get("ts", "")
        prefix = f"**{time_str}** " if time_str else ""
        lines.append(f"- {prefix}{text}")

    lines.extend(["", "---", "[[Index]]", ""])
    return "\n".join(lines)


def _build_index(memories: list[dict], graph: dict, person_count: int, fact_count: int) -> str:
    """Build the vault index/dashboard."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    nodes = graph.get("nodes", {})
    people = sorted(
        {node.get("name") for node in nodes.values() if node.get("type") == "person" and node.get("name")}
    )

    lines = [
        "---",
        f"type: index",
        f"updated: \"{now}\"",
        f"total_memories: {len(memories)}",
        f"people: {len(people)}",
        "---",
        "",
        "# KING Memory",
        "",
        f"> {len(memories)} memories · {len(people)} people · updated {now}",
        "",
    ]

    # People section with links
    if people:
        lines.extend(["## People", ""])
        for name in people:
            lines.append(f"- {_wiki_link(name, 'People')}")
        lines.append("")

    # Recent memories
    recent = sorted(memories, key=lambda m: (m.get("_date", ""), m.get("ts", "")), reverse=True)[:15]
    if recent:
        lines.extend(["## Recent Memories", ""])
        for m in recent:
            text = m.get("text", "")[:100]
            date = m.get("_date", "")
            time_str = m.get("ts", "")
            timestamp = f"{date} {time_str}".strip()
            lines.append(f"- {text}" + (f" — *{timestamp}*" if timestamp else ""))
        lines.append("")

    # Timeline links
    dates = sorted({m.get("_date", "") for m in memories if m.get("_date")}, reverse=True)
    if dates:
        lines.extend(["## Timeline", ""])
        for d in dates[:14]:
            count = sum(1 for m in memories if m.get("_date") == d)
            lines.append(f"- {_wiki_link(d, 'Timeline')} ({count} memories)")
        lines.append("")

    # Stats
    lines.extend([
        "## Stats",
        "",
        f"| Metric | Value |",
        f"|--------|-------|",
        f"| Total memories | {len(memories)} |",
        f"| People tracked | {len(people)} |",
        f"| Person pages | {person_count} |",
        f"| Fact pages | {fact_count} |",
        f"| Timeline days | {len(dates)} |",
        "",
    ])

    return "\n".join(lines)


# ─── Sync engine ────────────────────────────────────────────────────────────


def _cleanup_stale_files(vault: Path, expected_files: set[Path]):
    """Remove .md files in managed folders that are no longer expected."""
    managed_dirs = [vault / "People", vault / "Facts", vault / "Timeline"]
    for managed in managed_dirs:
        if not managed.exists():
            continue
        for md_file in managed.glob("*.md"):
            if md_file not in expected_files:
                md_file.unlink(missing_ok=True)


def sync_vault(memories: list[dict], graph: dict) -> dict:
    """Rebuild the Obsidian memory vault from current memories and graph.

    Idempotent: same input produces same output. Stale files are cleaned up.
    """
    if not settings.memory_obsidian_sync_enabled:
        return {"status": "disabled"}

    vault = _memory_vault()
    vault.mkdir(parents=True, exist_ok=True)

    # Build edge lookup
    edge_lookup = {}
    for edge in graph.get("edges", []):
        if edge.get("active", True):
            edge_id = str(edge.get("id", ""))
            if edge_id:
                edge_lookup[edge_id] = edge

    nodes = graph.get("nodes", {})

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

    # Collect all people names for cross-linking
    all_people = {
        node.get("name") for node in nodes.values()
        if node.get("type") == "person" and node.get("name")
    }

    # Group memories by person
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

        for node_id in memory.get("graph_nodes", []):
            node = nodes.get(node_id, {})
            if node.get("type") == "person" and node.get("name"):
                people_in_memory.add(node["name"])

        enriched = dict(memory)
        enriched["_resolved_edges"] = edges

        if people_in_memory:
            for person in people_in_memory:
                person_facts.setdefault(person, []).append(enriched)
        else:
            standalone_facts.append(enriched)

    # Group by date for timeline
    by_date: dict[str, list[dict]] = {}
    for memory in memories:
        d = memory.get("_date", "")
        if d:
            by_date.setdefault(d, []).append(memory)

    expected_files: set[Path] = set()
    written = 0

    # Write person pages
    people_dir = vault / "People"
    people_dir.mkdir(parents=True, exist_ok=True)
    for name, facts in person_facts.items():
        content = _build_person_page(name, facts, graph, all_people)
        path = people_dir / f"{_safe_filename(name)}.md"
        _atomic_write(path, content)
        expected_files.add(path)
        written += 1

    # Write fact pages
    facts_dir = vault / "Facts"
    facts_dir.mkdir(parents=True, exist_ok=True)
    for memory in standalone_facts:
        text = memory.get("text", "").strip()
        if not text:
            continue
        content = _build_fact_page(memory, graph)
        path = facts_dir / f"{_safe_filename(text[:60])}.md"
        _atomic_write(path, content)
        expected_files.add(path)
        written += 1

    # Write timeline pages
    timeline_dir = vault / "Timeline"
    timeline_dir.mkdir(parents=True, exist_ok=True)
    for date_str, day_mems in by_date.items():
        content = _build_timeline_page(date_str, day_mems)
        path = timeline_dir / f"{_safe_filename(date_str)}.md"
        _atomic_write(path, content)
        expected_files.add(path)
        written += 1

    # Write index
    index_path = vault / "Index.md"
    _atomic_write(index_path, _build_index(memories, graph, len(person_facts), len(standalone_facts)))
    written += 1

    # Cleanup stale files
    _cleanup_stale_files(vault, expected_files)

    return {
        "status": "synced",
        "vault": str(vault),
        "people": len(person_facts),
        "facts": len(standalone_facts),
        "timeline_days": len(by_date),
        "files_written": written,
    }


def on_memory_stored(memory: dict, all_memories: list[dict], graph: dict):
    """Trigger point: called when KING stores a new memory.

    Runs the vault sync to keep Obsidian current.
    """
    return sync_vault(all_memories, graph)
