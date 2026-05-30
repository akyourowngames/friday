"""Memory-driven scheduler bridge.

Closes the loop the other direction from the existing scheduler->memory writes:
this scans recent memory for time-bound intentions the user mentioned ("call the
dentist Friday", "demo is due next week", "renew the domain on the 30th") and
schedules a gentle reminder for each, so commitments buried in conversation
actually resurface at the right time.

Extraction is one LLM call that reads the facts and returns JSON with a concrete
date — no keyword routing, no regex date parsing. Each nudge is de-duplicated
against existing pending reminders by its task text, so running this nightly
never piles up duplicates. Runs only in the nightly maintenance pass.
"""

from __future__ import annotations

import json
from datetime import date, datetime, time, timedelta

from config import settings

_BRIDGE_TAG = "memory_nudge"

_EXTRACT_SYSTEM_PROMPT = (
    "You read personal memory facts and today's date, and extract TIME-BOUND commitments: "
    "things the user intends to do that have a specific day or deadline, and that sound "
    "unfinished. Return ONLY a JSON array (no prose, no code fence). Each item:\n"
    "{\n"
    '  "task": short reminder text in the imperative (e.g. "Call the dentist"),\n'
    '  "date": the concrete calendar date it should fire, as YYYY-MM-DD\n'
    "}\n"
    "Rules:\n"
    "- Resolve relative dates against the provided today's date (e.g. 'Friday', 'next week', "
    "'the 30th') into an absolute YYYY-MM-DD. If a fact has no clear date, skip it.\n"
    "- Only include genuine future-facing commitments. Skip completed actions, stable facts, "
    "and vague wishes. Judge by meaning, not by matching words.\n"
    "- Keep each task under 12 words. Return [] if nothing qualifies. Return strictly valid JSON."
)


def _recent_facts(memories: list[dict], recent_days: int, lookback: int, today: date) -> list[str]:
    cutoff = today - timedelta(days=recent_days)
    facts: list[str] = []
    for memory in memories:
        date_str = str(memory.get("_date", "")).strip()
        if date_str:
            try:
                if date.fromisoformat(date_str) < cutoff:
                    continue
            except ValueError:
                pass
        text = str(memory.get("text", "")).strip()
        if text:
            facts.append(text)
    return facts[-lookback:]


def _extract_commitments(facts: list[str], today: date, max_tokens: int, llm_client=None) -> list[dict]:
    if not facts:
        return []
    if llm_client is None:
        try:
            from agent.llm import NIMClient

            llm_client = NIMClient()
        except Exception:
            return []
    try:
        response = llm_client.client.chat.completions.create(
            model=settings.model_name,
            messages=[
                {"role": "system", "content": _EXTRACT_SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": f"Today's date: {today.isoformat()}\nMemory facts:\n- " + "\n- ".join(facts),
                },
            ],
            temperature=0,
            max_tokens=max_tokens,
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

    commitments: list[dict] = []
    for entry in parsed:
        if not isinstance(entry, dict):
            continue
        task = str(entry.get("task") or "").strip()
        date_str = str(entry.get("date") or "").strip()
        if not task or not date_str:
            continue
        try:
            when = date.fromisoformat(date_str)
        except ValueError:
            continue
        if when < today:
            continue
        commitments.append({"task": task, "date": when})
    return commitments


def _existing_nudge_tasks(scheduler) -> set[str]:
    """Tasks already scheduled by this bridge and still pending, for dedup."""
    existing: set[str] = set()
    try:
        for item in scheduler.list_items(status="pending"):
            if _BRIDGE_TAG not in (item.get("tags") or []):
                continue
            task = str((item.get("arguments") or {}).get("task", "")).strip().casefold()
            if task:
                existing.add(task)
    except Exception:
        pass
    return existing


def run_bridge(brain=None, now: datetime | None = None, llm_client=None) -> dict:
    """Scan recent memory for time-bound commitments and schedule reminders.

    De-duplicates against existing pending memory-nudge reminders so repeated
    nightly runs are idempotent. Degrades to a no-op on any failure or when the
    LLM is unavailable. Returns an evidence dict.
    """
    if not settings.memory_scheduler_bridge_enabled:
        return {"status": "disabled"}
    now = now or datetime.now()
    today = now.date()

    if brain is None:
        try:
            from memory.brain import Brain

            brain = Brain()
        except Exception:
            return {"status": "no_brain", "scheduled": 0}

    memories = list(getattr(brain, "memories", []) or [])
    facts = _recent_facts(
        memories,
        recent_days=int(settings.memory_scheduler_bridge_recent_days),
        lookback=int(settings.memory_scheduler_bridge_lookback),
        today=today,
    )
    commitments = _extract_commitments(
        facts,
        today,
        max_tokens=int(settings.memory_scheduler_bridge_max_tokens),
        llm_client=llm_client,
    )
    if not commitments:
        return {"status": "ok", "scheduled": 0, "candidates": 0}

    try:
        from scheduler.engine import build_scheduler
        from tools.reminder import _reminder_actions

        scheduler = build_scheduler(allowed_actions=_reminder_actions())
    except Exception:
        return {"status": "no_scheduler", "scheduled": 0}

    existing = _existing_nudge_tasks(scheduler)
    default_hour = int(settings.memory_scheduler_bridge_default_hour)
    max_nudges = int(settings.memory_scheduler_bridge_max_nudges)

    scheduled = 0
    created: list[dict] = []
    for commitment in commitments:
        if scheduled >= max_nudges:
            break
        task = commitment["task"]
        if task.strip().casefold() in existing:
            continue
        fire_at = datetime.combine(commitment["date"], time(hour=default_hour))
        if fire_at <= now:
            fire_at = now + timedelta(minutes=5)
        try:
            record = scheduler.schedule(
                title=f"Memory nudge: {task}",
                action="reminder_fire",
                scheduled_for=fire_at.isoformat(timespec="seconds"),
                arguments={"task": task},
                tags=[_BRIDGE_TAG],
            )
        except Exception:
            continue
        existing.add(task.strip().casefold())
        created.append({"task": task, "scheduled_for": record.get("scheduled_for"), "id": record.get("id")})
        scheduled += 1

    return {"status": "ok", "scheduled": scheduled, "candidates": len(commitments), "created": created}
