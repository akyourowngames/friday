"""Hard, current-turn authorization for root Ares tool calls.

Conversation history may explain a request, but it never grants authority.  A
``TurnExecutionContext`` is built solely from the current user message and
explicit single-use grants, then carried with that request (normally in a
``ContextVar`` by the Agent) until every tool call has finished.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Any

from ares.integrations.tool_registry import (
    AGENT_INTROSPECTION_TOOL_NAMES,
    DELEGATION_TOOL_NAMES,
    WORKFLOW_TOOL_NAMES,
    ToolCategory,
    categorize_tool_name,
)


class TurnIntent(str, Enum):
    CONVERSATION = "conversation"
    READ_ONLY = "read_only"
    DELEGATION = "delegation"
    LOCAL_MUTATION = "local_mutation"
    BROWSER_INTERACTION = "browser_interaction"
    DESKTOP_INTERACTION = "desktop_interaction"
    EXTERNAL_ACTION = "external_action"
    CONFIRMATION_RESPONSE = "confirmation_response"


class ToolEffect(str, Enum):
    READ_ONLY = "read_only"
    DELEGATION = "delegation"
    LOCAL_MUTATION = "local_mutation"
    WORKFLOW_MUTATION = "workflow_mutation"
    BROWSER_INTERACTION = "browser_interaction"
    DESKTOP_INTERACTION = "desktop_interaction"
    EXTERNAL_ACTION = "external_action"


BROWSER_ACTION_RE = re.compile(
    r"\b(?:open|click(?:ing)?|type|fill|navigate|visit|log\s*in|sign\s*in|submit|"
    r"upload|download|press|select|inspect|operate|scroll|interact|go\s+to)\b",
    re.IGNORECASE,
)
BROWSER_TARGET_RE = re.compile(
    r"\b(?:browser|chrome|website|web|web\s*page|webpage|web\s*app|page|site|portal|"
    r"dashboard|form|tab|url|google|github|youtube|instagram|linkedin|twitter)\b|https?://",
    re.IGNORECASE,
)
BROWSER_SESSION_REQUEST_RE = re.compile(
    r"\b(?:new|fresh)\s+(?:playwright|browser|chrome)(?:\s+session+)?\b|"
    r"\b(?:start|launch)\s+(?:a\s+)?(?:new|fresh)\s+"
    r"(?:playwright|browser|chrome)(?:\s+session+)?\b|"
    r"\b(?:start|restart|reopen|launch)\s+(?:the\s+)?(?:playwright|browser|chrome)"
    r"(?:\s+session+)?\b",
    re.IGNORECASE,
)
BROWSER_WINDOW_EXCEPTIONS = (
    "actual chrome window", "browser window", "chrome window", "visible desktop",
    "windows window", "desktop window",
)
_EXPLICIT_WINDOWS_MCP_RE = re.compile(
    r"\b(?:use|using|with|through|via)?\s*(?:the\s+)?"
    r"(?:windows|desktop|computer[-\s]?use)\s+mcp\b",
    re.IGNORECASE,
)
_EXPLICIT_PLAYWRIGHT_RE = re.compile(
    r"\b(?:use|using|with|through|via)?\s*(?:the\s+)?"
    r"(?:playwright|browser)\s+mcp\b|\b(?:use|using|via|through)\s+playwright\b",
    re.IGNORECASE,
)
_EXPLICIT_SURFACE_ACTION_RE = re.compile(
    r"\b(?:open|click|type|fill|navigate|visit|login|log\s+in|sign\s+in|submit|"
    r"upload|download|press|select|inspect|operate|scroll|interact|search|find|"
    r"send|message|msg|reply|read|look|capture|take|launch)\b",
    re.IGNORECASE,
)

_CASUAL_RE = re.compile(
    r"^\s*(?:hi|hello|hey|hiya|yo|sup|thanks|thank\s+you|thx|okay|ok|cool|nice|"
    r"great|got\s+it|sounds\s+good|alright|bye|goodbye)[!.?,\s]*$",
    re.IGNORECASE,
)
_CONFIRM_RE = re.compile(
    r"^\s*(?:yes|yep|yeah|confirm(?:ed)?|approve(?:d)?|go\s+ahead|do\s+it|proceed)[!.\s]*$",
    re.IGNORECASE,
)
_AGENT_META_PATTERNS = (
    re.compile(r"\bhow\s+many\s+(?:agents?|researchers?|specialists?)\b", re.I),
    re.compile(
        r"\bdid\s+(?:you|the\s+(?:agents?|researchers?|specialists?))\s+"
        r"(?:really\s+)?(?:use|launch|run|search)\b",
        re.I,
    ),
    re.compile(r"\bhow\s+(?:did|do|were)\s+.*\b(?:agents?|specialists?)\b", re.I),
    re.compile(r"\bshow\s+(?:me\s+)?(?:the\s+)?agent\s+run\b", re.I),
    re.compile(r"\bwhat\s+tools?\s+did\s+(?:the\s+)?agents?\s+use\b", re.I),
    re.compile(r"\b(?:agents?|researchers?|specialists?)\s+(?:ran|reported|used)\b", re.I),
    re.compile(r"\bagent\s+(?:count|status|manifest|history|run\s+id)\b", re.I),
    re.compile(r"\b(?:how|what|explain|is|are|does|did)\b.*\bmulti[-\s]?agent(?:\s+mode)?\b", re.I),
    re.compile(r"\bhow\s+did\s+you\s+launch\s+them\b", re.I),
)
_EXPLICIT_DELEGATION_PATTERNS = (
    re.compile(r"\buse\s+(?:multiple\s+|several\s+|\d+\s+|two\s+|three\s+|four\s+)?agents?\b", re.I),
    re.compile(r"\buse\s+(?:the\s+)?multi[-\s]?agent(?:\s+mode)?\b", re.I),
    re.compile(r"\bwith\s+multi[-\s]?agent\b", re.I),
    re.compile(
        r"\bwith\s+(?:multiple|several|two|three|four|five|\d+)\s+"
        r"(?:agents?|researchers?|specialists?)\b",
        re.I,
    ),
    re.compile(r"\bseparate\s+(?:researchers?|agents?|specialists?)\b", re.I),
    re.compile(r"\b(?:run|do)\s+.*\bin\s+parallel\s+with\s+agents?\b", re.I),
    re.compile(r"\bsupervisor\s+and\s+specialists?\b", re.I),
    re.compile(r"\bbuilder\s+and\s+(?:a\s+)?reviewer\b", re.I),
    re.compile(
        r"\b(?:launch|run|ask|have)\s+(?:two|three|four|five|\d+|multiple|several)\s+"
        r"(?:agents?|researchers?|specialists?)\b",
        re.I,
    ),
    re.compile(
        r"\b(?:launch|run|ask|have)\s+(?:the\s+)?"
        r"(?:agents?|researchers?|specialists?|planners?|analysts?|builders?|reviewers?|synthesi[sz]ers?)\b",
        re.I,
    ),
    re.compile(r"\bmulti[-\s]?agent(?:\s+mode)?\b", re.I),
    # FIX: Additional patterns for explicit multi-agent requests
    re.compile(r"\buse\s+(?:the\s+)?multi[-\s]?agent\b", re.I),
    re.compile(r"\bwith\s+multi[-\s]?agent\b", re.I),
    re.compile(r"\bdo\s+(?:this\s+)?research\b", re.I),
    re.compile(r"\bresearch\s+(?:this|that|on|about)\b", re.I),
    re.compile(r"\b(?:launch|run|use|start|spawn)\s+(?:the\s+)?agents?\b", re.I),
    re.compile(r"\bwith\s+(?:multiple|several|two|three|four|five|\d+)\s+(?:agents?|researchers?)\b", re.I),
    re.compile(r"\b(?:in\s+parallel|simultaneously)\b.*\bresearch\b", re.I),
    re.compile(r"\bresearch\b.*\b(?:in\s+parallel|simultaneously)\b", re.I),
)
_AGENT_MANAGEMENT_RE = re.compile(
    r"\b(?:cancel|stop|resume|continue)\s+(?:the\s+)?agent\s+run\b", re.I
)
_LOCAL_MUTATION_RE = re.compile(
    r"\b(?:write|edit|modify|change|update|configure|create|delete|remove|rename|move|copy|append|"
    r"remember|forget|store|"
    r"install|uninstall|execute|run(?:\s+(?:the\s+)?(?:command|script|code|tests?))?|"
    r"save|download|apply|patch|commit|snooze|dismiss|resolve|cancel|"
    r"adb|kdeconnect|python|pip|npm|git|docker|node)\b",
    re.IGNORECASE,
)
_EXTERNAL_ACTION_RE = re.compile(
    r"\b(?:send|email|text|sms|call|publish|post|purchase|buy|pay|transfer|invite|"
    r"share|deploy|merge|push|submit)\b",
    re.IGNORECASE,
)

# Additional patterns for command execution requests
_COMMAND_EXECUTION_RE = re.compile(
    r"\b(?:run|execute|do|perform|carry|complete|finish|start|begin|launch|open|close|stop|kill|"
    r"check|test|verify|validate|inspect|examine|analyze|process|handle|manage|"
    r"fix|repair|debug|troubleshoot|resolve|solve|"
    r"get|list|show|display|print|echo|cat|ls|dir|mkdir|cd|pwd|env|which|where)\b",
    re.IGNORECASE,
)
_VISION_MUTATION_RE = re.compile(
    r"\b(?:stop\s+watching|stop\s+all\s+(?:cameras?|screens?|visual\s+sources?)|"
    r"cancel\s+(?:the\s+)?(?:visual\s+)?watch|"
    r"forget\s+what\s+you\s+saw|erase\s+(?:recent\s+)?(?:visual\s+)?events?|"
    r"delete\s+(?:the\s+)?(?:saved\s+)?frame)\b|"
    r"\b(?:start|stop|watch|monitor)\b.{0,120}\b(?:vision|camera|webcam|screen|visual|object|cup|charger|download)\b",
    re.IGNORECASE,
)
_VISION_OBSERVATION_RE = re.compile(
    # A direct request to read/inspect a user's screen or camera is a local
    # Vision operation, not a generic read-only request or desktop-control
    # action.  Keep the visual target requirement so ordinary file/text reads
    # retain their read-only classification.
    r"\b(?:look(?:\s+at)?|observe|see|scan|analy[sz]e|describe|read|ocr|capture|inspect|"
    r"watch|monitor|compare|verify|check)\b.{0,160}\b(?:desk|camera|webcam|screen|display|monitor|"
    r"image|photo|picture|object|cup|charger|components?|setup|download|error|visible\s+text)\b|"
    r"\b(?:verify|check)\b.{0,160}\b(?:connected|components?|physical|setup)\b|"
    r"\bremember\s+where\b.{0,160}\b(?:placed|put|left|charger|object|cup|desk)\b",
    re.IGNORECASE,
)
_READ_ONLY_RE = re.compile(
    r"\b(?:what|why|how|who|when|where|explain|compare|research|investigate|analy[sz]e|"
    r"read|find|search|list|show|summarize|review|check|inspect|look\s+up|status)\b",
    re.IGNORECASE,
)
_CONTINUATION_RE = re.compile(r"\b(?:continue|resume)\b", re.IGNORECASE)
_SPECIFIC_TASK_CONTINUE_RE = re.compile(
    r"\b(?:continue|resume)\s+(?:task\s+)?(?P<task_id>[a-z0-9][a-z0-9_-]*-[0-9a-f]{8})\b",
    re.IGNORECASE,
)
_SPECIFIC_AGENT_RUN_RESUME_RE = re.compile(
    r"\b(?:continue|resume)\s+(?:the\s+)?agent\s+run\s+(?P<run_id>ma_[a-z0-9]+)\b",
    re.IGNORECASE,
)
_DESKTOP_TARGET_RE = re.compile(
    r"\b(?:desktop|window|windows|notepad|calculator|file\s+explorer|app|application|dialog)\b",
    re.IGNORECASE,
)


def utc_now() -> datetime:
    return datetime.now(UTC)


def _normalize_json_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float):
        if value != value or value in {float("inf"), float("-inf")}:
            raise ValueError("tool arguments cannot contain non-finite numbers")
        return value
    if isinstance(value, (Path, datetime, Enum)):
        if isinstance(value, datetime):
            return value.isoformat()
        if isinstance(value, Enum):
            return _normalize_json_value(value.value)
        return str(value)
    if isinstance(value, Mapping):
        normalized: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError("tool argument object keys must be strings")
            normalized[key] = _normalize_json_value(item)
        return normalized
    if isinstance(value, (list, tuple)):
        return [_normalize_json_value(item) for item in value]
    raise TypeError(f"tool arguments contain unsupported value {type(value).__name__}")


def canonical_arguments(arguments: Mapping[str, Any]) -> str:
    """Return stable JSON without weakening string or list equality."""
    normalized = _normalize_json_value(arguments)
    return json.dumps(
        normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def arguments_hash(arguments: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_arguments(arguments).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class ActionGrant:
    """An exact, expiring, single-use authorization minted by the root."""

    grant_id: str
    request_id: str
    session_id: str | None
    tool_name: str
    arguments_hash: str
    expires_at: datetime
    root_run_id: str | None = None
    child_run_id: str | None = None
    max_uses: int = 1

    def __post_init__(self) -> None:
        if not self.grant_id.strip() or not self.request_id.strip() or not self.tool_name.strip():
            raise ValueError("grant_id, request_id, and tool_name are required")
        if self.expires_at.tzinfo is None:
            raise ValueError("grant expiry must be timezone-aware")
        if not re.fullmatch(r"[0-9a-f]{64}", self.arguments_hash):
            raise ValueError("arguments_hash must be a SHA-256 hex digest")
        if self.max_uses != 1:
            raise ValueError("action grants are single-use")

    def is_expired(self, now: datetime | None = None) -> bool:
        return self.expires_at <= (now or utc_now())

    def mismatch_reason(
        self,
        *,
        request_id: str,
        session_id: str | None,
        tool_name: str,
        arguments: Mapping[str, Any],
        root_run_id: str | None = None,
        child_run_id: str | None = None,
        now: datetime | None = None,
    ) -> str | None:
        if self.is_expired(now):
            return "action grant has expired"
        if self.request_id != request_id:
            return "action grant request does not match"
        if self.session_id is not None and self.session_id != session_id:
            return "action grant session does not match"
        if self.root_run_id is not None and self.root_run_id != root_run_id:
            return "action grant root run does not match"
        if self.child_run_id is not None and self.child_run_id != child_run_id:
            return "action grant child run does not match"
        if self.tool_name != tool_name:
            return "action grant tool does not match"
        if self.arguments_hash != arguments_hash(arguments):
            return "action grant arguments do not match"
        return None


def issue_action_grant(
    *,
    request_id: str,
    session_id: str | None,
    tool_name: str,
    arguments: Mapping[str, Any],
    ttl_seconds: float = 300.0,
    root_run_id: str | None = None,
    child_run_id: str | None = None,
    now: datetime | None = None,
) -> ActionGrant:
    if ttl_seconds <= 0:
        raise ValueError("grant lifetime must be positive")
    issued_at = now or utc_now()
    return ActionGrant(
        grant_id=f"grant_{uuid.uuid4().hex}",
        request_id=str(request_id),
        session_id=str(session_id) if session_id is not None else None,
        tool_name=str(tool_name),
        arguments_hash=arguments_hash(arguments),
        expires_at=issued_at + timedelta(seconds=float(ttl_seconds)),
        root_run_id=str(root_run_id) if root_run_id is not None else None,
        child_run_id=str(child_run_id) if child_run_id is not None else None,
    )


class ActionGrantUseRegistry:
    """Process-local atomic consumption state for immutable grants."""

    def __init__(self) -> None:
        self._used: set[str] = set()
        self._lock = threading.Lock()

    def is_used(self, grant: ActionGrant | str) -> bool:
        grant_id = grant.grant_id if isinstance(grant, ActionGrant) else str(grant)
        with self._lock:
            return grant_id in self._used

    def consume(self, grant: ActionGrant, *, now: datetime | None = None) -> bool:
        """Atomically consume an unexpired grant exactly once."""
        if grant.is_expired(now):
            return False
        with self._lock:
            if grant.grant_id in self._used:
                return False
            self._used.add(grant.grant_id)
            return True


@dataclass(frozen=True, slots=True)
class TurnExecutionContext:
    request_id: str
    session_id: str | None
    user_input: str
    intent: TurnIntent
    explicit_targets: tuple[str, ...] = ()
    confirmation_grants: tuple[ActionGrant, ...] = ()
    root_run_id: str | None = None
    child_run_id: str | None = None

    def __post_init__(self) -> None:
        if not self.request_id.strip():
            raise ValueError("request_id is required")
        object.__setattr__(self, "explicit_targets", tuple(dict.fromkeys(self.explicit_targets)))
        object.__setattr__(self, "confirmation_grants", tuple(self.confirmation_grants))


@dataclass(frozen=True, slots=True)
class TurnAuthorizationDecision:
    allowed: bool
    reason: str
    effect: ToolEffect
    grant_id: str | None = None
    grant_consumed: bool = False


def is_browser_action_request(text: str) -> bool:
    lowered = str(text or "").casefold()
    if (
        _EXPLICIT_WINDOWS_MCP_RE.search(lowered)
        and _EXPLICIT_SURFACE_ACTION_RE.search(lowered)
    ):
        return False
    if (
        _EXPLICIT_PLAYWRIGHT_RE.search(lowered)
        and _EXPLICIT_SURFACE_ACTION_RE.search(lowered)
    ):
        return True
    if any(phrase in lowered for phrase in BROWSER_WINDOW_EXCEPTIONS):
        return False
    return bool(
        BROWSER_SESSION_REQUEST_RE.search(lowered)
        or (BROWSER_ACTION_RE.search(lowered) and BROWSER_TARGET_RE.search(lowered))
    )


def is_desktop_action_request(text: str) -> bool:
    value = str(text or "")
    if (
        _EXPLICIT_WINDOWS_MCP_RE.search(value)
        and _EXPLICIT_SURFACE_ACTION_RE.search(value)
    ):
        return True
    return bool(BROWSER_ACTION_RE.search(value) and _DESKTOP_TARGET_RE.search(value))


def is_agent_meta_question(text: str) -> bool:
    value = str(text or "")
    return any(pattern.search(value) for pattern in _AGENT_META_PATTERNS)


def has_explicit_delegation_signal(text: str) -> bool:
    value = str(text or "")
    return any(pattern.search(value) for pattern in _EXPLICIT_DELEGATION_PATTERNS)


def _extract_targets(text: str) -> tuple[str, ...]:
    lowered = text.casefold()
    targets: list[str] = []
    if re.search(r"\b(?:agents?|researchers?|specialists?|multi[-\s]?agent)\b", lowered):
        targets.append("agents")
    if BROWSER_TARGET_RE.search(lowered):
        targets.append("browser")
    if is_desktop_action_request(text):
        targets.append("desktop")
    if re.search(r"\b(?:file|folder|directory|repo|repository|codebase)\b|(?:[A-Za-z]:[\\/]|/)[^\s]+", text):
        targets.append("filesystem")
    if re.search(r"\b(?:command|shell|python|script|code|test|tests|lint|build|implement|fix)\b", lowered):
        targets.append("code")
    if re.search(r"\b(?:remember|memory|memories|person|contact|forget)\b", lowered):
        targets.append("recall")
    if re.search(r"\b(?:workflow|tasks?|durable\s+task|saved\s+task|task\s+id)\b", lowered):
        targets.append("workflow")
    if re.search(r"\bgoals?\b|\bfollow[-_\s]?ups?\b", lowered):
        targets.append("goals")
    if re.search(r"\b(?:watcher|monitor)\b", lowered):
        targets.append("watchers")
    if re.search(
        r"\b(?:vision|camera|webcam|screen(?:\s+share|shot)?|visual|photo|image|object\s+watch|look\s+at)\b",
        lowered,
    ) or _VISION_OBSERVATION_RE.search(lowered):
        targets.append("vision")
    if _VISION_MUTATION_RE.search(lowered):
        targets.append("vision")
    if re.search(r"\b(?:research|report|download|extract\s+document)\b", lowered):
        targets.append("research")
    if re.search(r"\b(?:config|configuration|settings)\b", lowered):
        targets.append("config")
    if re.search(r"\b(?:email|sms|text|telegram|phone|call)\b", lowered):
        targets.append("communication")
    if _CONTINUATION_RE.search(lowered):
        targets.append("prior_task")
    specific_task = _SPECIFIC_TASK_CONTINUE_RE.search(text)
    if specific_task:
        targets.extend(("workflow", f"task_id:{specific_task.group('task_id')}"))
    specific_agent_run = _SPECIFIC_AGENT_RUN_RESUME_RE.search(text)
    if specific_agent_run:
        targets.append(f"agent_run_id:{specific_agent_run.group('run_id')}")
    targets.extend(match[:200] for match in re.findall(r"https?://[^\s<>'\"]+", text, re.I))
    return tuple(dict.fromkeys(targets))


def classify_turn_intent(text: str, *, has_confirmation_grants: bool = False) -> TurnIntent:
    value = str(text or "").strip()
    if has_confirmation_grants and _CONFIRM_RE.fullmatch(value):
        return TurnIntent.CONFIRMATION_RESPONSE
    if not value or _CASUAL_RE.fullmatch(value):
        return TurnIntent.CONVERSATION
    if _AGENT_MANAGEMENT_RE.search(value):
        return TurnIntent.DELEGATION
    if is_agent_meta_question(value):
        return TurnIntent.READ_ONLY
    if has_explicit_delegation_signal(value):
        return TurnIntent.DELEGATION
    if is_browser_action_request(value):
        return TurnIntent.BROWSER_INTERACTION
    if is_desktop_action_request(value):
        return TurnIntent.DESKTOP_INTERACTION
    # Vision requests must take precedence over broad communication keywords
    # such as the noun "text".  Otherwise "read visible text on my screen"
    # is wrongly classified as an external SMS action and Vision tools are not
    # offered to the model.
    if _VISION_MUTATION_RE.search(value):
        return TurnIntent.LOCAL_MUTATION
    if _VISION_OBSERVATION_RE.search(value):
        return TurnIntent.LOCAL_MUTATION
    if _EXTERNAL_ACTION_RE.search(value):
        return TurnIntent.EXTERNAL_ACTION
    if _LOCAL_MUTATION_RE.search(value):
        return TurnIntent.LOCAL_MUTATION
    if _COMMAND_EXECUTION_RE.search(value):
        return TurnIntent.LOCAL_MUTATION
    if _SPECIFIC_TASK_CONTINUE_RE.search(value):
        return TurnIntent.LOCAL_MUTATION
    if _CONTINUATION_RE.search(value):
        return TurnIntent.READ_ONLY
    if _READ_ONLY_RE.search(value) or value.endswith("?"):
        return TurnIntent.READ_ONLY
    return TurnIntent.CONVERSATION


def build_turn_execution_context(
    user_input: str,
    *,
    request_id: str | None = None,
    session_id: str | None = None,
    confirmation_grants: tuple[ActionGrant, ...] = (),
    root_run_id: str | None = None,
    child_run_id: str | None = None,
) -> TurnExecutionContext:
    grants = tuple(confirmation_grants)
    return TurnExecutionContext(
        request_id=request_id or f"req_{uuid.uuid4().hex}",
        session_id=session_id,
        user_input=str(user_input or ""),
        intent=classify_turn_intent(user_input, has_confirmation_grants=bool(grants)),
        explicit_targets=_extract_targets(str(user_input or "")),
        confirmation_grants=grants,
        root_run_id=root_run_id,
        child_run_id=child_run_id,
    )


_READ_ONLY_FILE_TOOLS = frozenset({
    "read_file", "search_files", "list_directory", "get_file_info", "glob_pattern",
    "show_file_with_line_numbers", "find_text", "compare_files", "head_file", "tail_file",
    "file_tree", "checksum", "disk_usage", "find_duplicates", "preview_diff",
    "image_info",
})
_READ_ONLY_WORKFLOW_TOOLS = frozenset({"list_tasks", "get_task_status"})
_READ_ONLY_VISION_TOOLS = frozenset({
    "vision_compare", "vision_list_watches",
    "vision_list_events", "vision_list_sources",
})
_MUTATING_RECALL_PREFIXES = (
    "store_", "update_memory", "delete_memory", "remember_person", "update_person", "forget_person",
    "review_learning",
)
_READ_VERBS = ("get", "list", "read", "search", "find", "fetch", "inspect", "snapshot", "status", "show")
_EXTERNAL_VERBS = ("send", "publish", "push", "call", "submit", "purchase", "pay", "transfer", "invite", "share")
_LOCAL_MUTATION_VERBS = (
    "create", "update", "delete", "write", "edit", "move", "copy", "store",
    "remember", "forget", "run", "execute",
)


def classify_tool_effect(tool_name: str) -> ToolEffect:
    name = str(tool_name or "").strip()
    lowered = name.casefold()
    category = categorize_tool_name(name)
    if name in AGENT_INTROSPECTION_TOOL_NAMES or name == "list_agents":
        return ToolEffect.READ_ONLY
    if category is ToolCategory.CORE_CONVERSATION:
        return ToolEffect.READ_ONLY
    if category is ToolCategory.SYSTEM:
        return ToolEffect.LOCAL_MUTATION
    if name in DELEGATION_TOOL_NAMES:
        return ToolEffect.DELEGATION
    if name in _READ_ONLY_WORKFLOW_TOOLS:
        return ToolEffect.READ_ONLY
    if name in WORKFLOW_TOOL_NAMES:
        return ToolEffect.WORKFLOW_MUTATION
    if category is ToolCategory.BROWSER_DOM:
        return ToolEffect.BROWSER_INTERACTION
    if category is ToolCategory.DESKTOP_UIA:
        return ToolEffect.DESKTOP_INTERACTION
    if category is ToolCategory.COMMUNICATION:
        return ToolEffect.EXTERNAL_ACTION
    if category in {ToolCategory.PHONE, ToolCategory.TELEPHONY}:
        suffix = lowered.split("__")[-1]
        if suffix.startswith(_READ_VERBS) or any(f"_{verb}_" in f"_{suffix}_" for verb in _READ_VERBS):
            return ToolEffect.READ_ONLY
        return ToolEffect.EXTERNAL_ACTION
    if category is ToolCategory.CODE_EXECUTION:
        return ToolEffect.LOCAL_MUTATION
    if category is ToolCategory.FILES:
        return ToolEffect.READ_ONLY if name in _READ_ONLY_FILE_TOOLS else ToolEffect.LOCAL_MUTATION
    if category is ToolCategory.RESEARCH:
        if name in {"download_online_file", "create_research_report"}:
            return ToolEffect.LOCAL_MUTATION
        return ToolEffect.READ_ONLY
    if category is ToolCategory.RECALL:
        return ToolEffect.LOCAL_MUTATION if lowered.startswith(_MUTATING_RECALL_PREFIXES) else ToolEffect.READ_ONLY
    if category is ToolCategory.VISION:
        return ToolEffect.READ_ONLY if name in _READ_ONLY_VISION_TOOLS else ToolEffect.LOCAL_MUTATION
    if category in {ToolCategory.GOALS, ToolCategory.WATCHERS}:
        return ToolEffect.READ_ONLY if lowered.startswith(_READ_VERBS) else ToolEffect.LOCAL_MUTATION
    if category is ToolCategory.MCP:
        operation = lowered.split("__")[-1]
        if operation.startswith(_EXTERNAL_VERBS):
            return ToolEffect.EXTERNAL_ACTION
        if operation.startswith(_READ_VERBS):
            return ToolEffect.READ_ONLY
        # Unknown connected tools are consequential until classified.
        return ToolEffect.EXTERNAL_ACTION
    if lowered.startswith(_EXTERNAL_VERBS):
        return ToolEffect.EXTERNAL_ACTION
    if lowered.startswith(_LOCAL_MUTATION_VERBS):
        return ToolEffect.LOCAL_MUTATION
    # Future local/plugin tools must be registered before they can cross the
    # current-turn authorization boundary. Unknown is never read-only.
    return ToolEffect.EXTERNAL_ACTION


def _intent_allows(effect: ToolEffect, intent: TurnIntent) -> bool:
    """Apply the final effect/intent boundary immediately before execution."""
    allowed_effects = {
        TurnIntent.CONVERSATION: frozenset(),
        TurnIntent.READ_ONLY: frozenset({ToolEffect.READ_ONLY}),
        TurnIntent.DELEGATION: frozenset({
            ToolEffect.READ_ONLY,
            ToolEffect.DELEGATION,
        }),
        TurnIntent.LOCAL_MUTATION: frozenset({
            ToolEffect.READ_ONLY,
            ToolEffect.LOCAL_MUTATION,
            ToolEffect.WORKFLOW_MUTATION,
        }),
        TurnIntent.BROWSER_INTERACTION: frozenset({
            ToolEffect.READ_ONLY,
            ToolEffect.BROWSER_INTERACTION,
        }),
        TurnIntent.DESKTOP_INTERACTION: frozenset({
            ToolEffect.READ_ONLY,
            ToolEffect.DESKTOP_INTERACTION,
        }),
        TurnIntent.EXTERNAL_ACTION: frozenset({
            ToolEffect.READ_ONLY,
            ToolEffect.EXTERNAL_ACTION,
        }),
        # A confirmation response has authority only through an exact,
        # single-use ActionGrant handled above.
        TurnIntent.CONFIRMATION_RESPONSE: frozenset(),
    }
    return effect in allowed_effects[intent]


def authorize_turn_tool(
    context: TurnExecutionContext,
    tool_name: str,
    arguments: Mapping[str, Any],
    *,
    grant_uses: ActionGrantUseRegistry | None = None,
    consume_grant: bool = True,
    now: datetime | None = None,
) -> TurnAuthorizationDecision:
    """Authorize one call using only current-turn authority or an exact grant."""
    effect = classify_tool_effect(tool_name)
    category = categorize_tool_name(tool_name)
    grant_mismatches: list[str] = []
    for grant in context.confirmation_grants:
        mismatch = grant.mismatch_reason(
            request_id=context.request_id,
            session_id=context.session_id,
            tool_name=tool_name,
            arguments=arguments,
            root_run_id=context.root_run_id,
            child_run_id=context.child_run_id,
            now=now,
        )
        if mismatch is not None:
            grant_mismatches.append(mismatch)
            continue
        if grant_uses is None:
            return TurnAuthorizationDecision(
                False, "single-use action grant registry is required", effect, grant.grant_id, False
            )
        if grant_uses.is_used(grant):
            return TurnAuthorizationDecision(False, "action grant has already been used", effect, grant.grant_id, False)
        if consume_grant and not grant_uses.consume(grant, now=now):
            reason = "action grant has expired" if grant.is_expired(now) else "action grant has already been used"
            return TurnAuthorizationDecision(False, reason, effect, grant.grant_id, False)
        return TurnAuthorizationDecision(
            True,
            "authorized by exact single-use action grant",
            effect,
            grant.grant_id,
            consume_grant,
        )

    if context.intent is TurnIntent.READ_ONLY and "agents" in context.explicit_targets:
        if tool_name not in AGENT_INTROSPECTION_TOOL_NAMES:
            return TurnAuthorizationDecision(
                False, "agent meta-questions may only inspect real agent run records", effect
            )
    if "prior_task" in context.explicit_targets:
        continuation_tools = {
            "search_memory", "search_actions", "list_tasks", "get_task_status",
            "list_agent_runs", "get_latest_agent_run", "get_agent_run",
        }
        task_ids = {
            target.split(":", 1)[1]
            for target in context.explicit_targets
            if target.startswith("task_id:")
        }
        agent_run_ids = {
            target.split(":", 1)[1]
            for target in context.explicit_targets
            if target.startswith("agent_run_id:")
        }
        exact_run = (
            tool_name == "run_task"
            and len(task_ids) == 1
            and str(arguments.get("task_id") or "") in task_ids
        )
        exact_agent_resume = (
            tool_name == "resume_agent_run"
            and len(agent_run_ids) == 1
            and str(arguments.get("run_id") or "") in agent_run_ids
        )
        if tool_name not in continuation_tools and not exact_run and not exact_agent_resume:
            return TurnAuthorizationDecision(
                False,
                "ambiguous continue/resume must resolve a specific prior task before acting",
                effect,
            )

    cross_surface_categories = {
        TurnIntent.BROWSER_INTERACTION: {
            ToolCategory.DESKTOP_UIA,
            ToolCategory.COMMUNICATION,
            ToolCategory.PHONE,
            ToolCategory.TELEPHONY,
            ToolCategory.MCP,
        },
        TurnIntent.DESKTOP_INTERACTION: {
            ToolCategory.BROWSER_DOM,
            ToolCategory.COMMUNICATION,
            ToolCategory.PHONE,
            ToolCategory.TELEPHONY,
            ToolCategory.MCP,
        },
    }
    if category in cross_surface_categories.get(context.intent, set()):
        return TurnAuthorizationDecision(
            False,
            f"current {context.intent.value} turn is exclusive to its requested UI surface",
            effect,
        )

    if _intent_allows(effect, context.intent):
        return TurnAuthorizationDecision(
            True, f"current turn explicitly supports {effect.value}", effect
        )

    if context.confirmation_grants and grant_mismatches:
        reason = next(
            (item for item in grant_mismatches if "arguments" in item or "expired" in item),
            grant_mismatches[0],
        )
        return TurnAuthorizationDecision(False, reason, effect)
    return TurnAuthorizationDecision(
        False,
        f"current {context.intent.value} turn does not authorize {effect.value}",
        effect,
    )


__all__ = [
    "ActionGrant",
    "ActionGrantUseRegistry",
    "ToolEffect",
    "TurnAuthorizationDecision",
    "TurnExecutionContext",
    "TurnIntent",
    "arguments_hash",
    "authorize_turn_tool",
    "build_turn_execution_context",
    "canonical_arguments",
    "classify_tool_effect",
    "classify_turn_intent",
    "has_explicit_delegation_signal",
    "is_agent_meta_question",
    "is_browser_action_request",
    "is_desktop_action_request",
    "issue_action_grant",
]
