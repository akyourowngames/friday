"""Session digest: LLM-powered extraction from full session transcripts.

Reads a complete session transcript and extracts structured knowledge that
gets committed to the memory graph — topics discussed, goals stated, decisions
made, problems encountered, ideas proposed, events mentioned, and entities
(people, projects, tools). This is the "god-tier" layer that makes sessions
survive as durable memory, not just day-based facts.

No regex. No hardcoded keyword routing. The LLM reads the transcript and
decides what matters.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime
from pathlib import Path

from config import settings

_DIGEST_SYSTEM_PROMPT = """You are a session memory extractor for a personal AI assistant called KING.
Your job is to read a full conversation transcript and extract structured knowledge worth remembering long-term.

Extract these categories:
- topics: What subjects or themes were discussed (e.g. "web scraping project", "exam preparation")
- goals: What the user wants to achieve, is planning, or has committed to (e.g. "finish landing page by Friday")
- decisions: What was decided or concluded (e.g. "using FastAPI for the backend", "cutting the mobile version")
- problems: What obstacles, blockers, or issues were raised (e.g. "API key expired", "can't connect to database")
- ideas: What was proposed or brainstormed but not yet acted on (e.g. "maybe add dark mode", "could use Redis for caching")
- events: Concrete things that happened or were reported (e.g. "deployed the server", "exam is next Monday")
- entities: Named people, projects, tools, or places mentioned (with their type: person, project, tool, place)

Rules:
- Return ONLY a valid JSON object with the keys above, each mapping to a list of strings.
- Each item should be a single, specific, self-contained statement.
- Do NOT extract: pleasantries, greetings, meta-commentary about the assistant, general advice, or common knowledge.
- Do NOT extract vague or generic statements — each item must be grounded in something specific from the transcript.
- Use the user's own words as the source of truth. Assistant text is supporting context.
- If a category has nothing worth extracting, use an empty list for that key.
- Keep each item concise (under 120 chars) but specific enough to be useful months later.
- For entities, use the format "name (type)" e.g. "FastAPI (tool)", "Landing Page (project)", "Krish (person)".

Example output:
{
  "topics": ["budget tracker app design", "API authentication approach"],
  "goals": ["ship the landing page by end of month", "pass the physics exam"],
  "decisions": ["using PostgreSQL instead of MongoDB", "deploying on Railway"],
  "problems": ["database migration failed on staging", "can't find the API key"],
  "ideas": ["could add a dark mode toggle later", "maybe use Redis for session caching"],
  "events": ["deployed v1.2 to production", "exam is scheduled for next Monday"],
  "entities": ["FastAPI (tool)", "Railway (tool)", "Krish (person)", "Landing Page (project)"]
}

Return {} if nothing worth remembering."""


def _llm_call(system: str, user_content: str, max_tokens: int = 900) -> str | None:
    """One-shot LLM call. Returns None on failure (graceful degradation)."""
    if not settings.nim_api_key or not settings.nim_api_key.strip():
        return None
    try:
        from openai import OpenAI

        client = OpenAI(
            base_url=settings.nim_base_url,
            api_key=settings.nim_api_key,
            timeout=30,
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
        return resp.choices[0].message.content.strip()
    except Exception:
        return None


def _format_transcript(turns: list[dict], max_chars: int = 8000) -> str:
    """Format transcript turns into a readable block for the LLM."""
    lines = []
    total = 0
    for turn in turns:
        role = str(turn.get("role", "")).strip()
        content = str(turn.get("content", "")).strip()
        if not role or not content:
            continue
        block = f"{role}: {content}"
        if total + len(block) > max_chars:
            remaining = max_chars - total
            if remaining >= 10:
                lines.append(block[:remaining] + "...")
            break
        lines.append(block)
        total += len(block)
    return "\n".join(lines)


def digest_session(turns: list[dict]) -> dict:
    """Extract structured knowledge from a session transcript.

    Returns a dict with keys: topics, goals, decisions, problems, ideas,
    events, entities — each mapping to a list of strings. Returns empty
    dict on failure or when nothing worth extracting is found.
    """
    if not turns:
        return {}

    user_turns = [t for t in turns if t.get("role") == "user"]
    if len(user_turns) < 1:
        return {}

    transcript = _format_transcript(turns)
    if not transcript.strip():
        return {}

    result_text = _llm_call(
        _DIGEST_SYSTEM_PROMPT,
        f"Session transcript:\n\n{transcript}",
        max_tokens=settings.session_digest_max_tokens,
    )
    if not result_text:
        return {}

    # Strip markdown fences if present
    stripped = result_text.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        if len(lines) >= 2 and lines[0].strip().startswith("```") and lines[-1].strip() == "```":
            stripped = "\n".join(lines[1:-1]).strip()

    try:
        parsed = json.loads(stripped)
    except (json.JSONDecodeError, TypeError):
        return {}

    if not isinstance(parsed, dict):
        return {}

    # Normalize: ensure all values are lists of strings, non-empty only
    categories = ["topics", "goals", "decisions", "problems", "ideas", "events", "entities"]
    digest = {}
    for cat in categories:
        items = parsed.get(cat)
        if not isinstance(items, list):
            continue
        clean = []
        for item in items:
            text = str(item).strip()
            if text and len(text) >= 5:
                clean.append(text)
        if clean:
            digest[cat] = clean

    return digest


def digest_to_facts(digest: dict) -> list[str]:
    """Convert a session digest into memory-fact strings for brain.commit().

    Each fact is prefixed with a category label so the graph can route it
    to the right relation type via MEMORY_SESSION_RELATIONS.md.
    """
    if not digest:
        return []

    facts = []
    for category in ("topics", "goals", "decisions", "problems", "ideas", "events"):
        for item in (digest.get(category) or []):
            text = str(item).strip()
            if text:
                facts.append(text)

    # Entities get stored as named references
    for item in (digest.get("entities") or []):
        text = str(item).strip()
        if text:
            facts.append(f"Entity mentioned: {text}")

    return facts


def _save_digest(digest: dict, session_id: str, digest_dir: Path | None = None) -> Path:
    """Persist a session digest to disk for later reference."""
    target = digest_dir or Path(settings.session_store_dir).resolve()
    target.mkdir(parents=True, exist_ok=True)
    path = target / f"{session_id}_digest.json"
    fd, temp = tempfile.mkstemp(dir=target, prefix=f".{session_id}_digest.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(digest, handle, indent=2, ensure_ascii=False)
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)
    return path


def process_session(session_store, session_id: str, brain=None) -> dict:
    """Full pipeline: read session transcript, digest, commit facts, mark done.

    Returns a summary of what was extracted and stored.
    """
    turns = session_store.read_session(session_id)
    if not turns:
        return {"session_id": session_id, "status": "empty", "facts_stored": 0}

    user_turns = [t for t in turns if t.get("role") == "user"]
    if len(user_turns) < settings.session_digest_min_turns:
        return {"session_id": session_id, "status": "skipped", "reason": "too_few_turns", "facts_stored": 0}

    digest = digest_session(turns)
    if not digest:
        session_store.mark_digested(session_id)
        return {"session_id": session_id, "status": "empty_digest", "facts_stored": 0}

    facts = digest_to_facts(digest)
    stored = 0
    if brain is not None:
        for fact in facts:
            if brain.commit(fact, importance=0.6):
                stored += 1

    _save_digest(digest, session_id)
    session_store.mark_digested(session_id)

    return {
        "session_id": session_id,
        "status": "digested",
        "turns_read": len(turns),
        "categories": {k: len(v) for k, v in digest.items()},
        "facts_extracted": len(facts),
        "facts_stored": stored,
    }


def process_undigested(session_store, brain=None) -> list[dict]:
    """Digest all undigested sessions (e.g. on nightly maintenance or session start).

    Returns a list of per-session results.
    """
    undigested = session_store.undigested_sessions(exclude_current=True)
    results = []
    for entry in undigested:
        sid = str(entry.get("id"))
        if not sid:
            continue
        result = process_session(session_store, sid, brain=brain)
        results.append(result)
    return results
