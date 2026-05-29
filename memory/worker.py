"""Memory Worker — Obsidian memory vault builder.

Triggered when KING stores a new memory. Builds human-readable Obsidian notes
from raw memory facts and graph edges. Optional LLM enrichment is config-gated
so the default vault stays grounded in stored evidence.

Vault structure:
    People/         Person profiles built from graph facts and relationships
    Facts/          Standalone facts with connections and context
    Timeline/       Date-grouped memory entries for chronological browsing
    Index.md        Dashboard with people, stats, and recent memories
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime
from pathlib import Path

from config import settings

_PERSON_SYSTEM_PROMPT = """You are a memory organizer for a personal AI assistant called KING.
Your job is to write a clean, detailed Obsidian markdown note about a person based on raw memory facts.

Rules:
- Write in third person about the person (e.g. "Krish is..." not "You are...")
- If the person is "User", write about the user of KING (the owner). Use their real name if known.
- Organize facts into clear sections with ## headings
- Use bullet points for lists of facts
- Add brief contextual notes where relationships between facts are obvious
- Use [[People/Name|Name]] when referencing other people
- Use [[Timeline/DATE|DATE]] when referencing dates (format: YYYY-MM-DD)
- Include a Timeline section at the bottom linking to dates when facts were stored
- Include a "Last updated" line at the bottom
- Keep it concise but complete — every fact should appear somewhere
- Do NOT invent facts. Only use what is provided.
- When Other known people is not "none", reference only supported relationships and never say no other people are known.
- Do NOT add disclaimers or meta-commentary about the note itself.
- Use Obsidian frontmatter (---) with type, name, and updated fields.
- End with a link back to [[Index]]

Output ONLY the markdown note content. No explanation before or after."""

_FACT_SYSTEM_PROMPT = """You are a memory organizer for a personal AI assistant called KING.
Your job is to write a clean Obsidian markdown note for a standalone memory fact.

Rules:
- Write a brief, clear note about this fact
- Add context if the connections make the meaning clearer
- Use [[People/Name|Name]] wiki links when referencing people
- Include Obsidian frontmatter with type, date, and importance
- Keep it short — 3-8 lines max for the body
- Do NOT invent information beyond what is given
- Do NOT add meta-commentary

Output ONLY the markdown note content."""


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
    name = _safe_filename(text)
    if folder:
        return f"[[{folder}/{name}|{name}]]"
    return f"[[{name}]]"


def _strip_markdown_fence(content: str) -> str:
    lines = str(content or "").strip().splitlines()
    if len(lines) >= 2 and lines[0].strip().startswith("```") and lines[-1].strip() == "```":
        return "\n".join(lines[1:-1]).strip()
    return str(content or "").strip()


def _llm_call(system: str, user_content: str, max_tokens: int = 800) -> str | None:
    """Make a one-shot LLM call. Returns None on failure (graceful degradation).
    Skips entirely if no API key is configured or vault is in a temp directory."""
    if not settings.memory_obsidian_llm_pages_enabled:
        return None
    if not settings.nim_api_key or not settings.nim_api_key.strip():
        return None
    # Skip LLM in test environments (temp vault paths)
    vault_path = str(settings.memory_obsidian_vault_dir)
    if "tmp" in vault_path.lower() or "temp" in vault_path.lower():
        return None
    try:
        from openai import OpenAI

        client = OpenAI(
            base_url=settings.nim_base_url,
            api_key=settings.nim_api_key,
            timeout=12,
            max_retries=0,
        )
        resp = client.chat.completions.create(
            model=settings.model_name,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user_content},
            ],
            temperature=0.2,
            max_tokens=max_tokens,
        )
        return _strip_markdown_fence(resp.choices[0].message.content)
    except Exception:
        return None


# ─── Page builders ──────────────────────────────────────────────────────────


def _build_person_page_llm(name: str, facts: list[dict], graph: dict, all_people: set) -> str:
    """Use the LLM to write a rich person page from raw facts."""
    # Build the fact list for the LLM
    fact_lines = []
    seen = set()
    for fact in facts:
        text = fact.get("text", "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        date = fact.get("_date", "")
        edges = fact.get("_resolved_edges", [])
        relations = [f"{e.get('source','')} --{e.get('relation','')}-> {e.get('target','')}" for e in edges]
        line = f"- {text}"
        if date:
            line += f" (stored: {date})"
        if relations:
            line += f" [graph: {'; '.join(relations)}]"
        fact_lines.append(line)

    other_people = sorted(all_people - {name})
    user_content = f"""Person: {name}
Other known people: {', '.join(other_people) if other_people else 'none'}
Total facts: {len(fact_lines)}

Raw facts:
{chr(10).join(fact_lines)}"""

    result = _llm_call(_PERSON_SYSTEM_PROMPT, user_content, max_tokens=1000)
    if result:
        return result
    # Fallback: simple structured page without LLM
    return _build_person_page_fallback(name, facts, graph)


def _build_person_page_fallback(name: str, facts: list[dict], graph: dict) -> str:
    """Fallback person page when LLM is unavailable."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    nodes = graph.get("nodes", {})
    lines = [
        "---",
        f"type: person",
        f"name: \"{name}\"",
        f"updated: \"{now}\"",
        "---",
        "",
        f"# {name}",
        "",
        "## Facts",
        "",
    ]
    seen = set()
    dates = set()
    relationship_lines = []
    relationship_seen = set()
    for fact in facts:
        text = fact.get("text", "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        date = fact.get("_date", "")
        if date:
            dates.add(date)
        entry = f"- {text}"
        if date:
            entry += f" *({date})*"
        lines.append(entry)
        for edge in fact.get("_resolved_edges", []):
            source = nodes.get(edge.get("source", ""), {})
            target = nodes.get(edge.get("target", ""), {})
            source_name = source.get("name", "")
            target_name = target.get("name", "")
            relation = str(edge.get("relation", "related_to")).replace("_", " ").strip() or "related to"
            if source_name == name and target_name:
                other = _wiki_link(target_name, "People") if target.get("type") == "person" else target_name
                relation_line = f"- {relation}: {other}"
            elif target_name == name and source_name:
                other = _wiki_link(source_name, "People") if source.get("type") == "person" else source_name
                relation_line = f"- {other}: {relation}"
            else:
                continue
            if relation_line not in relationship_seen:
                relationship_seen.add(relation_line)
                relationship_lines.append(relation_line)

    if relationship_lines:
        lines.extend(["", "## Relationships", ""])
        lines.extend(relationship_lines)

    if dates:
        lines.extend(["", "## Timeline", ""])
        for d in sorted(dates, reverse=True):
            lines.append(f"- [[Timeline/{_safe_filename(d)}|{d}]]")

    lines.extend(["", "---", "[[Index]]", ""])
    return "\n".join(lines)


def _build_fact_page_llm(memory: dict, graph: dict) -> str:
    """Use the LLM to write a fact page."""
    text = memory.get("text", "")
    date = memory.get("_date", "")
    importance = memory.get("importance", 0.5)
    edges = memory.get("_resolved_edges", [])
    nodes = graph.get("nodes", {})

    connections = []
    for edge in edges:
        source_name = nodes.get(edge.get("source", ""), {}).get("name", edge.get("source", ""))
        target_name = nodes.get(edge.get("target", ""), {}).get("name", edge.get("target", ""))
        relation = edge.get("relation", "related_to")
        connections.append(f"{source_name} --{relation}-> {target_name}")

    user_content = f"""Fact: {text}
Date stored: {date}
Importance: {importance}
Graph connections: {'; '.join(connections) if connections else 'none'}"""

    result = _llm_call(_FACT_SYSTEM_PROMPT, user_content, max_tokens=400)
    if result:
        return result
    # Fallback
    return _build_fact_page_fallback(memory, graph)


def _build_fact_page_fallback(memory: dict, graph: dict) -> str:
    """Fallback fact page when LLM is unavailable."""
    text = memory.get("text", "")
    date = memory.get("_date", "")
    importance = memory.get("importance", 0.5)
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = [
        "---",
        f"type: fact",
        f"date: \"{date}\"",
        f"importance: {importance}",
        f"updated: \"{now}\"",
        "---",
        "",
        f"# {text}",
        "",
        f"Stored: {date}",
        "",
        "---",
        "[[Index]]",
        "",
    ]
    return "\n".join(lines)


def _build_timeline_page(date_str: str, day_memories: list[dict], graph: dict) -> str:
    """Build a daily timeline page with cross-links to people."""
    nodes = graph.get("nodes", {})
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
    people_mentioned = set()
    sorted_mems = sorted(day_memories, key=lambda m: m.get("ts", ""))
    for m in sorted_mems:
        text = m.get("text", "")
        time_str = m.get("ts", "")
        prefix = f"**{time_str}** " if time_str else ""
        lines.append(f"- {prefix}{text}")
        # Collect people for cross-links
        for node_id in m.get("graph_nodes", []):
            node = nodes.get(node_id, {})
            if node.get("type") == "person" and node.get("name"):
                people_mentioned.add(node["name"])

    if people_mentioned:
        lines.extend(["", "## People mentioned", ""])
        for name in sorted(people_mentioned):
            lines.append(f"- [[People/{_safe_filename(name)}|{name}]]")

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

    if people:
        lines.extend(["## People", ""])
        for name in people:
            lines.append(f"- {_wiki_link(name, 'People')}")
        lines.append("")

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

    dates = sorted({m.get("_date", "") for m in memories if m.get("_date")}, reverse=True)
    if dates:
        lines.extend(["## Timeline", ""])
        for d in dates[:14]:
            count = sum(1 for m in memories if m.get("_date") == d)
            lines.append(f"- {_wiki_link(d, 'Timeline')} ({count} memories)")
        lines.append("")

    lines.extend([
        "## Stats",
        "",
        "| Metric | Value |",
        "|--------|-------|",
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
    """Rebuild the Obsidian memory vault using the LLM for rich page content.

    Idempotent: same input produces same output. Stale files are cleaned up.
    Falls back to structured templates when the LLM is unavailable.
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

    all_people = {
        node.get("name") for node in nodes.values()
        if node.get("type") == "person" and node.get("name")
    }

    # Group memories
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

    # Write person pages (LLM-enriched)
    people_dir = vault / "People"
    people_dir.mkdir(parents=True, exist_ok=True)
    for name, facts in person_facts.items():
        content = _build_person_page_llm(name, facts, graph, all_people)
        path = people_dir / f"{_safe_filename(name)}.md"
        _atomic_write(path, content)
        expected_files.add(path)
        written += 1

    # Write fact pages (LLM-enriched)
    facts_dir = vault / "Facts"
    facts_dir.mkdir(parents=True, exist_ok=True)
    for memory in standalone_facts:
        text = memory.get("text", "").strip()
        if not text:
            continue
        content = _build_fact_page_llm(memory, graph)
        path = facts_dir / f"{_safe_filename(text[:60])}.md"
        _atomic_write(path, content)
        expected_files.add(path)
        written += 1

    # Write timeline pages (no LLM needed — just chronological listing)
    timeline_dir = vault / "Timeline"
    timeline_dir.mkdir(parents=True, exist_ok=True)
    for date_str, day_mems in by_date.items():
        content = _build_timeline_page(date_str, day_mems, graph)
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


# ─── User file ingestion ────────────────────────────────────────────────────

_MANAGED_DIRS = {"People", "Facts", "Timeline", ".obsidian"}
_MANAGED_FILES = {"Index.md"}


def _is_user_file(path: Path, vault: Path) -> bool:
    """Check if a file is user-created (not managed by the worker)."""
    if not path.suffix.lower() == ".md":
        return False
    relative = path.relative_to(vault)
    parts = relative.parts
    if not parts:
        return False
    # Files in managed directories are ours
    if parts[0] in _MANAGED_DIRS:
        return False
    # Index.md is ours
    if len(parts) == 1 and parts[0] in _MANAGED_FILES:
        return False
    return True


def scan_user_files(vault: Path | None = None) -> list[dict]:
    """Scan the vault root for user-created markdown files.

    Returns a list of {path, filename, content} dicts for files the user
    dropped into the vault that aren't managed by the worker.
    """
    vault = vault or _memory_vault()
    if not vault.exists():
        return []
    user_files = []
    for md_file in vault.rglob("*.md"):
        if _is_user_file(md_file, vault):
            try:
                content = md_file.read_text(encoding="utf-8").strip()
                if content:
                    user_files.append({
                        "path": str(md_file),
                        "filename": md_file.stem,
                        "content": content,
                    })
            except (OSError, UnicodeDecodeError):
                continue
    return user_files


_INGEST_SYSTEM_PROMPT = """You are a memory extraction agent for a personal AI assistant called KING.
The user has placed a markdown file in their memory vault with personal information.
Extract concrete personal facts from this file that KING should remember.

Rules:
- Return ONLY a JSON array of fact strings
- Each fact should be a single, specific, rememberable statement
- Focus on: names, handles, accounts, preferences, relationships, locations, dates, goals
- Do NOT extract vague or generic statements
- Do NOT extract formatting instructions or meta-content
- If the file has social media handles, extract each as "User's {platform} is {handle}"
- If the file has personal details, extract each as a clear fact

Example output: ["User's Twitter is @krish_dev", "User's Discord is krish#1234", "User's favorite color is blue"]

Return [] if nothing worth extracting."""


def ingest_user_files(brain=None) -> dict:
    """Read user-created files from the vault and store extracted facts as memories.

    This is the "user drops a file" flow: they create `socials.md` with their
    handles, the worker reads it, extracts facts via LLM, and stores them
    through the brain so they become part of KING's memory.

    Can be called manually or triggered by a file watcher on the vault directory.
    """
    vault = _memory_vault()
    user_files = scan_user_files(vault)
    if not user_files:
        return {"status": "no_user_files", "ingested": 0}

    if brain is None:
        from memory.brain import Brain
        brain = Brain()

    total_ingested = 0
    for file_info in user_files:
        content = file_info["content"]
        filename = file_info["filename"]

        # Use LLM to extract facts from the user file
        user_content = f"File: {filename}.md\n\nContent:\n{content[:2000]}"
        result = _llm_call(_INGEST_SYSTEM_PROMPT, user_content, max_tokens=500)

        if not result:
            continue

        try:
            facts = json.loads(result)
            if not isinstance(facts, list):
                continue
        except (json.JSONDecodeError, TypeError):
            continue

        for fact in facts:
            if isinstance(fact, str) and fact.strip():
                stored = brain.commit(fact.strip(), importance=0.7)
                if stored:
                    total_ingested += 1

    return {
        "status": "ingested",
        "user_files_found": len(user_files),
        "facts_ingested": total_ingested,
    }

