"""LLM-based task planner. Generates execution plans for auto-executable tasks."""

import json
import re
import logging

logger = logging.getLogger(__name__)

PLANNING_PROMPT = """You are a task planner. Break the following task into clear, actionable steps.

Task: {title}
Description: {description}

Return a JSON array of steps. Each step must have:
- "step": number (starting at 1)
- "title": short action description (max 60 chars)
- "description": detailed instructions for this step

Rules:
- 2-8 steps (keep it focused)
- Steps should be sequential and build on each other
- Last step should be saving/writing the final result
- Each step should be completable by running tools
- Return ONLY the JSON array, no other text"""


class TaskPlanner:
    """Generates execution plans for tasks using the session LLM."""

    def __init__(self, llm_client):
        self.llm = llm_client

    async def generate_plan(self, task: dict) -> list[dict]:
        """Generate an execution plan for a task.

        Returns list of step dicts with keys: step, title, description, status.
        Falls back to a single-step plan on parse failure.
        """
        title = task.get("title", "Untitled task")
        description = task.get("description", "") or ""
        prompt = PLANNING_PROMPT.format(title=title, description=description)

        try:
            response = await self.llm.chat([{"role": "user", "content": prompt}])
            return self._parse_plan(response.get("content", ""))
        except Exception as e:
            logger.warning("Planning failed, using single-step fallback: %s", e)
            return self._fallback_plan(task)

    def _parse_plan(self, content: str) -> list[dict]:
        """Parse JSON plan from LLM response."""
        # Try direct JSON parse
        try:
            plan = json.loads(content)
            if isinstance(plan, list):
                return self._validate_plan(plan)
        except json.JSONDecodeError:
            pass

        # Try extracting JSON from markdown code blocks
        match = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", content, re.DOTALL)
        if match:
            try:
                plan = json.loads(match.group(1))
                if isinstance(plan, list):
                    return self._validate_plan(plan)
            except json.JSONDecodeError:
                pass

        # Try finding array in content
        match = re.search(r"\[.*\]", content, re.DOTALL)
        if match:
            try:
                plan = json.loads(match.group(0))
                if isinstance(plan, list):
                    return self._validate_plan(plan)
            except json.JSONDecodeError:
                pass

        raise ValueError("Could not parse plan from LLM response")

    def _validate_plan(self, plan: list[dict]) -> list[dict]:
        """Validate and normalize a parsed plan."""
        validated = []
        for i, step in enumerate(plan):
            if not isinstance(step, dict):
                continue
            validated.append({
                "step": step.get("step", i + 1),
                "title": str(step.get("title", f"Step {i + 1}"))[:60],
                "description": str(step.get("description", "")),
                "status": "pending",
            })

        if not validated:
            raise ValueError("Plan is empty after validation")

        # Ensure sequential numbering
        for i, step in enumerate(validated):
            step["step"] = i + 1

        return validated

    def _fallback_plan(self, task: dict) -> list[dict]:
        """Single-step fallback plan."""
        return [{
            "step": 1,
            "title": task.get("title", "Execute task")[:60],
            "description": task.get("description", "") or task.get("title", ""),
            "status": "pending",
        }]
