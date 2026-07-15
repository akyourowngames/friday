"""Deterministic routing before Ares' general root tool-selection loop."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Any

from ares.turn_policy import (
    TurnExecutionContext,
    build_turn_execution_context,
    has_explicit_delegation_signal,
    is_agent_meta_question,
)


class DelegationMode(str, Enum):
    NONE = "none"
    EXPLICIT = "explicit"
    AUTO = "auto"
    META = "meta"


class DelegationFailureReason(str, Enum):
    DISABLED = "disabled"
    RUNTIME_UNAVAILABLE = "runtime_unavailable"
    NO_MATCHING_ROLE = "no_matching_role"
    TASK_LIMIT_EXCEEDED = "task_limit_exceeded"
    PROVIDER_FAILURE = "provider_failure"
    TIMEOUT = "timeout"
    AUTHORIZATION_FAILURE = "authorization_failure"


class DelegationTaskLimitError(ValueError):
    """A requested or review-expanded plan exceeds the configured bound."""


class DelegationRoleUnavailableError(KeyError):
    """A requested specialist role is disabled or not configured."""


_FAILURE_MESSAGES = {
    DelegationFailureReason.DISABLED: "Native multi-agent mode is disabled, so no agents ran.",
    DelegationFailureReason.RUNTIME_UNAVAILABLE: "The native multi-agent runtime is unavailable, so no agents ran.",
    DelegationFailureReason.NO_MATCHING_ROLE: "No configured specialist role matches the requested delegation, so no agents ran.",
    DelegationFailureReason.TASK_LIMIT_EXCEEDED: "The requested agent plan exceeds the configured task limit, so no agents ran.",
    DelegationFailureReason.PROVIDER_FAILURE: "The model provider failed during native delegation; the run did not complete successfully.",
    DelegationFailureReason.TIMEOUT: "Native multi-agent execution timed out; unfinished agents did not report success.",
    DelegationFailureReason.AUTHORIZATION_FAILURE: "Native delegation was not authorized for this request, so no agents ran.",
}


@dataclass(frozen=True, slots=True)
class DelegationAvailability:
    enabled: bool
    runtime_available: bool
    available_roles: tuple[str, ...]
    max_tasks_per_run: int = 8

    def __post_init__(self) -> None:
        if self.max_tasks_per_run < 1:
            raise ValueError("max_tasks_per_run must be at least 1")
        object.__setattr__(
            self,
            "available_roles",
            tuple(dict.fromkeys(str(role).strip() for role in self.available_roles if str(role).strip())),
        )


@dataclass(frozen=True, slots=True)
class DelegationPlanTask:
    task_id: str
    agent: str
    prompt: str
    depends_on: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.task_id.strip() or not self.agent.strip() or not self.prompt.strip():
            raise ValueError("delegation plan tasks require task_id, agent, and prompt")
        if self.task_id in self.depends_on:
            raise ValueError("a delegation task cannot depend on itself")
        object.__setattr__(self, "depends_on", tuple(dict.fromkeys(self.depends_on)))

    def as_tool_argument(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "agent": self.agent,
            "prompt": self.prompt,
            "depends_on": list(self.depends_on),
        }


@dataclass(frozen=True, slots=True)
class DelegationDecision:
    mode: DelegationMode
    should_delegate: bool
    reason: str
    requested_parallelism: bool = False
    requested_roles: tuple[str, ...] = ()
    plan: tuple[DelegationPlanTask, ...] = ()
    failure_reason: DelegationFailureReason | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "requested_roles", tuple(dict.fromkeys(self.requested_roles)))
        object.__setattr__(self, "plan", tuple(self.plan))
        if self.failure_reason is not None and self.should_delegate:
            raise ValueError("a failed delegation decision cannot be executable")
        if self.mode in {DelegationMode.NONE, DelegationMode.META} and self.should_delegate:
            raise ValueError(f"{self.mode.value} decisions cannot launch agents")

    @property
    def honest_failure_message(self) -> str | None:
        return _FAILURE_MESSAGES.get(self.failure_reason) if self.failure_reason else None

    def as_tool_arguments(self) -> dict[str, Any]:
        return {"tasks": [task.as_tool_argument() for task in self.plan]}


_ROLE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("planner", re.compile(r"\bplanners?\b", re.I)),
    ("researcher", re.compile(r"\bresearchers?\b", re.I)),
    ("analyst", re.compile(r"\banalysts?\b", re.I)),
    ("builder", re.compile(r"\bbuilders?\b", re.I)),
    ("reviewer", re.compile(r"\breviewers?\b", re.I)),
    ("synthesizer", re.compile(r"\bsynthesi[sz]ers?\b", re.I)),
)
_NUMBER_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10,
}
_COUNT_RE = re.compile(
    r"\b(?P<count>\d+|one|two|three|four|five|six|seven|eight|nine|ten)\s+"
    r"(?:agents?|researchers?|specialists?)\b",
    re.I,
)
_RUN_MANAGEMENT_RE = re.compile(
    r"\b(?:cancel|stop|resume|continue|show|inspect|list|get)\s+(?:the\s+)?(?:latest\s+)?agent\s+(?:run|runs|status|manifest)\b",
    re.I,
)
_AUTO_PARALLEL_RE = re.compile(r"\b(?:in\s+parallel|independently|separately|independent\s+(?:tracks|workstreams))\b", re.I)
_AUTO_REVIEW_RE = re.compile(
    r"\b(?:implement|build|change|fix)\b.*\b(?:review|verify|audit)\b|"
    r"\b(?:review|verify|audit)\b.*\b(?:implementation|change|fix)\b",
    re.I,
)
_SYNTHESIS_RE = re.compile(r"\b(?:synthesi[sz]e|synthesi[sz]er|combine\s+the\s+findings)\b", re.I)


def _requested_roles(text: str) -> tuple[str, ...]:
    return tuple(role for role, pattern in _ROLE_PATTERNS if pattern.search(text))


def _requested_count(text: str) -> int | None:
    match = _COUNT_RE.search(text)
    if not match:
        return None
    raw = match.group("count").casefold()
    return int(raw) if raw.isdigit() else _NUMBER_WORDS[raw]


def _clean_track(value: str) -> str:
    value = re.sub(r"^(?:research|compare|analy[sz]e|investigate|evaluate)\s+", "", value, flags=re.I)
    value = re.sub(r"\b(?:using|with)\s+separate\s+(?:agents?|researchers?|specialists?).*$", "", value, flags=re.I)
    return " ".join(value.strip(" .,:;-\n\t").split())[:120]


def _extract_tracks(text: str) -> tuple[str, ...]:
    lowered = text.casefold()
    if "backend" in lowered and "frontend" in lowered:
        return ("backend", "frontend")
    match = re.search(
        r"\b(?:research|compare|analy[sz]e|investigate|evaluate)\s+(.+?)"
        r"(?=\s+(?:in\s+parallel|using\s+separate|with\s+separate|then\s+synthesi[sz]e)|[.!?]|$)",
        text,
        re.I | re.S,
    )
    if not match:
        return ()
    candidate = match.group(1)
    raw_parts = re.split(r"\s*,\s*|\s+and\s+|\s+versus\s+|\s+vs\.?\s+", candidate, flags=re.I)
    parts = [_clean_track(part) for part in raw_parts]
    return tuple(dict.fromkeys(part for part in parts if 1 < len(part) <= 120))[:12]


def _is_meaningful_auto_request(text: str) -> bool:
    words = re.findall(r"[a-z0-9]+", text.casefold())
    if len(words) < 6:
        return False
    if _AUTO_REVIEW_RE.search(text):
        return True
    if _AUTO_PARALLEL_RE.search(text):
        return bool(
            len(_extract_tracks(text)) >= 2
            or ("backend" in text.casefold() and "frontend" in text.casefold())
        )
    return False


class DelegationRouter:
    """Produce a bounded, deterministic decision before general tool choice."""

    def route(
        self,
        context: TurnExecutionContext | str,
        availability: DelegationAvailability,
    ) -> DelegationDecision:
        if isinstance(context, str):
            context = build_turn_execution_context(context)
        text = context.user_input.strip()

        if is_agent_meta_question(text) or _RUN_MANAGEMENT_RE.search(text):
            return DelegationDecision(
                DelegationMode.META,
                False,
                "Agent introspection must query session-scoped run records; it must not launch a browser or a new team.",
            )

        explicit = has_explicit_delegation_signal(text)
        automatic = not explicit and _is_meaningful_auto_request(text)
        if not explicit and not automatic:
            return DelegationDecision(
                DelegationMode.NONE,
                False,
                "This turn has no explicit delegation request and no meaningful independent workstreams.",
            )

        mode = DelegationMode.EXPLICIT if explicit else DelegationMode.AUTO
        roles = _requested_roles(text)
        requested_count = _requested_count(text)
        parallel = bool(
            requested_count and requested_count > 1
            or re.search(r"\b(?:parallel|multiple|separate|independent|several)\b", text, re.I)
            or len(_extract_tracks(text)) > 1
        )

        if not availability.enabled:
            return self._failure(mode, DelegationFailureReason.DISABLED, parallel, roles)
        if not availability.runtime_available:
            return self._failure(mode, DelegationFailureReason.RUNTIME_UNAVAILABLE, parallel, roles)
        available = set(availability.available_roles)
        if not available:
            return self._failure(mode, DelegationFailureReason.NO_MATCHING_ROLE, parallel, roles)
        missing = [role for role in roles if role not in available]
        if missing:
            detail = f" Requested role(s) not configured: {', '.join(missing)}."
            return self._failure(mode, DelegationFailureReason.NO_MATCHING_ROLE, parallel, roles, detail)
        if requested_count is not None and requested_count > availability.max_tasks_per_run:
            return self._failure(mode, DelegationFailureReason.TASK_LIMIT_EXCEEDED, parallel, roles)

        plan = self._build_plan(
            text,
            available_roles=availability.available_roles,
            requested_roles=roles,
            requested_count=requested_count,
        )
        if not plan:
            return self._failure(mode, DelegationFailureReason.NO_MATCHING_ROLE, parallel, roles)
        if len(plan) > availability.max_tasks_per_run:
            return self._failure(mode, DelegationFailureReason.TASK_LIMIT_EXCEEDED, parallel, roles)
        if mode is DelegationMode.AUTO and len(plan) < 2:
            return DelegationDecision(
                DelegationMode.NONE,
                False,
                "Automatic delegation was skipped because the bounded plan has only one useful task.",
            )

        return DelegationDecision(
            mode,
            True,
            "The current turn explicitly requests native agents."
            if mode is DelegationMode.EXPLICIT
            else "Independent workstreams provide a deterministic parallelism benefit.",
            requested_parallelism=parallel,
            requested_roles=roles,
            plan=plan,
        )

    @staticmethod
    def _failure(
        mode: DelegationMode,
        reason: DelegationFailureReason,
        parallel: bool,
        roles: tuple[str, ...],
        detail: str = "",
    ) -> DelegationDecision:
        return DelegationDecision(
            mode,
            False,
            _FAILURE_MESSAGES[reason] + detail,
            requested_parallelism=parallel,
            requested_roles=roles,
            failure_reason=reason,
        )

    @staticmethod
    def _pick_role(available: tuple[str, ...], preferred: tuple[str, ...]) -> str | None:
        for role in preferred:
            if role in available:
                return role
        return available[0] if available else None

    def _build_plan(
        self,
        text: str,
        *,
        available_roles: tuple[str, ...],
        requested_roles: tuple[str, ...],
        requested_count: int | None,
    ) -> tuple[DelegationPlanTask, ...]:
        available = tuple(available_roles)
        tracks = _extract_tracks(text)
        tasks: list[DelegationPlanTask] = []

        if _AUTO_REVIEW_RE.search(text) and {"builder", "reviewer"}.issubset(available):
            tasks.append(DelegationPlanTask(
                "build", "builder", f"Implement the bounded request: {text[:800]}"
            ))
            tasks.append(DelegationPlanTask(
                "review",
                "reviewer",
                "Independently review the builder result for correctness, safety, and regressions.",
                depends_on=("build",),
            ))
        elif tracks:
            preferred = requested_roles or (
                ("researcher",) if re.search(r"\b(?:research|compare|investigate|source)\b", text, re.I)
                else ("analyst", "researcher")
            )
            role = self._pick_role(available, tuple(preferred))
            if role is None:
                return ()
            for index, track in enumerate(tracks, start=1):
                tasks.append(DelegationPlanTask(
                    f"{role}_{index}", role,
                    f"Independently investigate {track}. Preserve evidence, uncertainty, and task-specific conclusions.",
                ))
        elif "builder" in requested_roles and "reviewer" in requested_roles:
            tasks.append(DelegationPlanTask("build", "builder", f"Implement the bounded request: {text[:800]}"))
            tasks.append(DelegationPlanTask(
                "review", "reviewer", "Independently review the builder result for correctness, safety, and regressions.",
                depends_on=("build",),
            ))
        elif requested_roles:
            for role in requested_roles:
                dependencies = ("builder_1",) if role == "reviewer" and any(
                    task.agent == "builder" for task in tasks
                ) else ()
                task_id = f"{role}_{sum(task.agent == role for task in tasks) + 1}"
                tasks.append(DelegationPlanTask(
                    task_id,
                    role,
                    f"Work only as the {role} specialist on this bounded request: {text[:800]}",
                    depends_on=dependencies,
                ))
        else:
            preferred = (
                ("builder", "analyst") if re.search(r"\b(?:implement|build|fix|edit)\b", text, re.I)
                else ("researcher", "analyst", "planner")
            )
            role = self._pick_role(available, preferred)
            if role is None:
                return ()
            count = requested_count or (2 if _AUTO_PARALLEL_RE.search(text) else 1)
            for index in range(1, count + 1):
                tasks.append(DelegationPlanTask(
                    f"{role}_{index}", role,
                    f"Independently analyze one useful workstream of this bounded request: {text[:800]}",
                ))

        if requested_count and requested_count > len(tasks):
            role = self._pick_role(available, requested_roles or ("researcher", "analyst", "planner"))
            if role is None:
                return ()
            while len(tasks) < requested_count:
                index = len(tasks) + 1
                tasks.append(DelegationPlanTask(
                    f"{role}_{index}", role,
                    f"Independently cover a distinct workstream of this bounded request: {text[:800]}",
                ))

        if _SYNTHESIS_RE.search(text) and "synthesizer" in available and not any(
            task.agent == "synthesizer" for task in tasks
        ):
            dependencies = tuple(task.task_id for task in tasks)
            tasks.append(DelegationPlanTask(
                "synthesis", "synthesizer",
                "Synthesize the dependency results without increasing confidence or hiding disagreements.",
                depends_on=dependencies,
            ))
        return tuple(tasks)


def runtime_failure_decision(
    error: BaseException | str,
    *,
    mode: DelegationMode = DelegationMode.EXPLICIT,
) -> DelegationDecision:
    """Convert runtime failures into honest, non-success routing outcomes."""
    text = str(error or "").strip()
    lowered = text.casefold()
    if isinstance(error, DelegationTaskLimitError):
        reason = DelegationFailureReason.TASK_LIMIT_EXCEEDED
    elif isinstance(error, DelegationRoleUnavailableError):
        reason = DelegationFailureReason.NO_MATCHING_ROLE
    elif isinstance(error, TimeoutError) or "timed out" in lowered or "timeout" in lowered:
        reason = DelegationFailureReason.TIMEOUT
    elif isinstance(error, PermissionError) or "authoriz" in lowered or "permission" in lowered:
        reason = DelegationFailureReason.AUTHORIZATION_FAILURE
    else:
        reason = DelegationFailureReason.PROVIDER_FAILURE
    detail = f" Detail: {text[:500]}" if text else ""
    return DelegationDecision(
        mode,
        False,
        _FAILURE_MESSAGES[reason] + detail,
        failure_reason=reason,
    )


__all__ = [
    "DelegationAvailability",
    "DelegationDecision",
    "DelegationFailureReason",
    "DelegationMode",
    "DelegationPlanTask",
    "DelegationRouter",
    "DelegationRoleUnavailableError",
    "DelegationTaskLimitError",
    "runtime_failure_decision",
]
