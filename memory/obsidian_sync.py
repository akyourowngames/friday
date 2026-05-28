import json
import os
import tempfile
from datetime import datetime
from pathlib import Path

from config import settings


def _project_path(path_text: str) -> Path:
    path = Path(str(path_text or "").strip())
    if not path.is_absolute():
        path = Path(__file__).resolve().parent.parent / path
    return path


def _safe_page_name(value: str, fallback: str) -> str:
    text = str(value or "").strip() or fallback
    parts = []
    last_was_sep = False
    for char in text:
        if char.isalnum() or char in ("-", "_"):
            parts.append(char)
            last_was_sep = False
        elif not last_was_sep:
            parts.append("_")
            last_was_sep = True
    return "".join(parts).strip("_") or fallback


def _safe_yaml(value) -> str:
    text = str(value or "")
    text = text.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{text}"'


def _clip(value: str, limit: int = 120) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)].rstrip() + "..."


def _safe_link_label(value: str) -> str:
    text = str(value or "").replace("[", " ").replace("]", " ").replace("|", " ")
    return " ".join(text.split())


def _atomic_write_text(path: Path, content: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_path = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
        os.replace(temp_path, path)
    finally:
        if os.path.exists(temp_path):
            os.unlink(temp_path)


def _atomic_write_json(path: Path, payload):
    _atomic_write_text(path, json.dumps(payload, indent=2, ensure_ascii=False))


def _inside(child: Path, parent: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def _rel_no_ext(path: Path, root: Path) -> str:
    text = str(path.relative_to(root))
    if text.endswith(".md"):
        text = text[:-3]
    return text.replace("\\", "/")


def _wiki(path: Path, vault_root: Path, label: str = "") -> str:
    target = _rel_no_ext(path, vault_root)
    clean_label = _safe_link_label(label)
    if clean_label:
        return f"[[{target}|{clean_label}]]"
    return f"[[{target}]]"


def _frontmatter(page_type: str, status: str, generated_at: str, extra: dict | None = None) -> list[str]:
    lines = [
        "---",
        f"type: {page_type}",
        f"status: {status}",
        f"generated: {generated_at}",
    ]
    for key, value in (extra or {}).items():
        lines.append(f"{key}: {_safe_yaml(value)}")
    lines.extend(["---", ""])
    return lines


def _edge_label(edge: dict) -> str:
    return str(edge.get("relation") or "related_to").replace("_", " ")


def _node_page_path(root: Path, node_id: str) -> Path:
    return root / "Nodes" / f"{_safe_page_name(node_id, 'node')}.md"


def _edge_page_path(root: Path, edge_id: str) -> Path:
    return root / "Edges" / f"{_safe_page_name(edge_id, 'edge')}.md"


def _memory_page_path(root: Path, memory_id: str) -> Path:
    return root / "Memories" / f"{_safe_page_name(memory_id, 'memory')}.md"


def _active_edges(graph: dict) -> list[dict]:
    return [edge for edge in graph.get("edges", []) if edge.get("active", True)]


def _memory_lookup(memories: list[dict]) -> dict:
    lookup = {}
    for item in memories:
        memory_id = str(item.get("id") or "").strip()
        if memory_id:
            lookup[memory_id] = item
    return lookup


def _sorted_edges(edges: list[dict]) -> list[dict]:
    return sorted(
        edges,
        key=lambda edge: (
            str(edge.get("source", "")),
            str(edge.get("relation", "")),
            str(edge.get("target", "")),
            str(edge.get("id", "")),
        ),
    )


def _build_index_page(root: Path, vault_root: Path, graph: dict, memories: list[dict], generated_at: str) -> str:
    active_edges = _active_edges(graph)
    node_ids = set()
    memory_ids = set()
    for edge in active_edges:
        if edge.get("source"):
            node_ids.add(edge.get("source"))
        if edge.get("target"):
            node_ids.add(edge.get("target"))
        if edge.get("memory_id"):
            memory_ids.add(edge.get("memory_id"))
    lines = _frontmatter("generated-memory-index", "active", generated_at, {"nodes": len(node_ids), "edges": len(active_edges), "memories": len(memory_ids)})
    lines.extend(
        [
            "# Generated Memory Graph",
            "",
            "This folder is regenerated from KING runtime graph memory. Edit runtime",
            "memory with `memory_remember` and `memory_forget`; do not hand-edit these",
            "generated pages.",
            "",
            "## Counts",
            "",
            f"- Active nodes: {len(node_ids)}",
            f"- Active edges: {len(active_edges)}",
            f"- Active memories: {len(memory_ids)}",
            "",
            "## Pages",
            "",
            f"- {_wiki(root / 'Nodes.md', vault_root, 'Nodes')}",
            f"- {_wiki(root / 'Edges.md', vault_root, 'Edges')}",
            f"- {_wiki(root / 'Memories.md', vault_root, 'Memories')}",
            f"- {_wiki(root / 'Removed Memory.md', vault_root, 'Removed Memory')}",
            f"- {_wiki(root / 'Schema.md', vault_root, 'Schema')}",
            "",
            "## Hub Nodes",
            "",
        ]
    )
    nodes = graph.get("nodes", {})
    for node_id in sorted(node_ids):
        node = nodes.get(node_id, {})
        lines.append(f"- {_wiki(_node_page_path(root, node_id), vault_root, node.get('name', node_id))}")
    if not node_ids:
        lines.append("- No active memory nodes yet.")
    lines.append("")
    return "\n".join(lines)


def _build_listing(root: Path, vault_root: Path, graph: dict, active_edges: list[dict], generated_at: str, kind: str) -> str:
    lines = _frontmatter("generated-memory-list", "active", generated_at, {"kind": kind})
    title = kind[:1].upper() + kind[1:]
    lines.extend([f"# {title}", ""])
    nodes = graph.get("nodes", {})
    if kind == "nodes":
        node_ids = set()
        for edge in active_edges:
            if edge.get("source"):
                node_ids.add(edge.get("source"))
            if edge.get("target"):
                node_ids.add(edge.get("target"))
        for node_id in sorted(node_ids):
            node = nodes.get(node_id, {})
            lines.append(f"- {_wiki(_node_page_path(root, node_id), vault_root, node.get('name', node_id))}")
        if not node_ids:
            lines.append("- No active nodes.")
    elif kind == "edges":
        for edge in _sorted_edges(active_edges):
            source = nodes.get(edge.get("source"), {}).get("name", edge.get("source", ""))
            target = nodes.get(edge.get("target"), {}).get("name", edge.get("target", ""))
            lines.append(f"- {_wiki(_edge_page_path(root, edge.get('id', 'edge')), vault_root, f'{source} {_edge_label(edge)} {target}'.strip())}")
        if not active_edges:
            lines.append("- No active edges.")
    lines.append("")
    return "\n".join(lines)


def _build_memories_listing(root: Path, vault_root: Path, graph: dict, memories: list[dict], generated_at: str) -> str:
    lines = _frontmatter("generated-memory-list", "active", generated_at, {"kind": "memories"})
    lines.extend(["# Memories", ""])
    lookup = _memory_lookup(memories)
    seen = set()
    for edge in _sorted_edges(_active_edges(graph)):
        memory_id = str(edge.get("memory_id") or "").strip()
        if not memory_id or memory_id in seen:
            continue
        seen.add(memory_id)
        item = lookup.get(memory_id, {})
        lines.append(f"- {_wiki(_memory_page_path(root, memory_id), vault_root, _clip(item.get('text', memory_id), 90))}")
    if not seen:
        lines.append("- No active memories.")
    lines.append("")
    return "\n".join(lines)


def _build_schema_page(generated_at: str) -> str:
    lines = _frontmatter("generated-memory-schema", "active", generated_at)
    lines.extend(
        [
            "# Schema",
            "",
            "Generated node pages represent active graph nodes.",
            "Generated edge pages represent active graph edges.",
            "Generated memory pages represent active memory entries linked to active edges.",
            "Removed memory is listed without active wiki links to keep the live graph clean.",
            "",
            "Runtime authority remains `memory/brain.py` and `memory_graph.json`.",
            "",
        ]
    )
    return "\n".join(lines)


def _build_node_page(root: Path, vault_root: Path, graph: dict, node_id: str, outgoing: list[dict], incoming: list[dict], generated_at: str) -> str:
    node = graph.get("nodes", {}).get(node_id, {})
    name = str(node.get("name") or node_id)
    lines = _frontmatter("generated-memory-node", "active", generated_at, {"node_id": node_id, "node_type": node.get("type", "concept"), "importance": node.get("importance", 0.5)})
    lines.extend(
        [
            f"# {name}",
            "",
            f"- Index: {_wiki(root / 'Index.md', vault_root, 'Generated Memory Graph')}",
            f"- Node id: `{node_id}`",
            f"- Type: `{node.get('type', 'concept')}`",
            "",
            "## Outgoing",
            "",
        ]
    )
    nodes = graph.get("nodes", {})
    if outgoing:
        for edge in _sorted_edges(outgoing):
            target = nodes.get(edge.get("target"), {}).get("name", edge.get("target", ""))
            lines.append(f"- {_edge_label(edge)} -> {_wiki(_node_page_path(root, edge.get('target', 'node')), vault_root, target)} via {_wiki(_edge_page_path(root, edge.get('id', 'edge')), vault_root, 'edge')}")
    else:
        lines.append("- No active outgoing edges.")
    lines.extend(["", "## Incoming", ""])
    if incoming:
        for edge in _sorted_edges(incoming):
            source = nodes.get(edge.get("source"), {}).get("name", edge.get("source", ""))
            lines.append(f"- {_wiki(_node_page_path(root, edge.get('source', 'node')), vault_root, source)} -> {_edge_label(edge)} via {_wiki(_edge_page_path(root, edge.get('id', 'edge')), vault_root, 'edge')}")
    else:
        lines.append("- No active incoming edges.")
    lines.append("")
    return "\n".join(lines)


def _build_edge_page(root: Path, vault_root: Path, graph: dict, edge: dict, memories: dict, generated_at: str) -> str:
    nodes = graph.get("nodes", {})
    source_id = edge.get("source", "")
    target_id = edge.get("target", "")
    source = nodes.get(source_id, {}).get("name", source_id)
    target = nodes.get(target_id, {}).get("name", target_id)
    memory_id = str(edge.get("memory_id") or "").strip()
    lines = _frontmatter("generated-memory-edge", "active", generated_at, {"edge_id": edge.get("id", ""), "relation": edge.get("relation", ""), "memory_id": memory_id})
    lines.extend(
        [
            f"# {source} {_edge_label(edge)} {target}",
            "",
            f"- Source: {_wiki(_node_page_path(root, source_id), vault_root, source)}",
            f"- Target: {_wiki(_node_page_path(root, target_id), vault_root, target)}",
            f"- Relation: `{edge.get('relation', '')}`",
            f"- Confidence: `{edge.get('confidence', '')}`",
            f"- Strength: `{edge.get('strength', '')}`",
        ]
    )
    if memory_id and memory_id in memories:
        lines.append(f"- Memory: {_wiki(_memory_page_path(root, memory_id), vault_root, _clip(memories[memory_id].get('text', memory_id), 80))}")
    evidence = str(edge.get("evidence") or "").strip()
    if evidence:
        lines.extend(["", "## Evidence", "", evidence])
    lines.append("")
    return "\n".join(lines)


def _build_memory_page(root: Path, vault_root: Path, graph: dict, item: dict, edges: list[dict], generated_at: str) -> str:
    memory_id = str(item.get("id") or "").strip()
    text = str(item.get("text") or "").strip()
    lines = _frontmatter("generated-memory", "active", generated_at, {"memory_id": memory_id, "date": item.get("_date", ""), "time": item.get("ts", ""), "importance": item.get("importance", 0.5)})
    lines.extend(
        [
            f"# {_clip(text, 80) or memory_id}",
            "",
            f"- Index: {_wiki(root / 'Index.md', vault_root, 'Generated Memory Graph')}",
            f"- Memory id: `{memory_id}`",
            f"- Date: `{item.get('_date', '')}`",
            f"- Time: `{item.get('ts', '')}`",
            f"- Importance: `{item.get('importance', 0.5)}`",
            "",
            "## Claim",
            "",
            text,
            "",
            "## Graph Edges",
            "",
        ]
    )
    nodes = graph.get("nodes", {})
    if edges:
        for edge in _sorted_edges(edges):
            source = nodes.get(edge.get("source"), {}).get("name", edge.get("source", ""))
            target = nodes.get(edge.get("target"), {}).get("name", edge.get("target", ""))
            lines.append(f"- {_wiki(_node_page_path(root, edge.get('source', 'node')), vault_root, source)} {_edge_label(edge)} {_wiki(_node_page_path(root, edge.get('target', 'node')), vault_root, target)} via {_wiki(_edge_page_path(root, edge.get('id', 'edge')), vault_root, 'edge')}")
    else:
        lines.append("- No active graph edges.")
    lines.append("")
    return "\n".join(lines)


def _build_removed_page(graph: dict, memories: list[dict], generated_at: str) -> str:
    active_memory_ids = {str(edge.get("memory_id") or "") for edge in _active_edges(graph)}
    memory_ids = {str(item.get("id") or "") for item in memories}
    inactive_edges = [edge for edge in graph.get("edges", []) if not edge.get("active", True)]
    lines = _frontmatter("generated-removed-memory", "active", generated_at)
    lines.extend(
        [
            "# Removed Memory",
            "",
            "Inactive or superseded edges are listed as plain text so they remain",
            "auditable without adding active wiki edges to Obsidian Graph view.",
            "",
        ]
    )
    nodes = graph.get("nodes", {})
    if inactive_edges:
        lines.extend(["## Inactive Edges", ""])
        for edge in _sorted_edges(inactive_edges):
            source = nodes.get(edge.get("source"), {}).get("name", edge.get("source", ""))
            target = nodes.get(edge.get("target"), {}).get("name", edge.get("target", ""))
            reason = edge.get("inactive_reason", "inactive")
            lines.append(f"- {source} {_edge_label(edge)} {target} | reason={reason} | edge={edge.get('id', '')}")
        lines.append("")
    removed_memory_ids = sorted(memory_id for memory_id in memory_ids if memory_id and memory_id not in active_memory_ids)
    if removed_memory_ids:
        lines.extend(["## Memories Without Active Edges", ""])
        lookup = _memory_lookup(memories)
        for memory_id in removed_memory_ids:
            lines.append(f"- {memory_id}: {_clip(lookup.get(memory_id, {}).get('text', ''), 140)}")
        lines.append("")
    if not inactive_edges and not removed_memory_ids:
        lines.extend(["No removed memory entries.", ""])
    return "\n".join(lines)


def sync_memory_graph(graph: dict, memories: list[dict]) -> dict:
    if not settings.memory_obsidian_sync_enabled:
        return {"enabled": False, "status": "skipped", "reason": "disabled"}

    vault_root = _project_path(settings.memory_obsidian_vault_dir)
    graph_dir = str(settings.memory_obsidian_graph_dir or "").strip().strip("/\\")
    if not graph_dir:
        return {"enabled": True, "status": "skipped", "reason": "empty_graph_dir"}
    root = vault_root / graph_dir
    if not _inside(root, vault_root):
        return {"enabled": True, "status": "skipped", "reason": "graph_dir_outside_vault"}

    generated_at = datetime.now().isoformat(timespec="seconds")
    active_edges = _active_edges(graph)
    memory_by_id = _memory_lookup(memories)
    pages = {
        root / "Index.md": _build_index_page(root, vault_root, graph, memories, generated_at),
        root / "Nodes.md": _build_listing(root, vault_root, graph, active_edges, generated_at, "nodes"),
        root / "Edges.md": _build_listing(root, vault_root, graph, active_edges, generated_at, "edges"),
        root / "Memories.md": _build_memories_listing(root, vault_root, graph, memories, generated_at),
        root / "Schema.md": _build_schema_page(generated_at),
        root / "Removed Memory.md": _build_removed_page(graph, memories, generated_at),
    }
    pages[root / "README.md"] = pages[root / "Index.md"]

    outgoing = {}
    incoming = {}
    memory_edges = {}
    node_ids = set()
    for edge in active_edges:
        source_id = edge.get("source", "")
        target_id = edge.get("target", "")
        if source_id:
            node_ids.add(source_id)
            outgoing.setdefault(source_id, []).append(edge)
        if target_id:
            node_ids.add(target_id)
            incoming.setdefault(target_id, []).append(edge)
        memory_id = str(edge.get("memory_id") or "").strip()
        if memory_id:
            memory_edges.setdefault(memory_id, []).append(edge)
        edge_id = str(edge.get("id") or "").strip()
        if edge_id:
            pages[_edge_page_path(root, edge_id)] = _build_edge_page(root, vault_root, graph, edge, memory_by_id, generated_at)

    for node_id in sorted(node_ids):
        pages[_node_page_path(root, node_id)] = _build_node_page(root, vault_root, graph, node_id, outgoing.get(node_id, []), incoming.get(node_id, []), generated_at)

    for memory_id, edges in memory_edges.items():
        item = memory_by_id.get(memory_id)
        if item:
            pages[_memory_page_path(root, memory_id)] = _build_memory_page(root, vault_root, graph, item, edges, generated_at)

    manifest_path = root / ".sync_manifest.json"
    previous_files = []
    if manifest_path.exists():
        try:
            previous = json.loads(manifest_path.read_text(encoding="utf-8"))
            if isinstance(previous, dict) and isinstance(previous.get("files"), list):
                previous_files = [str(item) for item in previous.get("files", [])]
        except (OSError, json.JSONDecodeError):
            previous_files = []

    current_rel = sorted(_rel_no_ext(path, root) + ".md" for path in pages)
    current_set = set(current_rel)
    for rel_text in previous_files:
        if rel_text in current_set:
            continue
        stale_path = root / rel_text
        if stale_path.suffix == ".md" and _inside(stale_path, root) and stale_path.exists():
            stale_path.unlink()

    for path, content in pages.items():
        _atomic_write_text(path, content)

    _atomic_write_json(
        manifest_path,
        {
            "generated_at": generated_at,
            "vault_root": str(vault_root),
            "generated_root": str(root),
            "files": current_rel,
            "node_count": len(node_ids),
            "edge_count": len(active_edges),
            "memory_count": len(memory_edges),
        },
    )
    return {
        "enabled": True,
        "status": "synced",
        "vault_root": str(vault_root),
        "generated_root": str(root),
        "node_count": len(node_ids),
        "edge_count": len(active_edges),
        "memory_count": len(memory_edges),
        "file_count": len(pages),
    }
