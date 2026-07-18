"""Stateful policy and recovery for Playwright browser automation.

The Playwright MCP owns the browser.  This module owns the *reasoning state*
around it: which conversation requested the work, whether its evidence is still
fresh, how much authority the user granted, and whether a mutation was verified.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from threading import RLock
from time import monotonic
from typing import Any


_PLAYWRIGHT_PREFIX = "mcp__playwright__browser_"
_OBSERVE_TOKENS = ("snapshot", "screenshot", "console", "network")
_MUTATE_TOKENS = (
    "click", "type", "fill", "select", "press", "drag", "upload", "navigate",
    "go_back", "go_forward", "reload", "resize", "handle_dialog", "close", "install",
    "evaluate", "run_code", "wait_for",
)
_CONSEQUENTIAL_WORDS = {
    "send", "submit", "publish", "post", "delete", "remove", "purchase", "buy",
    "checkout", "pay", "transfer", "deploy", "merge", "share", "invite",
    "password", "security", "make public",
}
_BROWSER_HINTS = (
    "browser", "website", "web page", "webpage", "playwright", "http://", "https://",
    "log in", "login", "form", "tab", "click", "navigate",
)
_BROWSER_CLOSED_RE = re.compile(
    r"\b(?:target\s+(?:page|context|browser)\s+has\s+been\s+closed|"
    r"browser\s+(?:is|was|has\s+been)\s+closed|browser\s+disconnected|"
    r"browser\s+process\s+(?:exited|terminated|crashed)|"
    r"connection\s+(?:is\s+)?closed|transport\s+(?:is\s+)?closed)\b",
    re.IGNORECASE,
)


@dataclass(slots=True)
class BrowserPreflight:
    allowed: bool = True
    cached_result: str | None = None
    message: str = ""


@dataclass(slots=True)
class BrowserTaskState:
    goal: str = ""
    authority: str = "work"
    browser_active: bool = False
    snapshot: str | None = None
    snapshot_at: float = 0.0
    snapshot_generation: int = -1
    verification_required: bool = False
    last_action_signature: str = ""
    consecutive_failures: int = 0
    stale_recoveries: set[str] = field(default_factory=set)


class BrowserTaskController:
    """Coordinate fast, safe browser use across concurrent Ares conversations."""

    def __init__(self, *, snapshot_ttl_seconds: float = 4.0) -> None:
        self.snapshot_ttl_seconds = snapshot_ttl_seconds
        self._states: dict[str, BrowserTaskState] = {}
        self._generation = 0
        self._lock = RLock()

    @staticmethod
    def _session_key(session_id: str | None) -> str:
        return str(session_id or "default")

    def _state(self, session_id: str | None) -> BrowserTaskState:
        return self._states.setdefault(self._session_key(session_id), BrowserTaskState())

    @staticmethod
    def is_playwright_tool(tool_name: str) -> bool:
        return str(tool_name).casefold().startswith(_PLAYWRIGHT_PREFIX)

    @staticmethod
    def _short_tool(tool_name: str) -> str:
        return str(tool_name).casefold().removeprefix("mcp__playwright__")

    @classmethod
    def _is_snapshot(cls, tool_name: str) -> bool:
        return cls._short_tool(tool_name) == "browser_snapshot"

    @classmethod
    def _is_observation(cls, tool_name: str, arguments: dict[str, Any]) -> bool:
        short = cls._short_tool(tool_name)
        if any(token in short for token in _OBSERVE_TOKENS):
            return True
        if short == "browser_tabs":
            return str(arguments.get("action", "list")).casefold() == "list"
        return False

    @classmethod
    def _is_mutation(cls, tool_name: str, arguments: dict[str, Any]) -> bool:
        short = cls._short_tool(tool_name)
        if short == "browser_tabs":
            return str(arguments.get("action", "list")).casefold() != "list"
        return any(token in short for token in _MUTATE_TOKENS)

    @classmethod
    def _can_start_without_snapshot(cls, tool_name: str, arguments: dict[str, Any]) -> bool:
        short = cls._short_tool(tool_name)
        return cls._is_observation(tool_name, arguments) or short in {
            "browser_navigate", "browser_install", "browser_close", "browser_tabs",
            "browser_wait_for", "browser_handle_dialog",
        }

    @staticmethod
    def _signature(tool_name: str, arguments: dict[str, Any]) -> str:
        ordered = sorted((str(key), repr(value)) for key, value in arguments.items())
        return f"{tool_name}:{ordered}"

    @staticmethod
    def _result_failed(result: str) -> bool:
        lowered = str(result or "").strip().casefold()
        return lowered.startswith("error:") or any(
            marker in lowered for marker in ("timed out", "reported failure", "not connected")
        )

    @classmethod
    def result_succeeded(cls, result: str) -> bool:
        """Public result check used when composing recovery telemetry."""
        return not cls._result_failed(result)

    @classmethod
    def failure_kind(cls, result: str) -> str:
        """Classify a failed action without inventing browser availability."""
        if not cls._result_failed(result):
            return "none"
        if cls._stale_ref(result):
            return "stale_reference"
        if _BROWSER_CLOSED_RE.search(str(result or "")):
            return "browser_closed"
        return "action_failed"

    @staticmethod
    def _stale_ref(result: str) -> bool:
        lowered = str(result or "").casefold()
        return ("ref" in lowered or "reference" in lowered) and any(
            token in lowered for token in ("stale", "not found", "does not exist", "invalid", "unknown")
        )

    @staticmethod
    def _text_blob(arguments: dict[str, Any]) -> str:
        return " ".join(str(value) for value in arguments.values()).casefold()

    @staticmethod
    def _matched_consequential_terms(text: str) -> set[str]:
        matched: set[str] = set()
        for term in _CONSEQUENTIAL_WORDS:
            if re.search(rf"(?<!\w){re.escape(term)}(?!\w)", text.casefold()):
                matched.add(term)
        return matched

    @classmethod
    def _explicitly_authorizes_consequential_action(cls, text: str) -> bool:
        lowered = text.casefold()
        for term in cls._matched_consequential_terms(lowered):
            for match in re.finditer(rf"(?<!\w){re.escape(term)}(?!\w)", lowered):
                prefix = lowered[max(0, match.start() - 28):match.start()]
                if not any(
                    negation in prefix
                    for negation in ("do not ", "don't ", "dont ", "never ", "without ", "not to ")
                ):
                    return True
        return False

    def begin_turn(
        self,
        session_id: str | None,
        user_input: str,
        *,
        routing_text: str = "",
    ) -> str:
        """Record the current browser goal and return compact runtime guidance."""
        text = str(user_input or "").strip()
        lowered = f"{text} {routing_text}".casefold()
        with self._lock:
            state = self._state(session_id)
            state.goal = text[:800]
            state.browser_active = any(hint in lowered for hint in _BROWSER_HINTS)
            state.authority = "observe" if any(
                phrase in lowered for phrase in ("inspect", "read", "look at", "show me", "what is", "find")
            ) else "work"
            if self._explicitly_authorizes_consequential_action(lowered):
                state.authority = "consequential"
            state.stale_recoveries.clear()
            state.consecutive_failures = 0
            if not state.browser_active:
                # Current-turn intent wins. Do not drag an unfinished browser
                # verification into an unrelated chat request.
                state.verification_required = False
                return ""
            # A human or another surface may have changed the page between turns.
            state.snapshot_generation = -1
            return self.guidance(session_id)

    def guidance(self, session_id: str | None) -> str:
        with self._lock:
            state = self._state(session_id)
            verification = "required after the next/current mutation" if state.verification_required else "not pending"
            return (
                "## Live Browser Task\n"
                f"Goal: {state.goal or 'continue the current browser task'}\n"
                f"Authority: {state.authority}. Post-action verification: {verification}.\n"
                "Use a purpose-built connector/API/terminal for semantic data operations; use Playwright for web UI. "
                "Navigate directly when the URL is known, then take one accessibility snapshot. Reuse its refs only "
                "while the page is unchanged; batch related fields with browser_fill_form and paste a whole TSV/CSV "
                "block into spreadsheets instead of filling cells one at a time. After navigation, click, submit, "
                "tab change, or unexpected output, take a fresh snapshot and verify the requested outcome. "
                "Never claim a browser action succeeded while verification is pending."
            )

    def before_call(
        self, session_id: str | None, tool_name: str, arguments: dict[str, Any]
    ) -> BrowserPreflight:
        if not self.is_playwright_tool(tool_name):
            return BrowserPreflight()
        with self._lock:
            state = self._state(session_id)
            if self._is_snapshot(tool_name):
                fresh = (
                    state.snapshot is not None
                    and state.snapshot_generation == self._generation
                    and monotonic() - state.snapshot_at <= self.snapshot_ttl_seconds
                )
                if fresh:
                    return BrowserPreflight(cached_result=state.snapshot)
                return BrowserPreflight()

            signature = self._signature(tool_name, arguments)
            if state.consecutive_failures >= 2 and state.last_action_signature == signature:
                return BrowserPreflight(
                    allowed=False,
                    message=(
                        "Error: Browser action stopped after two identical failures. Take a fresh browser_snapshot, "
                        "choose new evidence, or report the blocker instead of repeating the same action."
                    ),
                )

            blob = self._text_blob(arguments)
            consequential = self._matched_consequential_terms(blob)
            if consequential and state.authority != "consequential":
                return BrowserPreflight(
                    allowed=False,
                    message=(
                        "Error: This browser action appears consequential "
                        f"({', '.join(sorted(consequential))}) and the current user turn did not authorize it. "
                        "Ask for explicit confirmation before acting."
                    ),
                )

            valid_snapshot = state.snapshot_generation == self._generation and state.snapshot is not None
            if not valid_snapshot and not self._can_start_without_snapshot(tool_name, arguments):
                return BrowserPreflight(
                    allowed=False,
                    message=(
                        "Error: Browser interaction needs a fresh accessibility snapshot for this conversation. "
                        "Call mcp__playwright__browser_snapshot first; do not guess a ref or coordinate."
                    ),
                )
            return BrowserPreflight()

    def after_call(
        self, session_id: str | None, tool_name: str, arguments: dict[str, Any], result: str
    ) -> str:
        text = str(result or "")
        if not self.is_playwright_tool(tool_name):
            return text
        with self._lock:
            state = self._state(session_id)
            failed = self._result_failed(text)
            signature = self._signature(tool_name, arguments)
            if failed:
                if state.last_action_signature == signature:
                    state.consecutive_failures += 1
                else:
                    state.last_action_signature = signature
                    state.consecutive_failures = 1
            else:
                state.last_action_signature = signature
                state.consecutive_failures = 0

            if self._is_snapshot(tool_name) and not failed:
                state.snapshot = text
                state.snapshot_at = monotonic()
                state.snapshot_generation = self._generation
                state.verification_required = False
                return text

            if self._is_mutation(tool_name, arguments) and not failed:
                self._generation += 1
                state.snapshot = None
                state.snapshot_generation = -1
                state.verification_required = True
                return (
                    f"{text}\n\nBrowser controller: page state may have changed. "
                    "Take a fresh browser_snapshot and verify the requested outcome before reporting success."
                )
            if failed:
                failure_kind = self.failure_kind(text)
                if failure_kind == "stale_reference":
                    diagnosis = (
                        "Browser controller diagnosis: stale_reference. The page reference expired; "
                        "this is not evidence that the browser closed or crashed."
                    )
                elif failure_kind == "browser_closed":
                    diagnosis = (
                        "Browser controller diagnosis: browser_closed. Playwright explicitly reported "
                        "a closed or disconnected browser runtime."
                    )
                else:
                    diagnosis = (
                        "Browser controller diagnosis: action_failed. This result does not prove that "
                        "the browser closed or crashed; verify availability with browser_tabs(list) or "
                        "browser_snapshot before describing the browser state."
                    )
                return f"{text}\n\n{diagnosis}"
            return text

    def verification_pending(self, session_id: str | None) -> bool:
        """Return whether a successful browser mutation still needs read-back."""
        with self._lock:
            return self._state(session_id).verification_required

    def should_recover_stale_ref(
        self, session_id: str | None, tool_name: str, arguments: dict[str, Any], result: str
    ) -> bool:
        if (
            not self.is_playwright_tool(tool_name)
            or not self._is_mutation(tool_name, arguments)
            or not self._result_failed(result)
            or not self._stale_ref(result)
        ):
            return False
        with self._lock:
            state = self._state(session_id)
            signature = self._signature(tool_name, arguments)
            if signature in state.stale_recoveries:
                return False
            state.stale_recoveries.add(signature)
            state.snapshot = None
            state.snapshot_generation = -1
            return True
