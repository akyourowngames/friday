"""Capability rule aggregation for the router.

A "capability rule" maps a natural-language capability phrase to a backing tool,
plus optional argument hints to pre-fill (for example the exact Composio
tool_slug). The router embeds each phrase as its own retrieval probe so a single
multi-capability gateway tool competes per-capability instead of being averaged
into one weak embedding.

Two sources, merged:

1. Static rules from `tools/CAPABILITY_ROUTING.md` (hand-written overrides).
2. Provider rules auto-generated from each multi-capability tool's own config
   surface (e.g. the Composio gateway's enabled-tool notes). Providers mean no
   hand-maintenance: adding a Composio slug to the gateway markdown automatically
   gives it a routing probe and a resolved slug.

This is semantic routing (embeddings), never a keyword table. Phrases are matched
by embedding similarity downstream, not by substring.
"""

from __future__ import annotations

from pathlib import Path

from config import settings


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _resolve(path_str: str) -> Path:
    path = Path(path_str)
    if not path.is_absolute():
        path = _repo_root() / path
    return path


def _slug_grounding_text(slug: str, toolkit: str = "") -> str:
    pieces = []
    if toolkit:
        pieces.append(str(toolkit).strip().lower())
    if slug:
        pieces.append(str(slug).replace("_", " ").lower())
    return " ".join(piece for piece in pieces if piece).strip()


def _static_rules() -> list[dict]:
    path = _resolve(settings.capability_routing_file)
    if not path.exists():
        return []
    rules: list[dict] = []
    in_section = False
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if line.startswith("## "):
            in_section = line[3:].strip().casefold() == "capabilities"
            continue
        if not in_section or not line.startswith("- ") or "=>" not in line:
            continue
        phrase, _, target = line[2:].partition("=>")
        phrase = phrase.strip()
        target = target.strip()
        if not phrase or not target:
            continue
        # target is "tool" or "tool | tool_slug: SLUG" for gateway pre-fill.
        tool_part, _, slug_part = target.partition("|")
        tool_name = tool_part.strip()
        args: dict = {}
        grounding = ""
        if slug_part.strip():
            key, sep, value = slug_part.partition(":")
            if sep and key.strip().lower() == "tool_slug" and value.strip():
                slug = value.strip().upper()
                args = {"action": "execute", "tool_slug": slug}
                grounding = _slug_grounding_text(slug)
        if tool_name:
            rule = {"phrase": phrase, "tool": tool_name, "args": args}
            if grounding:
                rule["grounding"] = grounding
            rules.append(rule)
    return rules


def _parse_gateway_tool_line(body: str) -> dict | None:
    """Parse one `## Enabled Tools` line from the Composio gateway markdown.

    Format: `SLUG | toolkit: x | risk: y | enabled: z | note: text`
    Returns {slug, toolkit, note, enabled} or None.
    """
    pieces = [piece.strip() for piece in body.split("|")]
    if not pieces or not pieces[0]:
        return None
    slug = pieces[0].strip().upper()
    fields = {"toolkit": "", "note": "", "enabled": "true"}
    for piece in pieces[1:]:
        key, sep, value = piece.partition(":")
        if sep:
            key = key.strip().lower()
            if key in fields:
                fields[key] = value.strip()
    if not slug:
        return None
    return {
        "slug": slug,
        "toolkit": fields["toolkit"].lower(),
        "note": fields["note"],
        "enabled": fields["enabled"].lower() in ("1", "true", "yes", "on"),
    }


def _composio_provider_rules() -> list[dict]:
    """Auto-generate capability rules from the Composio gateway enabled tools.

    Each enabled tool's note becomes a capability phrase resolving to the
    `composio` tool with the exact slug carried in args, so execution does not
    rely on the model guessing the slug.
    """
    try:
        from tools.registry import get_tool

        if get_tool("composio") is None:
            return []
    except Exception:
        return []

    gateway_path = _resolve(settings.composio_policy_file)
    if not gateway_path.exists():
        return []

    rules: list[dict] = []
    section = ""
    for raw_line in gateway_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if line.startswith("## "):
            section = line[3:].strip().casefold()
            continue
        if section != "enabled tools" or not line.startswith("- "):
            continue
        parsed = _parse_gateway_tool_line(line[2:].strip())
        if not parsed or not parsed["enabled"]:
            continue
        note = parsed["note"]
        toolkit = parsed["toolkit"]
        # Build a clean capability phrase from the note plus the toolkit name so
        # "list repository issues" also carries its app context ("github").
        phrase = note or parsed["slug"].replace("_", " ").lower()
        if toolkit and toolkit not in phrase.lower():
            phrase = f"{toolkit}: {phrase}"
        rules.append(
            {
                "phrase": phrase,
                "tool": "composio",
                "args": {"action": "execute", "tool_slug": parsed["slug"]},
                "grounding": _slug_grounding_text(parsed["slug"], toolkit),
                "toolkit": toolkit,
            }
        )
    return rules


# Provider registry. Each provider returns a list of capability rules built from
# a tool's own config. Add new multi-capability tools here.
_PROVIDERS = (_composio_provider_rules,)


def build_capability_rules() -> list[dict]:
    """Merge static and provider-generated capability rules, deduped by phrase."""
    rules: list[dict] = []
    seen: set[str] = set()

    def _add(rule: dict):
        phrase = str(rule.get("phrase") or "").strip()
        tool = str(rule.get("tool") or "").strip()
        if not phrase or not tool:
            return
        key = f"{tool}::{phrase.casefold()}"
        if key in seen:
            return
        seen.add(key)
        merged = {"phrase": phrase, "tool": tool, "args": dict(rule.get("args") or {})}
        grounding = str(rule.get("grounding") or "").strip()
        toolkit = str(rule.get("toolkit") or "").strip()
        if grounding:
            merged["grounding"] = grounding
        if toolkit:
            merged["toolkit"] = toolkit
        rules.append(merged)

    for rule in _static_rules():
        _add(rule)
    for provider in _PROVIDERS:
        try:
            for rule in provider():
                _add(rule)
        except Exception:
            continue
    return rules
