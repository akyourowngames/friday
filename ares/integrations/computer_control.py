"""Semantic state and workflow control for native Windows applications.

Windows MCP remains the actuator.  This module owns the state which raw UI
automation does not: what application/view is active, which region an element
belongs to, what text is allowed in that region, and whether an old element
reference still belongs to the current UI generation.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from threading import RLock
from typing import Any, Mapping, Protocol


_WINDOWS_PREFIX = "mcp__windows__"
_MUTATION_TOKENS = (
    "click",
    "type",
    "edit",
    "select",
    "key",
    "drag",
    "scroll",
    "launch",
    "resize",
)
_SUCCESS_FAILURE_MARKERS = (
    "error:",
    "timed out",
    "not connected",
    "not found",
    "failed",
)
_TREE_LINE_RE = re.compile(
    r'^(?P<prefix>[│ ]*)(?:├──|└──)\s+'
    r'(?:(?P<coords>\(-?\d+,-?\d+\))\s+)?'
    r'(?P<role>[^"]+?)\s+"(?P<name>.*?)"(?P<meta>.*)$'
)
_COORD_RE = re.compile(r"\((-?\d+)\s*,\s*(-?\d+)\)")
_VALUE_RE = re.compile(r'\[value:"(.*?)"\]')


class SendMessagePhase(str, Enum):
    OPEN_APP = "open_app"
    OPEN_GLOBAL_SEARCH = "open_global_search"
    SEARCH_CONTACT = "search_contact"
    SELECT_CONTACT = "select_contact"
    VERIFY_CHAT = "verify_chat"
    FOCUS_COMPOSER = "focus_composer"
    TYPE_MESSAGE = "type_message"
    VERIFY_DRAFT = "verify_draft"
    SEND = "send"
    VERIFY_SENT = "verify_sent"
    COMPLETE = "complete"


@dataclass(frozen=True, slots=True)
class SendMessageGoal:
    """Immutable separation between navigation text and user content."""

    target_name: str
    message_text: str


@dataclass(frozen=True, slots=True)
class UIElement:
    role: str
    name: str
    path: tuple[str, ...]
    x: int | None = None
    y: int | None = None
    focused: bool = False
    value: str = ""

    @property
    def semantic_path(self) -> str:
        return " / ".join((*self.path, self.name)).strip(" /")


@dataclass(frozen=True, slots=True)
class RegionState:
    name: str
    visible: bool = True
    focused: bool = False
    text: str = ""
    element: UIElement | None = None
    confidence: float = 0.0

    def projection(self) -> dict[str, Any]:
        element = self.element
        return {
            "visible": self.visible,
            "focused": self.focused,
            "text": self.text,
            "role": element.role if element else "",
            "loc": [element.x, element.y] if element and element.x is not None else None,
            "path": element.semantic_path if element else "",
            "confidence": round(self.confidence, 2),
        }


@dataclass(frozen=True, slots=True)
class AppState:
    app_name: str
    window_title: str
    view: str
    selected_entity: str = ""
    search_mode: bool = False
    focused_region: str = ""
    visible_regions: Mapping[str, RegionState] = field(default_factory=dict)
    visible_overlay: str = ""
    confidence: float = 0.0
    window_width: int = 0
    window_height: int = 0
    elements: tuple[UIElement, ...] = ()

    def region(self, name: str) -> RegionState | None:
        normalized = _normalize_region(name)
        if normalized in {"active_window", "application"}:
            return RegionState(
                name=normalized,
                focused=normalized == "active_window",
                confidence=1.0,
            )
        return self.visible_regions.get(normalized)

    @property
    def fingerprint(self) -> str:
        payload = {
            "app": self.app_name,
            "window": self.window_title,
            "view": self.view,
            "selected": self.selected_entity,
            "search": self.search_mode,
            "focus": self.focused_region,
            "overlay": self.visible_overlay,
            "regions": {
                key: {
                    "visible": value.visible,
                    "focused": value.focused,
                    "text": value.text,
                }
                for key, value in sorted(self.visible_regions.items())
            },
        }
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
        ).hexdigest()[:16]

    def compact_projection(self) -> dict[str, Any]:
        return {
            "app": self.app_name,
            "window_title": self.window_title,
            "view": self.view,
            "selected_entity": self.selected_entity or None,
            "search_mode": self.search_mode,
            "focused_region": self.focused_region or None,
            "visible_overlay": self.visible_overlay or None,
            "regions": {
                name: region.projection()
                for name, region in sorted(self.visible_regions.items())
            },
            "confidence": round(self.confidence, 2),
            "ui_fingerprint": self.fingerprint,
        }


@dataclass(frozen=True, slots=True)
class SemanticAction:
    intent: str
    entity: str
    phase: str
    expected_app: str
    expected_region: str
    purpose: str
    text_owner: str
    ui_generation: int | None
    tool_name: str

    @property
    def signature(self) -> str:
        return "|".join(
            (
                self.intent,
                self.entity.casefold(),
                self.phase,
                self.expected_region,
                self.purpose,
                self.text_owner,
            )
        )


@dataclass(frozen=True, slots=True)
class ComputerPreflight:
    allowed: bool = True
    message: str = ""
    action: SemanticAction | None = None


@dataclass(frozen=True, slots=True)
class ComputerTrace:
    task_id: str
    app: str
    phase: str
    semantic_action: str
    precondition_result: str
    postcondition_result: str
    ui_fingerprint: str
    state_transition: str
    recovery_reason: str
    duration_ms: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "app": self.app,
            "phase": self.phase,
            "semantic_action": self.semantic_action,
            "precondition_result": self.precondition_result,
            "postcondition_result": self.postcondition_result,
            "ui_fingerprint": self.ui_fingerprint,
            "state_transition": self.state_transition,
            "recovery_reason": self.recovery_reason,
            "duration_ms": round(self.duration_ms, 2),
        }


class AppAdapter(Protocol):
    name: str

    def detect(self, snapshot: str) -> float: ...

    def parse_state(self, snapshot: str) -> AppState: ...

    def available_skills(self, state: AppState) -> tuple[str, ...]: ...

    def validate_action(
        self,
        state: AppState,
        action: SemanticAction,
        arguments: Mapping[str, Any],
    ) -> tuple[bool, str]: ...

    def verify_postcondition(
        self,
        previous_state: AppState,
        action: SemanticAction,
        new_state: AppState,
        goal: SendMessageGoal | None,
    ) -> tuple[bool, str]: ...


@dataclass(slots=True)
class ComputerTaskState:
    task_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    goal: SendMessageGoal | None = None
    phase: SendMessagePhase = SendMessagePhase.OPEN_APP
    app_state: AppState | None = None
    ui_generation: int = 0
    completed_phases: set[SendMessagePhase] = field(default_factory=set)
    action_history: deque[tuple[str, str]] = field(
        default_factory=lambda: deque(maxlen=16)
    )
    pending_action: SemanticAction | None = None
    pending_started_at: float = 0.0
    observation_required: bool = False
    consecutive_failures: int = 0
    recovery_reason: str = ""
    active: bool = False


def _normalize_region(value: str) -> str:
    text = re.sub(r"[^a-z0-9]+", "_", str(value or "").strip().casefold()).strip("_")
    aliases = {
        "search": "global_search",
        "contact_search": "global_search",
        "search_box": "global_search",
        "message_input": "message_composer",
        "message_box": "message_composer",
        "composer": "message_composer",
        "chat_input": "message_composer",
        "chat_header": "chat_header",
        "header": "chat_header",
        "conversation": "message_history",
        "chat_history": "message_history",
    }
    return aliases.get(text, text)


def _snapshot_window(snapshot: str) -> tuple[str, int, int]:
    """Extract the first focused-window table row from Windows MCP output."""

    section = str(snapshot or "").partition("Focused Window:")[2].partition(
        "Opened Windows:"
    )[0]
    lines = [line.strip() for line in section.splitlines() if line.strip()]
    for line in lines:
        if line.startswith(("Name ", "---", "No active")):
            continue
        # tabulate's final five columns are depth/status/width/height/handle;
        # the title itself may contain spaces.
        match = re.match(
            r"(?P<title>.*?)\s+\d+\s+(?:Maximized|Minimized|Normal|Hidden)"
            r"\s+(?P<width>\d+)\s+(?P<height>\d+)\s+\d+\s*$",
            line,
            re.IGNORECASE,
        )
        if match:
            return (
                match.group("title").strip(),
                int(match.group("width")),
                int(match.group("height")),
            )
    tree_window = re.search(r'(?m)^\s*window\s+"(.*?)"\s*$', snapshot)
    return (tree_window.group(1).strip() if tree_window else "", 0, 0)


def _parse_elements(snapshot: str) -> tuple[UIElement, ...]:
    """Parse Windows MCP's semantic tree while preserving ancestry."""

    tree = str(snapshot or "").partition("UI Tree:")[2]
    ancestors: dict[int, str] = {}
    elements: list[UIElement] = []
    current_window = ""
    for raw_line in tree.splitlines():
        line = raw_line.rstrip()
        window_match = re.match(r'^\s*window\s+"(.*?)"\s*$', line)
        if window_match:
            current_window = window_match.group(1).strip()
            ancestors.clear()
            continue
        match = _TREE_LINE_RE.match(line)
        if not match:
            continue
        prefix = match.group("prefix").replace("│", " ")
        depth = max(0, len(prefix) // 4)
        role = match.group("role").strip().casefold()
        name = match.group("name").strip()
        meta = match.group("meta") or ""
        coords = _COORD_RE.search(match.group("coords") or "")
        value = _VALUE_RE.search(meta)
        path = tuple(
            item for level, item in sorted(ancestors.items()) if level < depth
        )
        elements.append(
            UIElement(
                role=role,
                name=name,
                path=((current_window,) if current_window else ()) + path,
                x=int(coords.group(1)) if coords else None,
                y=int(coords.group(2)) if coords else None,
                focused="[focused]" in meta.casefold(),
                value=value.group(1) if value else "",
            )
        )
        # Nodes without coordinates are semantic containers and therefore
        # useful ancestors for every deeper actionable child.
        if coords is None:
            ancestors[depth] = name or role
            for level in tuple(ancestors):
                if level > depth:
                    ancestors.pop(level, None)
    return tuple(elements)


def _element_blob(element: UIElement) -> str:
    return " ".join((*element.path, element.role, element.name)).casefold()


def _best_region(
    elements: tuple[UIElement, ...],
    *,
    name: str,
    positive: tuple[str, ...],
    negative: tuple[str, ...] = (),
    prefer_bottom: bool = False,
    window_height: int = 0,
) -> RegionState | None:
    candidates: list[tuple[float, UIElement]] = []
    for element in elements:
        if element.role not in {"edit", "document", "pane", "group", "text"}:
            continue
        blob = _element_blob(element)
        score = sum(0.28 for token in positive if token in blob)
        score -= sum(0.35 for token in negative if token in blob)
        if element.role == "edit":
            score += 0.2
        if prefer_bottom and element.y is not None and window_height:
            score += 0.25 * min(1.0, element.y / window_height)
        if element.focused:
            score += 0.03
        if score > 0:
            candidates.append((score, element))
    if not candidates:
        return None
    score, element = max(candidates, key=lambda item: item[0])
    return RegionState(
        name=name,
        focused=element.focused,
        text=element.value,
        element=element,
        confidence=min(score, 0.99),
    )


class GenericWindowsAdapter:
    """Conservative adapter for unknown multi-pane Windows applications."""

    name = "unknown_app"

    def detect(self, snapshot: str) -> float:
        return 0.05 if "UI Tree:" in str(snapshot or "") else 0.0

    def parse_state(self, snapshot: str) -> AppState:
        title, width, height = _snapshot_window(snapshot)
        elements = _parse_elements(snapshot)
        regions: dict[str, RegionState] = {}
        edits = [element for element in elements if element.role == "edit"]
        for index, element in enumerate(edits, start=1):
            blob = _element_blob(element)
            if any(token in blob for token in ("search", "filter", "find")):
                region_name = "global_search"
            elif any(
                token in blob
                for token in (
                    "message composer",
                    "messagecomposer",
                    "compose",
                    "reply",
                    "chat input",
                    "write a message",
                )
            ):
                region_name = "message_composer"
            elif any(
                token in blob
                for token in ("editor", "document", "code", "text area", "body")
            ):
                region_name = "editor"
            else:
                region_name = f"editable_{index}"
            if region_name in regions:
                region_name = f"{region_name}_{index}"
            regions[region_name] = RegionState(
                name=region_name,
                focused=element.focused,
                text=element.value,
                element=element,
                confidence=0.65,
            )
        focused = next(
            (name for name, region in regions.items() if region.focused), ""
        )
        overlay = next(
            (
                element.name
                for element in elements
                if element.role in {"dialog", "window"}
                and any(token in _element_blob(element) for token in ("dialog", "modal", "popup"))
            ),
            "",
        )
        app_name = _app_name_from_title(title)
        return AppState(
            app_name=app_name,
            window_title=title,
            view="dialog" if overlay else "workspace",
            focused_region=focused,
            visible_regions=regions,
            visible_overlay=overlay,
            confidence=0.5 if title else 0.25,
            window_width=width,
            window_height=height,
            elements=elements,
        )

    def available_skills(self, state: AppState) -> tuple[str, ...]:
        return ("inspect", "navigate", "edit", "verify")

    def validate_action(
        self,
        state: AppState,
        action: SemanticAction,
        arguments: Mapping[str, Any],
    ) -> tuple[bool, str]:
        region = state.region(action.expected_region)
        if action.expected_region and region is None:
            return False, (
                f"expected region '{action.expected_region}' is not present in the "
                "current active-window subtree"
            )
        if region and not _arguments_match_element(arguments, region.element):
            path = region.element.semantic_path if region.element else "unknown path"
            return False, (
                f"tool target does not resolve inside semantic region "
                f"'{action.expected_region}' ({path})"
            )
        return True, "semantic target is present in the active-window subtree"

    def verify_postcondition(
        self,
        previous_state: AppState,
        action: SemanticAction,
        new_state: AppState,
        goal: SendMessageGoal | None,
    ) -> tuple[bool, str]:
        if (
            action.expected_region
            and new_state.region(action.expected_region) is None
        ):
            return False, f"region '{action.expected_region}' disappeared"
        if action.intent in {"focus", "focus_region"}:
            return (
                new_state.focused_region == action.expected_region,
                f"focused region is '{new_state.focused_region or 'unknown'}'",
            )
        return (
            previous_state.fingerprint != new_state.fingerprint,
            (
                "UI state changed"
                if previous_state.fingerprint != new_state.fingerprint
                else "UI fingerprint is unchanged"
            ),
        )


class TelegramDesktopAdapter(GenericWindowsAdapter):
    """Semantic interpretation for Telegram Desktop's multi-pane chat layout."""

    name = "telegram"

    def detect(self, snapshot: str) -> float:
        title, _width, _height = _snapshot_window(snapshot)
        blob = f"{title}\n{snapshot[:4000]}".casefold()
        if "telegram" in blob:
            return 0.99
        return 0.0

    def parse_state(self, snapshot: str) -> AppState:
        title, width, height = _snapshot_window(snapshot)
        elements = _parse_elements(snapshot)
        regions: dict[str, RegionState] = {}

        search = _best_region(
            elements,
            name="global_search",
            positive=("search", "leftnavigation", "left navigation", "chat list"),
            negative=("messagecomposer", "message composer", "activechat", "active chat"),
            window_height=height,
        )
        composer = _best_region(
            elements,
            name="message_composer",
            positive=(
                "messagecomposer",
                "message composer",
                "write a message",
                "type a message",
                "activechat",
                "active chat",
                "compose",
            ),
            negative=("global search", "leftnavigation", "left navigation"),
            prefer_bottom=True,
            window_height=height,
        )
        if search:
            regions["global_search"] = search
        if composer:
            regions["message_composer"] = composer

        header_element = next(
            (
                element
                for element in elements
                if any(
                    token in _element_blob(element)
                    for token in ("chatheader", "chat header", "conversation header")
                )
                and element.name
                and re.sub(r"[^a-z0-9]+", "", element.name.casefold())
                not in {"chatheader", "header", "conversationheader"}
            ),
            None,
        )
        selected = header_element.name if header_element else _explicit_selected(snapshot)
        if selected:
            regions["chat_header"] = RegionState(
                name="chat_header",
                element=header_element,
                confidence=0.95 if header_element else 0.75,
            )
        history_element = next(
            (
                element
                for element in elements
                if any(
                    token in _element_blob(element)
                    for token in ("messagehistory", "message history", "chat history")
                )
            ),
            None,
        )
        if history_element:
            regions["message_history"] = RegionState(
                name="message_history",
                element=history_element,
                confidence=0.9,
            )
        send_element = next(
            (
                element
                for element in elements
                if element.role in {"button", "split button"}
                and any(
                    token in _element_blob(element)
                    for token in ("send", "messagecomposer", "message composer")
                )
            ),
            None,
        )
        if send_element:
            regions["send_button"] = RegionState(
                name="send_button",
                element=send_element,
                confidence=0.9,
            )
        result_element = next(
            (
                element
                for element in elements
                if any(
                    token in _element_blob(element)
                    for token in ("searchresult", "search result", "result list")
                )
                and element.x is not None
            ),
            None,
        )
        if result_element:
            regions["search_results"] = RegionState(
                name="search_results",
                element=result_element,
                confidence=0.82,
            )

        overlay_element = next(
            (
                element
                for element in elements
                if any(
                    token in _element_blob(element)
                    for token in ("search overlay", "popup", "dialog", "modal")
                )
            ),
            None,
        )
        search_text = search.text if search else ""
        search_mode = bool(
            overlay_element
            or (search and (search.focused or bool(search_text)))
            or re.search(r"(?im)^\s*search_mode\s*:\s*(?:true|yes|1)\s*$", snapshot)
        )
        focused = next(
            (name for name, region in regions.items() if region.focused), ""
        )
        view = "chat" if selected and composer else ("search" if search_mode else "chat_list")
        confidence = 0.97 if selected and composer else 0.82
        return AppState(
            app_name="telegram",
            window_title=title or "Telegram",
            view=view,
            selected_entity=selected,
            search_mode=search_mode,
            focused_region=focused,
            visible_regions=regions,
            visible_overlay=overlay_element.name if overlay_element else "",
            confidence=confidence,
            window_width=width,
            window_height=height,
            elements=elements,
        )

    def available_skills(self, state: AppState) -> tuple[str, ...]:
        skills = ["inspect", "open_chat"]
        if state.region("message_composer"):
            skills.append("send_message")
        return tuple(skills)

    def validate_action(
        self,
        state: AppState,
        action: SemanticAction,
        arguments: Mapping[str, Any],
    ) -> tuple[bool, str]:
        if action.expected_region == "search_results":
            label = str(arguments.get("label") or "").strip().casefold()
            candidates = [
                element
                for element in state.elements
                if element.x is not None
                and any(
                    token in _element_blob(element)
                    for token in ("searchresult", "search result", "result list")
                )
                and (not label or element.name.casefold() == label)
            ]
            if len(candidates) > 1 and arguments.get("loc") is None:
                return False, (
                    f"label {label!r} is ambiguous in search_results ({len(candidates)} "
                    "matches); choose a coordinate/ref whose result-row ancestry identifies "
                    "the intended entity"
                )
            if arguments.get("loc") is not None:
                if not any(
                    _arguments_match_element({"loc": arguments.get("loc")}, element)
                    for element in candidates
                ):
                    return False, "coordinate is outside the current search_results subtree"
                return True, "result coordinate and ancestry identify one current row"
        valid, reason = super().validate_action(state, action, arguments)
        if not valid:
            return valid, reason
        region = _normalize_region(action.expected_region)
        if state.selected_entity and region == "global_search":
            return False, (
                f"chat '{state.selected_entity}' is already open; contact-search "
                "actions are invalid after chat verification"
            )
        return True, reason

    def verify_postcondition(
        self,
        previous_state: AppState,
        action: SemanticAction,
        new_state: AppState,
        goal: SendMessageGoal | None,
    ) -> tuple[bool, str]:
        if goal is None:
            return super().verify_postcondition(previous_state, action, new_state, goal)
        intent = action.intent
        if intent in {"select_contact", "open_chat"}:
            passed = (
                new_state.selected_entity.casefold() == goal.target_name.casefold()
                and not new_state.search_mode
            )
            return passed, f"selected chat is '{new_state.selected_entity or 'unknown'}'"
        if intent in {"focus", "focus_composer"}:
            return (
                new_state.focused_region == "message_composer",
                f"focused region is '{new_state.focused_region or 'unknown'}'",
            )
        if intent in {"type_message", "draft_message"}:
            draft = new_state.region("message_composer")
            actual = draft.text if draft else ""
            return actual == goal.message_text, f"draft text is {actual!r}"
        if intent in {"send", "send_message", "commit"}:
            draft = new_state.region("message_composer")
            empty = draft is not None and not draft.text
            outgoing = _outgoing_message_visible(new_state, goal.message_text)
            return empty and outgoing, (
                f"composer_empty={empty}, outgoing_message_visible={outgoing}"
            )
        return super().verify_postcondition(previous_state, action, new_state, goal)


def _explicit_selected(snapshot: str) -> str:
    match = re.search(
        r"(?im)^\s*(?:selected_chat|selected_entity)\s*:\s*(.+?)\s*$",
        str(snapshot or ""),
    )
    return match.group(1).strip().strip('"') if match else ""


def _outgoing_message_visible(state: AppState, message_text: str) -> bool:
    expected = message_text.strip().casefold()
    if not expected:
        return False
    return any(
        element.name.strip().casefold() == expected
        and any(
            token in _element_blob(element)
            for token in ("outgoing", "messagehistory", "message history", "bubble")
        )
        for element in state.elements
    )


def _app_name_from_title(title: str) -> str:
    lowered = str(title or "").casefold()
    for app in (
        "telegram",
        "discord",
        "whatsapp",
        "slack",
        "visual studio code",
        "file explorer",
        "settings",
        "notepad",
    ):
        if app in lowered:
            return "vscode" if app == "visual studio code" else app.replace(" ", "_")
    return re.sub(r"[^a-z0-9]+", "_", lowered).strip("_") or "unknown_app"


def _arguments_match_element(
    arguments: Mapping[str, Any], element: UIElement | None
) -> bool:
    if element is None:
        return True
    loc = arguments.get("loc")
    if loc is not None and element.x is not None and element.y is not None:
        if isinstance(loc, str):
            match = _COORD_RE.search(loc)
            if match:
                return (
                    abs(int(match.group(1)) - element.x) <= 8
                    and abs(int(match.group(2)) - element.y) <= 8
                )
        if isinstance(loc, (list, tuple)) and len(loc) >= 2:
            try:
                return (
                    abs(int(loc[0]) - element.x) <= 8
                    and abs(int(loc[1]) - element.y) <= 8
                )
            except (TypeError, ValueError):
                return False
    label = str(arguments.get("label") or "").strip().casefold()
    if label:
        return label == element.name.casefold()
    return True


def _metadata(arguments: Mapping[str, Any]) -> dict[str, Any]:
    value = arguments.get("__ares", arguments.get("_ares", {}))
    return dict(value) if isinstance(value, Mapping) else {}


def _semantic_action(tool_name: str, arguments: Mapping[str, Any]) -> SemanticAction:
    meta = _metadata(arguments)
    short = str(tool_name).casefold().removeprefix(_WINDOWS_PREFIX)
    intent = str(meta.get("semantic_intent") or meta.get("purpose") or short).strip().casefold()
    generation = meta.get("ui_generation")
    try:
        parsed_generation = int(generation) if generation is not None else None
    except (TypeError, ValueError):
        parsed_generation = -1
    return SemanticAction(
        intent=re.sub(r"[^a-z0-9]+", "_", intent).strip("_"),
        entity=str(meta.get("entity") or "").strip(),
        phase=str(meta.get("phase") or "").strip().casefold(),
        expected_app=str(meta.get("expected_app") or "").strip().casefold(),
        expected_region=_normalize_region(str(meta.get("expected_region") or "")),
        purpose=str(meta.get("purpose") or "").strip().casefold(),
        text_owner=_normalize_region(str(meta.get("text_owner") or "")),
        ui_generation=parsed_generation,
        tool_name=tool_name,
    )


def _is_windows(tool_name: str) -> bool:
    return str(tool_name).casefold().startswith(_WINDOWS_PREFIX)


def _short_tool(tool_name: str) -> str:
    return str(tool_name).casefold().removeprefix(_WINDOWS_PREFIX)


def _is_snapshot(tool_name: str) -> bool:
    return _short_tool(tool_name) == "snapshot"


def _is_mutation(tool_name: str) -> bool:
    short = _short_tool(tool_name)
    return any(token in short for token in _MUTATION_TOKENS)


def _failed(result: str) -> bool:
    lowered = str(result or "").strip().casefold()
    return any(marker in lowered for marker in _SUCCESS_FAILURE_MARKERS)


class ComputerTaskController:
    """Application-aware guard and postcondition verifier for Windows MCP."""

    def __init__(
        self,
        adapters: tuple[AppAdapter, ...] | None = None,
        *,
        max_phase_failures: int = 3,
    ) -> None:
        self.adapters = adapters or (
            TelegramDesktopAdapter(),
            GenericWindowsAdapter(),
        )
        self.max_phase_failures = max(1, int(max_phase_failures))
        self._states: dict[str, ComputerTaskState] = {}
        self._traces: deque[ComputerTrace] = deque(maxlen=256)
        self._lock = RLock()

    @staticmethod
    def _key(session_id: str | None) -> str:
        return str(session_id or "default")

    def _state(self, session_id: str | None) -> ComputerTaskState:
        return self._states.setdefault(self._key(session_id), ComputerTaskState())

    def state_for(self, session_id: str | None) -> ComputerTaskState:
        with self._lock:
            return self._state(session_id)

    @property
    def traces(self) -> tuple[ComputerTrace, ...]:
        with self._lock:
            return tuple(self._traces)

    def begin_turn(
        self,
        session_id: str | None,
        user_input: str,
        *,
        routing_text: str = "",
    ) -> str:
        text = str(user_input or "").strip()
        routed = f"{text} {routing_text}".casefold()
        goal = self._parse_send_message_goal(text)
        desktop_active = bool(routing_text) or goal is not None or any(
            token in routed
            for token in (
                "desktop",
                "windows app",
                "computer use",
                "notepad",
                "telegram",
                "discord",
                "whatsapp",
                "vscode",
                "file explorer",
                "settings app",
            )
        )
        with self._lock:
            if not desktop_active:
                existing = self._states.get(self._key(session_id))
                if existing is not None:
                    existing.active = False
                    existing.pending_action = None
                    existing.observation_required = False
                return ""
            state = self._state(session_id)
            state.active = True
            state.task_id = uuid.uuid4().hex
            state.goal = goal
            state.phase = SendMessagePhase.OPEN_APP
            state.app_state = None
            state.completed_phases.clear()
            state.action_history.clear()
            state.pending_action = None
            state.observation_required = False
            state.consecutive_failures = 0
            state.recovery_reason = ""
            return self.guidance(session_id, text)

    @staticmethod
    def _parse_send_message_goal(text: str) -> SendMessageGoal | None:
        patterns = (
            r"(?is)\b(?:message|text)\s+(?:to\s+)?(?P<target>.+?)\s+"
            r"(?:saying|say|with(?: the)? message|that)\s+(?P<message>.+?)\s*$",
            r"(?is)\bsend(?: a)?(?: telegram| whatsapp| discord)? message to\s+"
            r"(?P<target>.+?)\s+(?:saying|say|with(?: the)? message|that|:)\s*"
            r"(?P<message>.+?)\s*$",
        )
        for pattern in patterns:
            match = re.search(pattern, text)
            if not match:
                continue
            target = match.group("target").strip().strip("\"'")
            message = match.group("message").strip().strip("\"'")
            if target and message and target.casefold() != message.casefold():
                return SendMessageGoal(target_name=target, message_text=message)
        return None

    def guidance(self, session_id: str | None, goal_text: str = "") -> str:
        state = self._state(session_id)
        goal = state.goal
        details = (
            f"Immutable target: {goal.target_name!r}. Immutable message: {goal.message_text!r}."
            if goal
            else f"Goal: {goal_text or 'continue the native desktop task'}."
        )
        return (
            "## Live Computer Task\n"
            f"{details}\n"
            "Prefer a connected semantic API/connector for messaging or structured app data; "
            "use visible desktop control when no such capability is available or the visible UI is required. "
            "Start with Windows Snapshot. Treat its UI tree as app/window/ancestor/role/region state, "
            "not as a flat list of labels. Every Windows Type or Click call must include the required "
            "`__ares` object: expected_app, expected_region, purpose, semantic_intent, phase, "
            "ui_generation, and (for Type) text_owner. Copy ui_generation from the compact state. "
            "Use text_owner=global_search only for an entity/query and text_owner=message_composer "
            "only for user-authored message content. Snapshot after each navigation or mutation and "
            "satisfy its postcondition before continuing. Never reuse an element from an older generation."
        )

    def before_call(
        self,
        session_id: str | None,
        tool_name: str,
        arguments: Mapping[str, Any],
    ) -> ComputerPreflight:
        if not _is_windows(tool_name) or _is_snapshot(tool_name) or not _is_mutation(tool_name):
            return ComputerPreflight()
        started = time.monotonic()
        action = _semantic_action(tool_name, arguments)
        with self._lock:
            state = self._state(session_id)
            if not state.active:
                return ComputerPreflight()
            app_state = state.app_state
            bootstrap_launch = "launch" in _short_tool(tool_name)
            if app_state is None:
                meta = _metadata(arguments)
                if (
                    bootstrap_launch
                    and action.expected_app
                    and action.expected_region == "application"
                    and action.purpose
                    and action.phase == SendMessagePhase.OPEN_APP.value
                    and str(meta.get("semantic_intent") or "").strip()
                ):
                    self._trace(
                        state,
                        action=action,
                        precondition="passed: bootstrap application launch",
                        duration_ms=(time.monotonic() - started) * 1000,
                    )
                    return ComputerPreflight(action=action)
                return self._reject(
                    state,
                    action,
                    "No semantic desktop state is available. Take Windows Snapshot before acting.",
                    started,
                )
            if state.observation_required:
                return self._reject(
                    state,
                    action,
                    "The previous desktop mutation has not been verified. "
                    "Take a fresh Snapshot before another action.",
                    started,
                )
            if action.ui_generation is None and not bootstrap_launch:
                return self._reject(
                    state,
                    action,
                    f"Missing __ares.ui_generation; use current generation {state.ui_generation}.",
                    started,
                )
            if action.ui_generation != state.ui_generation:
                return self._reject(
                    state,
                    action,
                    f"Stale UI reference from generation {action.ui_generation}; current generation is "
                    f"{state.ui_generation}. Take a fresh Snapshot and resolve the target again.",
                    started,
                )
            if (
                not action.expected_app
                or not action.expected_region
                or not action.purpose
                or not action.phase
                or not str(_metadata(arguments).get("semantic_intent") or "").strip()
            ):
                return self._reject(
                    state,
                    action,
                    "Click/Type requires __ares.expected_app, expected_region, purpose, "
                    "semantic_intent, and phase.",
                    started,
                )
            if action.phase != state.phase.value:
                return self._reject(
                    state,
                    action,
                    f"Action phase '{action.phase}' is stale or incorrect; current phase is "
                    f"'{state.phase.value}'.",
                    started,
                )
            expected_app = action.expected_app.replace(" ", "_")
            actual_app = app_state.app_name.replace(" ", "_")
            if expected_app not in {actual_app, "current_app"}:
                return self._reject(
                    state,
                    action,
                    f"Expected app '{action.expected_app}' but focused app is '{app_state.app_name}'.",
                    started,
                )
            if (
                state.goal is not None
                and app_state.selected_entity.casefold()
                == state.goal.target_name.casefold()
                and action.expected_region in {"global_search", "search_results"}
            ):
                return self._reject(
                    state,
                    action,
                    f"Target chat '{state.goal.target_name}' is already verified. "
                    "All contact-search actions and old search references are invalid.",
                    started,
                )
            short = _short_tool(tool_name)
            if "type" in short:
                if not action.text_owner:
                    return self._reject(
                        state,
                        action,
                        "Type requires __ares.text_owner so navigation text cannot become content.",
                        started,
                    )
                if action.text_owner != action.expected_region:
                    return self._reject(
                        state,
                        action,
                        f"Text owner '{action.text_owner}' does not match expected region "
                        f"'{action.expected_region}'.",
                        started,
                    )
                region = app_state.region(action.expected_region)
                if (
                    region is None
                    or not region.focused
                    or app_state.focused_region != action.expected_region
                ):
                    return self._reject(
                        state,
                        action,
                        f"Type expected focused subtree '{action.expected_region}', but "
                        f"focused region is '{app_state.focused_region or 'unknown'}'. "
                        "Focus the intended region, take a fresh Snapshot, then type.",
                        started,
                    )
                ownership_error = self._text_ownership_error(
                    state.goal, app_state, action, str(arguments.get("text") or "")
                )
                if ownership_error:
                    return self._reject(state, action, ownership_error, started)

            adapter = self._adapter_for_state(app_state)
            valid, reason = adapter.validate_action(app_state, action, arguments)
            if not valid:
                return self._reject(state, action, reason, started)

            repeated = sum(
                1
                for signature, fingerprint in state.action_history
                if signature == action.signature and fingerprint == app_state.fingerprint
            )
            if repeated >= 2:
                state.recovery_reason = (
                    "same semantic action repeated twice with an unchanged UI fingerprint"
                )
                return self._reject(
                    state,
                    action,
                    f"Semantic loop detected for '{action.intent}'. Stop this strategy and "
                    "take a full Snapshot to reclassify state.",
                    started,
                )
            self._trace(
                state,
                action=action,
                precondition="passed",
                fingerprint=app_state.fingerprint,
                duration_ms=(time.monotonic() - started) * 1000,
            )
            return ComputerPreflight(action=action)

    def _text_ownership_error(
        self,
        goal: SendMessageGoal | None,
        app_state: AppState,
        action: SemanticAction,
        text: str,
    ) -> str:
        if goal is None:
            return ""
        owner = action.text_owner
        if owner == "global_search":
            if app_state.selected_entity.casefold() == goal.target_name.casefold():
                return (
                    f"Target chat '{goal.target_name}' is already verified; global search "
                    "text is now prohibited."
                )
            if text != goal.target_name:
                return (
                    f"global_search owns only target_name {goal.target_name!r}; "
                    "message content cannot be typed there."
                )
        if owner == "message_composer":
            if text == goal.target_name:
                return (
                    f"Rejected target_name {goal.target_name!r} in message_composer. "
                    f"This region owns only message_text {goal.message_text!r}."
                )
            if text != goal.message_text:
                return (
                    f"message_composer owns exactly message_text {goal.message_text!r}; "
                    "contact/search text cannot be typed there."
                )
            if app_state.selected_entity.casefold() != goal.target_name.casefold():
                return (
                    f"Cannot type message: selected chat is "
                    f"{app_state.selected_entity or 'unknown'!r}, expected {goal.target_name!r}."
                )
        return ""

    def _reject(
        self,
        state: ComputerTaskState,
        action: SemanticAction,
        reason: str,
        started: float,
    ) -> ComputerPreflight:
        state.consecutive_failures += 1
        state.recovery_reason = reason
        app_state = state.app_state
        self._trace(
            state,
            action=action,
            precondition=f"rejected: {reason}",
            fingerprint=app_state.fingerprint if app_state else "",
            recovery=reason,
            duration_ms=(time.monotonic() - started) * 1000,
        )
        suffix = ""
        if state.consecutive_failures >= self.max_phase_failures:
            suffix = (
                f" Phase failure limit ({self.max_phase_failures}) reached; stop instead "
                "of trying unrelated controls."
            )
        return ComputerPreflight(
            allowed=False,
            message=f"Error: Computer semantic precondition rejected the action. {reason}{suffix}",
            action=action,
        )

    def after_action(
        self,
        session_id: str | None,
        tool_name: str,
        arguments: Mapping[str, Any],
        result: str,
        action: SemanticAction | None = None,
    ) -> str:
        if not _is_windows(tool_name) or not _is_mutation(tool_name):
            return str(result)
        with self._lock:
            state = self._state(session_id)
            if not state.active:
                return str(result)
            if action is None:
                action = _semantic_action(tool_name, arguments)
            if _failed(result):
                state.consecutive_failures += 1
                state.recovery_reason = "Windows actuator reported failure"
                return str(result)
            app_state = state.app_state
            if app_state is not None:
                state.action_history.append((action.signature, app_state.fingerprint))
            state.pending_action = action
            state.pending_started_at = time.monotonic()
            state.observation_required = True
            return (
                f"{result}\n\nComputer controller: action dispatched; postcondition is pending. "
                "Take a fresh Windows Snapshot before the next mutation or claiming success."
            )

    def after_snapshot(
        self,
        session_id: str | None,
        snapshot: str,
        *,
        cached: bool = False,
    ) -> str:
        if _failed(snapshot):
            return str(snapshot)
        with self._lock:
            state = self._state(session_id)
            if not state.active:
                return str(snapshot)
            if "UI Tree:" not in str(snapshot):
                return (
                    f"{snapshot}\n\nComputer controller: this capture has no UI tree, so semantic "
                    "state and element generations were not changed. Take Windows Snapshot with "
                    "use_ui_tree=true before a guarded Type or Click."
                )
            previous = state.app_state
            adapter = max(self.adapters, key=lambda item: item.detect(snapshot))
            parsed = adapter.parse_state(snapshot)
            if not cached:
                state.ui_generation += 1
            old_phase = state.phase
            pending = state.pending_action
            postcondition = ""
            if pending is not None and previous is not None:
                passed, detail = adapter.verify_postcondition(
                    previous, pending, parsed, state.goal
                )
                postcondition = ("passed: " if passed else "failed: ") + detail
                if passed:
                    state.consecutive_failures = 0
                    state.recovery_reason = ""
                else:
                    state.consecutive_failures += 1
                    state.recovery_reason = detail
                self._trace(
                    state,
                    action=pending,
                    precondition="passed",
                    postcondition=postcondition,
                    fingerprint=parsed.fingerprint,
                    transition=f"{old_phase.value}->{state.phase.value}",
                    recovery="" if passed else detail,
                    duration_ms=max(
                        0.0, (time.monotonic() - state.pending_started_at) * 1000
                    ),
                )
            state.app_state = parsed
            state.pending_action = None
            state.observation_required = False
            self._advance_phase(state)
            transition = (
                f"{old_phase.value}->{state.phase.value}"
                if old_phase != state.phase
                else state.phase.value
            )
            recovery = self._detect_bad_draft(state)
            if recovery:
                state.recovery_reason = recovery
            elif state.recovery_reason.startswith("same semantic action repeated"):
                # A fresh full-tree observation is the reclassification
                # boundary. The failed strategy is discarded, while the
                # consecutive-failure budget remains visible.
                state.action_history.clear()
                state.recovery_reason = "state reclassified from a fresh full Snapshot"

            projection = parsed.compact_projection()
            projection.update(
                {
                    "task_id": state.task_id,
                    "phase": state.phase.value,
                    "ui_generation": state.ui_generation,
                    "completed_phases": sorted(item.value for item in state.completed_phases),
                    "available_skills": list(adapter.available_skills(parsed)),
                    "state_transition": transition,
                    "postcondition": postcondition or None,
                    "recovery_reason": state.recovery_reason or None,
                }
            )
            return (
                "## Ares Compact Computer State (authoritative)\n"
                f"```json\n{json.dumps(projection, ensure_ascii=False, indent=2)}\n```\n\n"
                "The UI tree below is actuator evidence. Use only elements whose app, ancestry, "
                "region, and generation agree with the compact state.\n\n"
                f"{snapshot}"
            )

    def _advance_phase(self, state: ComputerTaskState) -> None:
        app = state.app_state
        goal = state.goal
        if app is None:
            return
        if goal is None or app.app_name != "telegram":
            state.phase = SendMessagePhase.VERIFY_CHAT
            return
        target_selected = app.selected_entity.casefold() == goal.target_name.casefold()
        composer = app.region("message_composer")
        search = app.region("global_search")
        if target_selected and composer:
            state.completed_phases.update(
                {
                    SendMessagePhase.OPEN_APP,
                    SendMessagePhase.OPEN_GLOBAL_SEARCH,
                    SendMessagePhase.SEARCH_CONTACT,
                    SendMessagePhase.SELECT_CONTACT,
                    SendMessagePhase.VERIFY_CHAT,
                }
            )
            if (
                composer.text == goal.target_name
                and goal.target_name != goal.message_text
            ):
                state.phase = SendMessagePhase.FOCUS_COMPOSER
            elif composer.text == goal.message_text:
                state.completed_phases.update(
                    {
                        SendMessagePhase.FOCUS_COMPOSER,
                        SendMessagePhase.TYPE_MESSAGE,
                        SendMessagePhase.VERIFY_DRAFT,
                    }
                )
                state.phase = SendMessagePhase.SEND
            elif not composer.text and _outgoing_message_visible(app, goal.message_text):
                state.completed_phases.update(set(SendMessagePhase))
                state.phase = SendMessagePhase.COMPLETE
            elif not composer.text and state.phase in {
                SendMessagePhase.SEND,
                SendMessagePhase.VERIFY_SENT,
            }:
                state.completed_phases.add(SendMessagePhase.SEND)
                state.phase = SendMessagePhase.VERIFY_SENT
            elif composer.focused:
                state.completed_phases.add(SendMessagePhase.FOCUS_COMPOSER)
                state.phase = SendMessagePhase.TYPE_MESSAGE
            else:
                state.phase = SendMessagePhase.FOCUS_COMPOSER
            return
        if target_selected:
            state.phase = SendMessagePhase.VERIFY_CHAT
        elif app.search_mode and search and search.text == goal.target_name:
            state.completed_phases.update(
                {
                    SendMessagePhase.OPEN_APP,
                    SendMessagePhase.OPEN_GLOBAL_SEARCH,
                    SendMessagePhase.SEARCH_CONTACT,
                }
            )
            state.phase = SendMessagePhase.SELECT_CONTACT
        elif app.search_mode:
            state.completed_phases.update(
                {SendMessagePhase.OPEN_APP, SendMessagePhase.OPEN_GLOBAL_SEARCH}
            )
            state.phase = SendMessagePhase.SEARCH_CONTACT
        elif app.app_name == "telegram":
            state.completed_phases.add(SendMessagePhase.OPEN_APP)
            state.phase = SendMessagePhase.OPEN_GLOBAL_SEARCH
        else:
            state.phase = SendMessagePhase.OPEN_APP

    @staticmethod
    def _detect_bad_draft(state: ComputerTaskState) -> str:
        goal = state.goal
        app = state.app_state
        if not goal or not app:
            return ""
        composer = app.region("message_composer")
        if (
            composer
            and composer.text == goal.target_name
            and goal.target_name != goal.message_text
        ):
            return (
                f"message_composer incorrectly contains target_name {goal.target_name!r}. "
                f"Clear that region, then type immutable message_text {goal.message_text!r}; "
                "do not reopen contact search."
            )
        return ""

    def _adapter_for_state(self, state: AppState) -> AppAdapter:
        return next(
            (adapter for adapter in self.adapters if adapter.name == state.app_name),
            self.adapters[-1],
        )

    def _trace(
        self,
        state: ComputerTaskState,
        *,
        action: SemanticAction,
        precondition: str,
        postcondition: str = "",
        fingerprint: str = "",
        transition: str = "",
        recovery: str = "",
        duration_ms: float = 0.0,
    ) -> None:
        app = state.app_state.app_name if state.app_state else action.expected_app
        self._traces.append(
            ComputerTrace(
                task_id=state.task_id,
                app=app,
                phase=state.phase.value,
                semantic_action=action.signature,
                precondition_result=precondition,
                postcondition_result=postcondition,
                ui_fingerprint=fingerprint,
                state_transition=transition,
                recovery_reason=recovery,
                duration_ms=duration_ms,
            )
        )
