import json
import threading
import time
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from functools import lru_cache
from pathlib import Path

import numpy as np
from rich.console import Console

from config import settings
from tools.registry import get_tools, execute_tool, get_tool_schemas, get_tool
from .embedder import embed
from .llm import NIMClient
from .router import ToolRouter, _FILE_GENERATING_TOOLS
from .tokenizer import count_messages_tokens
from .validator import ToolValidator
from .verifier import Verifier
from memory.brain import Brain
from memory.summaries import SummaryStore

console = Console()

PERSONA_PATH = Path(__file__).resolve().parent.parent / "persona.md"
TOOL_POLICY_PATH = Path(__file__).resolve().parent.parent / "tool_policy.md"
ROUTING_POLICY_PATH = Path(__file__).resolve().parent.parent / "routing_policy.md"


# ─── Pre-computed routing text embeddings ────────────────────────────────────
# All the routing policy texts that get compared against q_emb are embedded
# once in a single batch on first access. This eliminates repeated ONNX
# inference calls that were the main source of the pre-answer gap.

class _RouteEmbeddings:
    """Lazy singleton that pre-embeds all routing policy texts in one batch."""
    _instance = None
    _vectors: dict[str, np.ndarray] = {}

    @classmethod
    def get(cls) -> "_RouteEmbeddings":
        if cls._instance is None:
            cls._instance = cls()
            cls._instance._build()
        return cls._instance

    def _build(self):
        from .router import _load_routing_section, ROUTING_POLICY_PATH as RP
        texts = {
            "banter": _load_routing_policy_section_raw("Conversational Banter Text", "The user is reacting, joking, or continuing casual chat."),
            "actionable": _load_routing_policy_section_raw("Actionable Request Text", "The user wants something done."),
            "memory_recall": _load_routing_policy_section_raw("Memory Recall Text", "The user asks what the assistant remembers about them."),
            "no_memory_small_talk": _load_routing_policy_section_raw("No Memory Small Talk Text", "The user is greeting or chatting casually."),
            "broad_recall": _load_routing_policy_section_raw("Broad Memory Recall Text", "The user asks for a broad overview of remembered facts."),
            "specific_recall": _load_routing_policy_section_raw("Specific Memory Recall Text", "The user asks for one particular remembered fact."),
            "proactive_memory": _load_routing_policy_section_raw("Proactive Memory Context Text", "The user is casually checking in or continuing personal context where one relevant remembered fact would help."),
            "followup": _load_routing_policy_section_raw("Context Follow-Up Text", "The user is asking to continue the previous result."),
            "new_topic": _load_routing_policy_section_raw("New Topic Text", "The user is giving a fresh standalone topic."),
            "system_control": _load_routing_policy_section_raw("Local System Control Text", "The user wants to change volume, brightness, or media."),
            "incomplete": _load_routing_policy_section_raw("Incomplete Utterance Text", "The user trailed off or left the object unstated."),
            "correction": _load_routing_policy_section_raw("Action Correction Text", "The user says the previous action was wrong."),
        }
        # Batch embed all texts in one ONNX call.
        keys = list(texts.keys())
        values = [texts[k] for k in keys]
        embeddings = embed(values)
        if embeddings.ndim == 1:
            embeddings = embeddings.reshape(1, -1)
        for i, key in enumerate(keys):
            self._vectors[key] = embeddings[i]

    def v(self, name: str) -> np.ndarray:
        return self._vectors[name]


def _load_routing_policy_section_raw(heading: str, fallback: str) -> str:
    """Load a section from routing_policy.md (no caching wrapper needed here)."""
    if not ROUTING_POLICY_PATH.exists():
        return fallback
    targets = {f"# {heading}".casefold(), f"## {heading}".casefold()}
    lines = []
    in_section = False
    for raw_line in ROUTING_POLICY_PATH.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if line.startswith("# ") or line.startswith("## "):
            in_section = line.casefold() in targets
            continue
        if in_section and line:
            lines.append(line)
    return " ".join(lines).strip() or fallback


def _debug_safe(text) -> str:
    return str(text).encode("ascii", errors="backslashreplace").decode("ascii")


def _print_assistant_content(content: str, emit_chunk=None) -> None:
    if not content:
        return
    print(content, flush=True)
    if emit_chunk:
        emit_chunk(content)
        emit_chunk("\n")


@lru_cache(maxsize=1)
def _load_persona() -> str:
    """Cached persona loading."""
    if PERSONA_PATH.exists():
        return PERSONA_PATH.read_text(encoding="utf-8").strip()
    return "You are KING, an AI assistant. Respond naturally in plain language."


@lru_cache(maxsize=1)
def _load_tool_policy() -> str:
    """Cached tool policy loading from markdown."""
    if TOOL_POLICY_PATH.exists():
        return TOOL_POLICY_PATH.read_text(encoding="utf-8").strip()
    return (
        "Use selected tools for actionable requests. Never claim an action happened "
        "unless an actual tool result proves it. Ask for missing targets instead of guessing."
    )


def _chat_polish_policy_path() -> Path:
    path = Path(settings.chat_polish_policy_file)
    if not path.is_absolute():
        path = Path(__file__).resolve().parent.parent / path
    return path


@lru_cache(maxsize=8)
def _load_chat_polish_section(heading: str, fallback: str) -> str:
    path = _chat_polish_policy_path()
    if not path.exists():
        return fallback
    target = f"## {heading}".lower()
    lines = []
    in_section = False
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if line.startswith("## "):
            if in_section:
                break
            in_section = line.lower() == target
            continue
        if in_section and line:
            lines.append(line)
    return " ".join(lines) or fallback


def _load_routing_policy_section(heading: str, fallback: str) -> str:
    if not ROUTING_POLICY_PATH.exists():
        return fallback
    target = f"## {heading}".lower()
    lines = []
    in_section = False
    for raw_line in ROUTING_POLICY_PATH.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if line.startswith("## "):
            if in_section:
                break
            in_section = line.lower() == target
            continue
        if in_section and line:
            lines.append(line)
    return " ".join(lines) or fallback


def _time_of_day(now: datetime) -> str:
    hour = now.hour
    if 5 <= hour < 12:
        return "morning"
    if 12 <= hour < 17:
        return "afternoon"
    if 17 <= hour < 21:
        return "evening"
    return "night"


def _local_time_context(now: datetime | None = None) -> str:
    current = (now or datetime.now()).astimezone()
    time_of_day = _time_of_day(current)
    return (
        f"Current local date and time: {current.strftime('%A, %B %d, %Y  %I:%M:%S %p  %Z')}.\n"
        f"Current local time of day: {time_of_day}.\n"
        "When greeting or correcting the user's wording about morning, afternoon, evening, or night, "
        "use this current local time of day exactly. Do not use a different time-of-day greeting."
    )


def _system_header() -> str:
    return f"{_load_persona()}\n{_local_time_context()}"

MAX_CONTEXT_TOKENS = 6000
MAX_TOOL_RESULT_CHARS = settings.tool_result_max_chars
TYPING_SPEED = max(0.0, settings.typing_speed_seconds)

def _find_json(text: str) -> str | None:
    """Extract the outermost JSON object from text using brace-depth tracking."""
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    for i, ch in enumerate(text[start:], start):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start:i+1]
    return None


def _extract_json_tool_shape(text: str) -> tuple[str | None, dict | None, bool]:
    stripped = (text or "").strip()
    json_str = stripped if stripped.startswith("{") else _find_json(stripped)
    if not json_str:
        return None, None, False
    try:
        parsed = json.loads(json_str)
    except (json.JSONDecodeError, TypeError):
        return None, None, False
    if not isinstance(parsed, dict):
        return None, None, False

    func_name = None
    func_args = None

    function = parsed.get("function")
    if isinstance(function, dict) and function.get("name"):
        func_name = function.get("name")
        func_args = function.get("arguments", "{}")
    elif "name" in parsed and "parameters" in parsed:
        func_name = parsed["name"]
        func_args = parsed["parameters"]
    elif "name" in parsed and "arguments" in parsed:
        func_name = parsed["name"]
        func_args = parsed["arguments"]

    if not isinstance(func_name, str) or not func_name.strip():
        return None, None, False

    if isinstance(func_args, str):
        try:
            func_args = json.loads(func_args)
        except (json.JSONDecodeError, TypeError):
            func_args = {}

    if func_args is None:
        func_args = {}

    return func_name.strip(), func_args if isinstance(func_args, dict) else {}, True


def _try_parse_json_tool_call(text: str, schemas: list) -> tuple:
    """Detect JSON-formatted tool call leaked as text content.
    Returns (tool_call_dict, None) on success,
    (None, error_message) if parsed but tool unknown,
    (None, None) if not a JSON tool call."""
    # Some models leak their native tool-call token format as plain text, e.g.
    #   [TOOL_CALLS]project_status{}
    #   [TOOL_CALLS]reminder{"task": "x", "when": "5pm"}
    # Recover that shape first, then fall back to JSON-object detection.
    token_name, token_args, is_token = _extract_token_tool_shape(text)
    if is_token and token_name:
        func_name, func_args = token_name, token_args
    else:
        func_name, func_args, is_tool_shape = _extract_json_tool_shape(text)
        if not is_tool_shape or not func_name:
            return None, None

    known = {t["function"]["name"].lower(): t["function"]["name"] for t in schemas}
    actual = known.get(func_name.lower())

    if not actual:
        available = ", ".join(sorted(known.values()))
        msg = f"'{func_name}' is not an available tool. Available: {available}. Use one of them."
        return None, msg

    func_args = _repair_schema_argument_names(actual, func_args)
    return {
        "id": f"call_{int(time.time() * 1000)}",
        "name": actual,
        "arguments": json.dumps(func_args),
    }, None


def _looks_like_tool_token_prefix(text: str) -> bool:
    """True while streamed content could still become a `[TOOL_CALLS]` token.

    Holds back emission of the leading characters so a leaked native tool-call
    token is never shown to the user mid-stream. Conservative: only fires while
    the content's start is a prefix of the marker, or the marker is present.
    """
    stripped = str(text or "").lstrip()
    if not stripped:
        return False
    marker = "[TOOL_CALLS]"
    head = stripped[: len(marker)]
    return marker in stripped or marker.startswith(head)


def _extract_token_tool_shape(text: str) -> tuple:
    """Parse a leaked native tool-call token like `[TOOL_CALLS]name{...}`.

    Returns (name, args_dict, True) when the text contains that token shape, else
    (None, None, False). Handles missing or malformed argument objects by falling
    back to an empty dict, so a bare `[TOOL_CALLS]project_status` still recovers.
    """
    stripped = str(text or "").strip()
    marker = "[TOOL_CALLS]"
    pos = stripped.find(marker)
    if pos == -1:
        return None, None, False
    rest = stripped[pos + len(marker):].strip()
    if not rest:
        return None, None, False
    # Tool name runs until the first '{', '(' or whitespace.
    name_chars = []
    for ch in rest:
        if ch.isalnum() or ch in ("_", "-", "."):
            name_chars.append(ch)
        else:
            break
    name = "".join(name_chars).strip()
    if not name:
        return None, None, False
    args: dict = {}
    brace = rest.find("{")
    if brace != -1:
        obj = _find_json(rest[brace:])
        if obj:
            try:
                parsed = json.loads(_sanitize_tool_arguments(obj))
                if isinstance(parsed, dict):
                    args = parsed
            except (json.JSONDecodeError, TypeError, ValueError):
                args = {}
    return name, args, True


def _sanitize_tool_arguments(raw: str) -> str:
    """Return a JSON-valid arguments string from a (possibly corrupted) stream.

    Streamed tool-call argument deltas can occasionally concatenate into invalid
    JSON (duplicated or overlapping fragments), e.g.
        {"a": "x, "b": false{"a": "x", "b": false}
    A malformed string must never be stored in conversation history: the chat API
    rejects the whole request with a 400 on every later turn, permanently breaking
    the session. This repairs the common cases and falls back to "{}".
    """
    text = str(raw or "").strip()
    if not text:
        return "{}"
    # Fast path: already valid.
    try:
        json.loads(text)
        return text
    except (json.JSONDecodeError, TypeError):
        pass
    # The model often re-emits the full object after a corrupted partial. Try the
    # last complete top-level {...} object in the string.
    last = _find_last_json_object(text)
    if last is not None:
        try:
            json.loads(last)
            return last
        except (json.JSONDecodeError, TypeError):
            pass
    # Try the first complete top-level object.
    first = _find_json(text)
    if first is not None:
        try:
            json.loads(first)
            return first
        except (json.JSONDecodeError, TypeError):
            pass
    # Unrecoverable: empty args keep history valid so the session survives.
    return "{}"


def _find_last_json_object(text: str) -> str | None:
    """Return the last balanced top-level {...} object substring, or None."""
    end = text.rfind("}")
    while end != -1:
        depth = 0
        for i in range(end, -1, -1):
            ch = text[i]
            if ch == "}":
                depth += 1
            elif ch == "{":
                depth -= 1
                if depth == 0:
                    return text[i:end + 1]
        end = text.rfind("}", 0, end)
    return None


def _repair_schema_argument_names(tool_name: str, args: dict) -> dict:
    if not isinstance(args, dict):
        return {}
    registered = get_tool(tool_name)
    if not registered:
        return args
    parameters = registered.get("parameters", {})
    properties = parameters.get("properties", {}) if isinstance(parameters, dict) else {}
    if not isinstance(properties, dict) or not properties:
        return args
    accepted = set(properties.keys())
    unknown = [key for key in args.keys() if key not in accepted]
    required = parameters.get("required", []) if isinstance(parameters, dict) else []
    missing_required = [
        key
        for key in required
        if key in accepted and key not in args
    ]
    if len(unknown) == 1 and len(missing_required) == 1:
        repaired = dict(args)
        repaired[missing_required[0]] = repaired.pop(unknown[0])
        return repaired
    return args


def _json_tool_leak_message(text: str, schemas: list) -> str | None:
    func_name, _, is_tool_shape = _extract_json_tool_shape(text)
    if not is_tool_shape or not func_name:
        return None

    known = {t["function"]["name"].lower(): t["function"]["name"] for t in schemas}
    if known and known.get(func_name.lower()):
        return (
            f"I produced a text-formatted call for `{known[func_name.lower()]}` "
            "instead of executing it, sir. Please retry that request."
        )
    if get_tool(func_name):
        return (
            f"I could not execute `{func_name}` because that tool was not selected "
            "for this turn, sir."
        )
    return f"I could not execute `{func_name}` because it is not registered as a KING tool, sir."


def _has_backtick_tool_call(text: str, schemas: list) -> bool:
    """Detect backtick-quoted tool calls by selected schema similarity."""
    blocks = []
    start = None
    for idx, char in enumerate(text):
        if char != "`":
            continue
        if start is None:
            start = idx + 1
        else:
            blocks.append(text[start:idx])
            start = None
    if not blocks:
        return False
    tool_names = {t["function"]["name"] for t in schemas}
    all_params = set()
    schema_texts = []
    for schema in schemas:
        function = schema.get("function", {})
        params = function.get("parameters", {}).get("properties", {})
        all_params.update(params.keys())
        parts = [function.get("name", ""), function.get("description", "")]
        for param_name, param_schema in params.items():
            parts.append(param_name)
            parts.append(param_schema.get("description", ""))
        registered = get_tool(function.get("name", ""))
        if registered:
            parts.extend(registered.get("examples", []))
        schema_texts.extend(part for part in parts if part)

    for block in blocks:
        stripped = block.strip()
        for name in tool_names:
            if stripped.startswith(f"{name}("):
                return True
        if not stripped or "\n" in stripped or not schema_texts:
            continue
        # Skip if it is exactly a tool name or parameter name (purely a reference, not an execution)
        if stripped in tool_names or stripped in all_params:
            continue
        try:
            block_emb = embed(stripped)
            schema_embs = embed(schema_texts)
            if getattr(schema_embs, "ndim", 1) == 1:
                schema_embs = schema_embs.reshape(1, -1)
            similarity = float(np.max(np.dot(schema_embs, block_emb)))
        except Exception:
            continue
        if similarity >= settings.backtick_tool_similarity_threshold:
            return True
    return False


def _progressive_disclosure_tool_names() -> list[str]:
    if not settings.progressive_disclosure_enabled:
        return []
    return [
        name.strip()
        for name in settings.progressive_disclosure_tools.split(",")
        if name.strip()
    ]


def _should_expose_progressive_disclosure(selected_tools: list, router_decision: dict) -> bool:
    if not settings.progressive_disclosure_enabled:
        return False
    if selected_tools:
        return False
    return router_decision.get("reason") == "below_tool_threshold"


def _loaded_tools_from_result(result) -> list[dict]:
    """Extract loaded tool entries (name + optional tool_slug) from a load_tool
    result (structured or legacy JSON)."""
    text = result if isinstance(result, str) else _tool_result_content(result)
    try:
        parsed = json.loads(text)
    except (TypeError, json.JSONDecodeError):
        return []
    if not isinstance(parsed, dict):
        return []
    payload = parsed.get("result", parsed)
    if not isinstance(payload, dict):
        return []
    loaded = payload.get("loaded")
    entries = []
    if isinstance(loaded, list):
        for item in loaded:
            if isinstance(item, dict) and item.get("name"):
                entry = {"name": str(item["name"])}
                if item.get("tool_slug"):
                    entry["tool_slug"] = str(item["tool_slug"])
                entries.append(entry)
    if entries:
        return entries
    names = payload.get("loaded_names")
    if isinstance(names, list):
        return [{"name": str(n)} for n in names if str(n).strip()]
    return []


def _discovered_tools_from_result(result, limit: int = 1) -> list[dict]:
    text = result if isinstance(result, str) else _tool_result_content(result)
    try:
        parsed = json.loads(text)
    except (TypeError, json.JSONDecodeError):
        return []
    if not isinstance(parsed, dict):
        return []
    payload = parsed.get("result", parsed)
    if not isinstance(payload, dict):
        return []
    matches = payload.get("matches")
    if not isinstance(matches, list):
        return []
    entries = []
    for item in matches:
        if not isinstance(item, dict):
            continue
        name = str(item.get("backing_tool") or item.get("name") or "").strip()
        if not name:
            continue
        entry = {"name": name}
        slug = str(item.get("tool_slug") or "").strip()
        if slug:
            entry["tool_slug"] = slug
        entries.append(entry)
        if len(entries) >= max(1, limit):
            break
    return entries


def _build_system_prompt(
    selected_tools,
    recent_action_context: str = "",
    memory_facts: str = "",
    summary_context: str = "",
    conversational_turn: bool = False,
    broad_recall: bool = False,
    capability_hint: dict | None = None,
):
    lines = [_system_header(), "", _load_tool_policy()]
    if summary_context:
        lines.append("")
        lines.append(summary_context)
    if memory_facts:
        lines.append("")
        lines.append(_load_chat_polish_section("Memory Presentation Rules", "State known facts naturally."))
        lines.append("")
        if broad_recall:
            lines.append(_load_chat_polish_section(
                "Broad Recall Rules",
                "When the user asks for everything you know about them, list every fact provided "
                "for this turn, grouped naturally. Do not omit, summarize away, or cap the facts.",
            ))
        else:
            lines.append(_load_chat_polish_section(
                "Proactive Engagement Rules",
                "When facts are listed, weave at most one relevant ongoing fact into casual replies.",
            ))
        lines.append("Known facts for this turn:")
        lines.append(memory_facts)
    if conversational_turn:
        lines.append("")
        lines.append(_load_chat_polish_section(
            "Conversational Response Rules",
            "Answer naturally without mentioning tools or memory systems.",
        ))
    if recent_action_context:
        lines.append("")
        lines.append("Recent actionable context:")
        lines.append(recent_action_context)
        lines.append(
            "For an underspecified follow-up, reuse the latest relevant target, query, URL, "
            "file, command, or failed tool result from this context. Do not append the "
            "follow-up wording itself as a new search term. Do not repeat an older successful "
            "action when the latest context is a different failed action."
        )
    if selected_tools:
        lines.append("")
        lines.append("Available tools:")
        for t in selected_tools:
            params = ", ".join(t["parameters"]["properties"])
            lines.append(f"- {t['name']}({params}): {t['description']}")
        lines.append("")
        lines.append(
            "OUTPUT FORMAT: When you need to perform an action, output a JSON tool call "
            "on its own line: {\"name\": \"tool_name\", \"parameters\": {\"param\": \"value\"}}. "
            "Example: {\"name\": \"terminal\", \"parameters\": {\"command\": \"start notepad\"}}. "
            "The system will execute it. Do not describe what the tool does in text."
        )
        if any(t.get("name") == "system_control" for t in selected_tools):
            lines.append(
                "For volume or brightness on this PC, call system_control with action volume_up, "
                "volume_down, brightness_up, or brightness_down. Omit config_path."
            )
        if capability_hint and capability_hint.get("tool") and capability_hint.get("args"):
            hint_tool = capability_hint["tool"]
            hint_args = capability_hint["args"]
            slug = hint_args.get("tool_slug", "")
            if any(t.get("name") == hint_tool for t in selected_tools) and slug:
                lines.append(
                    f"For this request, the resolved {hint_tool} action is `{slug}`. "
                    f"Call {hint_tool} with action=\"execute\" and tool_slug=\"{slug}\", "
                    "adding only the arguments that the user actually specified. "
                    "Do not invent or guess a different tool_slug."
                )
        if any(t.get("name") == "find_tools" for t in selected_tools):
            lines.append(
                "If none of the available tools can do what the user asked, call find_tools "
                "with a short description of the capability you need, then call load_tool with "
                "the returned tool name to make it callable, then call that tool. Do not claim "
                "you cannot do something before searching with find_tools."
            )
    else:
        lines.append("")
        lines.append(
            "No tools are selected for this turn. Answer in natural conversation. "
            "If the user clearly wanted a device or system action you cannot run here, say so briefly without sounding like a policy bot."
        )
        if conversational_turn:
            lines.append(_load_chat_polish_section(
                "Fragment Follow-Up Rules",
                "Short reactions refer to your previous answer; clarify instead of refusing.",
            ))
    if memory_facts:
        lines.append("")
        lines.append(
            "Memory answer priority: if the Known facts for this turn answer the user's question, "
            "answer from those facts and do not replace them with general-world knowledge. "
            "For names, relationships, preferences, places, or ongoing situations, the matching known fact is the answer."
        )
        lines.append("Known facts for this turn:")
        lines.append(memory_facts)
    return "\n".join(lines)


def _recent_tool_context(messages, limit=3):
    parts = []
    for msg in reversed(messages):
        if msg.get("role") != "tool":
            continue
        content = msg.get("content") or ""
        if content:
            parts.append(content)
        if len(parts) >= limit:
            break
    return "\n".join(reversed(parts))


def _recent_action_context(messages, limit=6, max_chars=1600) -> str:
    parts = []
    for msg in reversed(messages):
        role = msg.get("role")
        if role not in ("user", "assistant", "tool"):
            continue
        content = msg.get("content") or ""
        if not content:
            continue
        if role == "assistant" and msg.get("tool_calls"):
            calls = []
            for call in msg.get("tool_calls") or []:
                function = call.get("function", {})
                calls.append(f"{function.get('name', '')}({function.get('arguments', '')})")
            if calls:
                content = "Tool call: " + " | ".join(calls)
        content = str(content).strip()
        if content:
            parts.append(f"{role}: {content[:max_chars]}")
        if len(parts) >= limit:
            break
    return "\n".join(reversed(parts))[:max_chars]


def _configured_context_followup_tools() -> set[str]:
    return {
        item.strip()
        for item in settings.context_followup_tools.split(",")
        if item.strip()
    }


@lru_cache(maxsize=1)
def _context_followup_texts() -> tuple[str, str]:
    return (
        _load_routing_policy_section(
            "Context Follow-Up Text",
            "The user is asking to continue the previous actionable result or show more from the same topic.",
        ),
        _load_routing_policy_section(
            "New Topic Text",
            "The user is giving a fresh standalone topic or a new named target.",
        ),
    )


@lru_cache(maxsize=1)
def _memory_context_texts() -> tuple[str, str]:
    return (
        _load_routing_policy_section(
            "Memory Recall Text",
            "The user asks what the assistant remembers about them or asks for personal saved facts.",
        ),
        _load_routing_policy_section(
            "No Memory Small Talk Text",
            "The user is greeting, chatting, reacting, or asking how the assistant is.",
        ),
    )


@lru_cache(maxsize=1)
def _memory_scope_texts() -> tuple[str, str]:
    return (
        _load_routing_policy_section(
            "Broad Memory Recall Text",
            "The user asks for a broad overview of remembered personal facts.",
        ),
        _load_routing_policy_section(
            "Specific Memory Recall Text",
            "The user asks for one particular remembered fact.",
        ),
    )


def _looks_like_context_followup(text: str, q_emb=None) -> bool:
    text = str(text or "").strip()
    if not text:
        return False
    try:
        text_emb = q_emb if q_emb is not None else embed(text)
        re = _RouteEmbeddings.get()
        followup_score = float(np.dot(re.v("followup"), text_emb))
        new_topic_score = float(np.dot(re.v("new_topic"), text_emb))
    except Exception:
        return False
    return followup_score >= new_topic_score


def _should_use_memory_context(user_input: str, q_emb, selected_tools: list) -> bool:
    if q_emb is None:
        return False
    try:
        re = _RouteEmbeddings.get()
        memory_score = float(np.dot(re.v("memory_recall"), q_emb))
        small_talk_score = float(np.dot(re.v("no_memory_small_talk"), q_emb))
    except Exception:
        return False
    if memory_score <= small_talk_score:
        return False
    if selected_tools:
        if not _selected_tools_allow_memory_context(selected_tools):
            return False
    return True


def _selected_tools_allow_memory_context(selected_tools: list) -> bool:
    memory_tools = {"memory_recall", "memory_assess", "memory_remember", "memory_forget"}
    selected_names = {tool.get("name", "") for tool in selected_tools}
    return not selected_names or selected_names <= memory_tools


def _selected_tools_are_memory_recall_only(selected_tools: list) -> bool:
    selected_names = {tool.get("name", "") for tool in selected_tools}
    return bool(selected_names) and selected_names <= {"memory_recall"}


def _should_use_proactive_memory_context(user_input: str, q_emb, selected_tools: list) -> bool:
    if not settings.proactive_memory_context_enabled or q_emb is None:
        return False
    if not _selected_tools_allow_memory_context(selected_tools):
        return False
    try:
        route_embeddings = _RouteEmbeddings.get()
        proactive_score = float(np.dot(route_embeddings.v("proactive_memory"), q_emb))
        small_talk_score = float(np.dot(route_embeddings.v("no_memory_small_talk"), q_emb))
        actionable_score = float(np.dot(route_embeddings.v("actionable"), q_emb))
    except Exception:
        return False
    if actionable_score > proactive_score:
        return False
    margin = max(0.0, float(settings.proactive_memory_context_margin))
    return proactive_score + margin >= small_talk_score


def _should_use_profile_context(user_input: str, q_emb, last_profile_context: bool = False) -> bool:
    if q_emb is None:
        return False
    if last_profile_context and _looks_like_context_followup(user_input):
        return True
    try:
        re = _RouteEmbeddings.get()
        broad_score = float(np.dot(re.v("broad_recall"), q_emb))
        specific_score = float(np.dot(re.v("specific_recall"), q_emb))
    except Exception:
        return False
    return broad_score >= specific_score


def _latest_tool_name(messages) -> str:
    for msg in reversed(messages):
        if msg.get("role") != "assistant":
            continue
        calls = msg.get("tool_calls") or []
        if not calls:
            continue
        function = calls[-1].get("function", {})
        name = function.get("name", "")
        if name:
            return name
    return ""


def _maybe_reuse_latest_context_tool(user_input: str, selected_tools: list, messages: list) -> list:
    context_tools = _configured_context_followup_tools()
    if not context_tools:
        return selected_tools

    latest_name = _latest_tool_name(messages)
    if not latest_name or latest_name not in context_tools:
        return selected_tools

    if not _looks_like_context_followup(user_input):
        return selected_tools

    if not selected_tools:
        latest_tool = get_tool(latest_name)
        return [latest_tool] if latest_tool else selected_tools

    selected_names = {tool["name"] for tool in selected_tools}
    if not selected_names <= context_tools or latest_name in selected_names:
        return selected_tools

    terms = _grounding_terms(user_input)
    for selected_name in selected_names:
        if terms & _grounding_terms(selected_name):
            return selected_tools

    latest_tool = get_tool(latest_name)
    return [latest_tool] if latest_tool else selected_tools


def _latest_tool_arguments(messages: list, tool_name: str) -> dict:
    for msg in reversed(messages):
        if msg.get("role") != "assistant":
            continue
        calls = msg.get("tool_calls") or []
        for call in reversed(calls):
            function = call.get("function", {})
            if function.get("name") != tool_name:
                continue
            try:
                parsed = json.loads(function.get("arguments") or "{}")
            except (TypeError, json.JSONDecodeError):
                return {}
            return parsed if isinstance(parsed, dict) else {}
    return {}


def _latest_tool_result(messages: list, tool_name: str) -> dict:
    for msg in reversed(messages):
        if msg.get("role") != "tool":
            continue
        content = msg.get("content") or ""
        try:
            parsed = json.loads(content)
        except (TypeError, json.JSONDecodeError):
            continue
        if not isinstance(parsed, dict):
            continue
        meta = parsed.get("meta", {})
        if meta.get("tool") == tool_name:
            return parsed
    return {}


def _first_result_url(result_payload: dict) -> str:
    result = result_payload.get("result", {})
    if not isinstance(result, dict):
        return ""
    results = result.get("results")
    if not isinstance(results, list) or not results:
        return ""
    first = results[0]
    if not isinstance(first, dict):
        return ""
    return str(first.get("url", "")).strip()


def _first_result_item_id(result_payload: dict) -> str:
    result = result_payload.get("result", {})
    if not isinstance(result, dict):
        return ""
    items = result.get("items")
    if not isinstance(items, list) or not items:
        items = result.get("results")
    if not isinstance(items, list) or not items:
        return ""
    first = items[0]
    if not isinstance(first, dict):
        return ""
    return str(first.get("id") or first.get("objectID") or "").strip()


def _result_count(result_payload: dict) -> int:
    result = result_payload.get("result", {})
    if not isinstance(result, dict):
        return 0
    try:
        return int(result.get("result_count", 0))
    except (TypeError, ValueError):
        return 0


def _repair_contextual_tool_args(tool_name: str, args: dict, user_input: str, messages: list) -> dict:
    if tool_name not in _configured_context_followup_tools():
        return args
    if not _looks_like_context_followup(user_input):
        return args
    latest_args = _latest_tool_arguments(messages, tool_name)
    if not latest_args:
        return args
    repaired = dict(args)
    if "query" in repaired and latest_args.get("query") and _looks_like_context_followup(str(repaired.get("query", ""))):
        repaired["query"] = latest_args["query"]
        if "max_results" in repaired:
            try:
                repaired["max_results"] = min(10, max(int(repaired["max_results"]), 8))
            except (TypeError, ValueError):
                repaired["max_results"] = 8
        elif _tool_supports_parameter(tool_name, "max_results"):
            repaired["max_results"] = 8
        if "limit" in repaired:
            try:
                repaired["limit"] = min(30, max(int(repaired["limit"]), 10))
            except (TypeError, ValueError):
                repaired["limit"] = 10
    return repaired


def _repair_contextual_tool_call(tool_name: str, args: dict, user_input: str, messages: list) -> tuple[str, dict]:
    if tool_name == "web_search" and _looks_like_context_followup(user_input):
        latest_result = _latest_tool_result(messages, "web_search")
        url = _first_result_url(latest_result)
        if url and 0 < _result_count(latest_result) <= 2:
            return "web_fetch", {
                "url": url,
                "max_chars": 6000,
                "follow_redirects": True,
            }
    if tool_name == "hackernews" and _looks_like_context_followup(user_input):
        latest_result = _latest_tool_result(messages, "hackernews")
        item_id = _first_result_item_id(latest_result)
        if item_id:
            repaired = dict(args)
            repaired["action"] = "comments"
            repaired["query"] = item_id
            if "id" in repaired:
                repaired["id"] = item_id
            return "hackernews", repaired
    repaired_args = _repair_contextual_tool_args(tool_name, args, user_input, messages)
    repaired_args = _repair_search_query_specificity(tool_name, repaired_args, user_input)
    repaired_args = _repair_system_control_args(tool_name, repaired_args, user_input)
    return tool_name, repaired_args


def _forced_contextual_tool_call(user_input: str, tool_schemas: list, messages: list) -> dict | None:
    if not tool_schemas or not _looks_like_context_followup(user_input):
        return None
    available = {schema["function"]["name"] for schema in tool_schemas}
    latest_name = _latest_tool_name(messages)
    if latest_name not in available:
        return None
    latest_args = _latest_tool_arguments(messages, latest_name)
    repaired_name, repaired_args = _repair_contextual_tool_call(latest_name, latest_args, user_input, messages)
    if repaired_name not in available:
        return None
    if repaired_name == latest_name and repaired_args == latest_args:
        return None
    return {
        "id": "call_contextual_0",
        "name": repaired_name,
        "arguments": json.dumps(repaired_args, ensure_ascii=False),
    }


def _forced_hint_tool_call(capability_hint: dict | None, tool_schemas: list) -> dict | None:
    if not capability_hint or not capability_hint.get("direct"):
        return None
    tool_name = str(capability_hint.get("tool") or "").strip()
    args = capability_hint.get("args")
    if not tool_name or not isinstance(args, dict):
        return None
    available = {schema["function"]["name"] for schema in tool_schemas}
    if tool_name not in available:
        return None
    return {
        "id": f"call_{tool_name}_hint",
        "name": tool_name,
        "arguments": json.dumps(args, ensure_ascii=False),
    }


def _forced_folder_watcher_call(user_input: str, tool_schemas: list, messages: list | None = None) -> dict | None:
    if len(tool_schemas) != 1:
        return None
    function = tool_schemas[0].get("function", {})
    if function.get("name") != "folder_watcher":
        return None

    try:
        from tools.folder_watcher import build_natural_folder_watcher_args
    except Exception:
        return None

    recent_result = _latest_tool_result(messages or [], "folder_watcher")
    args = build_natural_folder_watcher_args(
        user_input,
        recent_result=recent_result,
        response_format="structured" if _tool_supports_parameter("folder_watcher", "response_format") else "",
    )
    if not args:
        return None
    return {
        "id": "call_folder_watcher_0",
        "name": "folder_watcher",
        "arguments": json.dumps(args, ensure_ascii=False),
    }


def _should_suppress_memory_context(router_decision: dict) -> bool:
    return router_decision.get("reason") == "below_tool_threshold"


def _structured_result_payload(text: str):
    """If a tool result string is a structured-response envelope, return its dict.

    Structured responses look like {"result": {...}, "meta": {...}} (or an
    {"error": {...}, "meta": {...}} envelope). Returns the parsed dict when the
    string is that shape, else None. Used to avoid dumping raw JSON to the user
    on the single-tool direct path.
    """
    stripped = str(text or "").strip()
    if not (stripped.startswith("{") and stripped.endswith("}")):
        return None
    try:
        parsed = json.loads(stripped)
    except (json.JSONDecodeError, TypeError, ValueError):
        return None
    if isinstance(parsed, dict) and isinstance(parsed.get("meta"), dict) and ("result" in parsed or "error" in parsed):
        return parsed
    return None


def _merge_context_facts(primary: str, secondary: str, limit: int = 8) -> str:
    """Combine two ' | '-joined fact strings, keeping order and dropping
    duplicates. The primary (query-specific) facts come first so a precise
    recall is never lost when broad profile facts are added for breadth.
    Dedupe is based on the bare fact, ignoring any '(via ...)' evidence
    suffix so the same fact is not listed twice in different forms."""
    def _base(fact: str) -> str:
        head = fact.split(" (via ", 1)[0]
        return head.strip().casefold()

    merged = []
    seen = set()
    for source in (primary, secondary):
        for part in str(source or "").split(" | "):
            fact = part.strip()
            if not fact:
                continue
            key = _base(fact)
            if key in seen:
                continue
            seen.add(key)
            merged.append(fact)
            if len(merged) >= max(1, limit):
                return " | ".join(merged)
    return " | ".join(merged)


def _memory_context_has_graph(context: str | None) -> bool:
    for line in str(context or "").splitlines():
        line = line.strip()
        if line.startswith("Graph memory:") and line[len("Graph memory:"):].strip():
            return True
    return False


def _should_keep_memory_context(
    context: str | None,
    user_input: str,
    q_emb,
    selected_tools: list,
    router_decision: dict,
    proactive_context_requested: bool = False,
) -> bool:
    if not context:
        return False
    if _memory_context_has_graph(context):
        return True
    if proactive_context_requested:
        return True
    if _should_suppress_memory_context(router_decision):
        return _should_use_memory_context(user_input, q_emb, selected_tools)
    return _should_use_memory_context(user_input, q_emb, selected_tools)


def _tool_result_content(result) -> str:
    if result is None:
        return "Done"
    if isinstance(result, (dict, list)):
        result = _compact_tool_result_for_context(result)
        return json.dumps(result, ensure_ascii=False)
    return str(result)


def _compact_tool_result_for_context(result):
    if not isinstance(result, dict):
        return result
    meta = result.get("meta", {})
    tool_name = meta.get("tool", "")
    if tool_name == "folder_watcher" and isinstance(result.get("result"), dict):
        compact = dict(result)
        payload = dict(result["result"])
        stats = payload.get("stats") if isinstance(payload.get("stats"), dict) else {}
        compact_stats = {}
        for key in ("active_files", "total_size_bytes", "events", "summary_coverage", "fts_enabled", "by_extension", "by_mime_type", "by_extension_details", "by_mime_type_details"):
            if key in stats:
                compact_stats[key] = stats[key]
        files = []
        for item in payload.get("files", []) or []:
            if not isinstance(item, dict):
                continue
            compact_item = {}
            for key in ("id", "path", "filename", "extension", "mime_type", "size_bytes", "summary", "tags", "status"):
                if key in item:
                    compact_item[key] = item[key]
            excerpt = str(item.get("content_excerpt") or "")
            if excerpt:
                compact_item["content_excerpt"] = excerpt[:700]
            files.append(compact_item)
            if len(files) >= 8:
                break
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        payload["data"] = {
            "mode": data.get("mode", payload.get("mode", "")),
            "message": data.get("message", payload.get("query", "")),
            "answer": data.get("answer", payload.get("answer", "")),
            "provider": data.get("provider", ""),
            "context_mode": data.get("context_mode", ""),
            "stats": compact_stats,
            "files": files,
        }
        payload["stats"] = compact_stats
        payload["files"] = files
        payload["count"] = payload.get("count", len(files))
        compact["result"] = payload
        compact.pop("trace", None)
        return compact
    if tool_name == "web_search" and isinstance(result.get("result"), dict):
        compact = dict(result)
        payload = dict(result["result"])
        compact_results = []
        for item in payload.get("results", []) or []:
            if not isinstance(item, dict):
                continue
            compact_item = dict(item)
            body = str(compact_item.get("body", ""))
            if len(body) > 700:
                compact_item["body"] = body[:700] + "\n...[truncated]"
            compact_results.append(compact_item)
        payload["results"] = compact_results
        compact["result"] = payload
        return compact
    if tool_name == "web_fetch" and isinstance(result.get("result"), dict):
        compact = dict(result)
        payload = dict(result["result"])
        text = str(payload.get("text", ""))
        if len(text) > 6000:
            payload["text"] = text[:6000] + "\n...[truncated]"
            payload["truncated"] = True
        compact["result"] = payload
        return compact
    return result


def _build_tool_answer_instruction(user_input: str, tool_names: list[str]) -> str:
    names = ", ".join(tool_names)
    return (
        "A tool call has completed for the user's request. Use the tool result fields as evidence "
        "and now answer the user in natural language. Do not expose raw JSON, Python dict syntax, "
        "tool traces, function-call syntax, or unprocessed result lists. If the result contains titles, "
        "URLs, counts, provider status, fallback state, errors, truncation, or readable text, base the "
        "answer only on those observed fields. If the user asked for latest or current information, "
        "state what the fetched results actually show and mention any source/provider limits. If the "
        "tool result contains an error or missing target, do not claim success; ask for the missing "
        "target or state the observed failure. For search results, do not merely say how many results "
        "were found; include the useful observed titles, links, snippets, and provider/fallback status. "
        "Do not add themes, summaries, totals, descriptions, or categories that are not explicitly "
        "present in the tool result. "
        "If the original tool-call arguments conflict with returned result fields, trust the returned "
        "result fields. "
        "For keyboard_press and keyboard_shortcut, say the keys were sent; do not claim the visible "
        "desktop, app, or window state changed unless the result includes explicit verification. "
        "For system_control, claim the state changed only when the result has verified=true. "
        "If status is attempted_unverified or claim is sent_key_only, say what was sent and that the "
        "state was not verified. "
        "When the result has a query field, describe that field as the searched query, not the user's "
        "short follow-up wording. "
        "For folder_watcher stats, use returned by_extension, by_mime_type, by_extension_details, "
        "and by_mime_type_details maps for counts and sizes; if the user asks about a file family "
        "that is absent from those returned maps, say the returned stats show zero for that family "
        "instead of saying the data is unavailable. "
        f"Tools used: {names}. User request: {user_input}"
    )


def _direct_answer_from_tool_result(tool_name: str, result_content: str) -> str:
    try:
        parsed = json.loads(result_content or "{}")
    except (TypeError, json.JSONDecodeError):
        return ""
    if not isinstance(parsed, dict) or parsed.get("error"):
        return ""
    result = parsed.get("result")
    if not isinstance(result, dict):
        return ""
    answer = str(result.get("answer") or result.get("text") or "").strip()
    data = result.get("data") if isinstance(result.get("data"), dict) else {}
    if not answer:
        answer = str(data.get("answer") or "").strip()
    return answer


def _tool_supports_parameter(tool_name: str, parameter_name: str) -> bool:
    registered = get_tool(tool_name)
    if not registered:
        return False
    properties = registered.get("parameters", {}).get("properties", {})
    return parameter_name in properties


@lru_cache(maxsize=1)
def _load_grounding_policy() -> tuple[set[str], set[str]]:
    path = Path(__file__).resolve().parent.parent / settings.tool_grounding_policy_file
    skip_params = {
        "response_format",
        "trace_enabled",
        "config_path",
        "output_style",
        "read_mode",
    }
    loose_tools = {"memory_recall", "memory_assess", "system_control"}
    if not path.exists():
        return skip_params, loose_tools
    section = ""
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if line.startswith("## "):
            section = line[3:].strip().lower()
            continue
        if not line.startswith("- "):
            continue
        item = line[2:].strip()
        if not item:
            continue
        if section == "skip parameters":
            skip_params.add(item)
        elif section == "loose grounding tools":
            loose_tools.add(item)
    return skip_params, loose_tools


@lru_cache(maxsize=1)
def _conversational_banter_text() -> str:
    return _load_routing_policy_section(
        "Conversational Banter Text",
        "The user is reacting, joking, or chatting casually without requesting a concrete action.",
    )


@lru_cache(maxsize=1)
def _actionable_request_text() -> str:
    return _load_routing_policy_section(
        "Actionable Request Text",
        "The user wants a concrete tool-backed action or observable outcome.",
    )


def _looks_like_conversational_banter(user_input: str, q_emb) -> bool:
    text = str(user_input or "").strip()
    if not text or q_emb is None:
        return False
    if _looks_like_local_system_control(text, q_emb) or _looks_like_action_correction(text, []):
        return False
    try:
        re = _RouteEmbeddings.get()
        banter_score = float(np.dot(re.v("banter"), q_emb))
        action_score = float(np.dot(re.v("actionable"), q_emb))
        chat_score = float(np.dot(re.v("no_memory_small_talk"), q_emb))
    except Exception:
        return False
    conversational_score = max(banter_score, chat_score)
    return conversational_score >= action_score and conversational_score >= settings.tool_similarity_threshold


def _looks_like_actionable_request(user_input: str, q_emb) -> bool:
    text = str(user_input or "").strip()
    if not text or q_emb is None:
        return False
    try:
        route_embeddings = _RouteEmbeddings.get()
        action_score = float(np.dot(route_embeddings.v("actionable"), q_emb))
        banter_score = float(np.dot(route_embeddings.v("banter"), q_emb))
        chat_score = float(np.dot(route_embeddings.v("no_memory_small_talk"), q_emb))
        memory_score = float(np.dot(route_embeddings.v("memory_recall"), q_emb))
    except Exception:
        return False
    return (
        action_score >= settings.tool_similarity_threshold
        and action_score >= max(banter_score, chat_score, memory_score)
    )


@lru_cache(maxsize=1)
def _incomplete_utterance_text() -> str:
    return _load_routing_policy_section(
        "Incomplete Utterance Text",
        "The user trailed off or left the object of the question unstated.",
    )


def _looks_like_incomplete_utterance(user_input: str, q_emb) -> bool:
    text = str(user_input or "").strip()
    if not text:
        return False
    trimmed = text.rstrip()
    if trimmed.endswith("...") or trimmed.endswith("…"):
        return True
    if len(_query_terms(text)) > max(0, settings.incomplete_utterance_max_terms):
        return False
    if q_emb is None:
        return False
    try:
        re = _RouteEmbeddings.get()
        incomplete_score = float(np.dot(re.v("incomplete"), q_emb))
        new_topic_score = float(np.dot(re.v("new_topic"), q_emb))
        action_score = float(np.dot(re.v("actionable"), q_emb))
        return (
            incomplete_score >= settings.tool_similarity_threshold
            and incomplete_score >= new_topic_score
            and incomplete_score >= action_score
        )
    except Exception:
        return False


def _recent_conversation_topic(messages: list) -> str:
    for msg in reversed(messages):
        if msg.get("role") != "user":
            continue
        content = _strip_memory_prefix(str(msg.get("content", ""))).strip()
        if content and len(content) >= 8:
            return content
    return ""


def _expand_conversational_input(user_input: str, messages: list) -> str:
    text = str(user_input or "").strip()
    if not text:
        return text
    topic = _recent_conversation_topic(messages)
    if not topic or topic == text:
        return text
    trimmed = text.rstrip()
    explicit_incomplete = trimmed.endswith("...") or trimmed.endswith("…")
    if not settings.context_followup_expansion_enabled and not explicit_incomplete:
        return text
    utterance_emb = embed(text)
    if _looks_like_conversational_banter(text, utterance_emb):
        return text
    if explicit_incomplete or _looks_like_context_followup(text, utterance_emb) or _looks_like_incomplete_utterance(text, utterance_emb):
        return f"Earlier: {topic}\nNow: {text}"
    return text


def _embedding_query(user_input: str, messages: list) -> tuple[object | None, str]:
    expanded = _expand_conversational_input(user_input, messages)
    if len(expanded.strip()) >= settings.embedding_min_chars:
        return embed(expanded), expanded
    topic = _recent_conversation_topic(messages)
    if topic:
        combined = f"{topic}\n{expanded}".strip()
        if len(combined) >= settings.embedding_min_chars:
            return embed(combined), expanded
    return None, expanded


def _is_conversational_turn(selected_tools: list, user_input: str, q_emb) -> bool:
    if not selected_tools:
        return True
    return _looks_like_conversational_banter(user_input, q_emb)


def _should_extract_memory(user_input: str, q_emb) -> bool:
    if not settings.memory_store_enabled:
        return False
    text = str(user_input or "").strip()
    if len(text) < 12:
        return False
    if _looks_like_conversational_banter(text, q_emb if q_emb is not None else embed(text)):
        return False
    return True


def _filter_tools_for_conversation(user_input: str, q_emb, selected_tools: list) -> list:
    if not selected_tools:
        return selected_tools
    if _looks_like_conversational_banter(user_input, q_emb):
        if _selected_tool_has_user_terms(user_input, selected_tools):
            return selected_tools
        return []
    return selected_tools


@lru_cache(maxsize=1)
def _folder_watcher_route_texts() -> tuple[str, str]:
    return (
        _load_routing_policy_section(
            "Folder Watcher Request Text",
            "The user asks a natural question about current folder evidence, file counts, file types, sizes, images, media, search, or content.",
        ),
        _load_routing_policy_section(
            "Raw Directory Listing Text",
            "The user asks for raw filesystem directory entries or filenames for a specific path.",
        ),
    )


def _prefer_folder_watcher_for_folder_context(user_input: str, q_emb, selected_tools: list) -> list:
    """Trust the router's utterance-based ordering. If folder_watcher scored
    higher than file_list/file_read/gallery, it stays first. Otherwise keep the
    router's order. No secondary embedding comparison needed — the utterance
    bank already encodes the distinction between semantic folder queries and raw
    directory listings."""
    return selected_tools


def _selected_tool_has_user_terms(user_input: str, selected_tools: list) -> bool:
    user_terms = _grounding_terms(user_input)
    if not user_terms:
        return False
    for tool in selected_tools:
        parts = [
            str(tool.get("name", "")),
            str(tool.get("description", "")),
        ]
        examples = tool.get("examples") or []
        if isinstance(examples, list):
            parts.extend(str(item) for item in examples)
        properties = tool.get("parameters", {}).get("properties", {})
        if isinstance(properties, dict):
            for param_name, details in properties.items():
                parts.append(str(param_name))
                if isinstance(details, dict):
                    parts.append(str(details.get("description", "")))
        if user_terms & _grounding_terms(" ".join(parts)):
            return True
    return False


@lru_cache(maxsize=1)
def _local_system_control_text() -> str:
    return _load_routing_policy_section(
        "Local System Control Text",
        "The user wants to change this computer's volume, brightness, mute state, or media playback.",
    )


def _looks_like_local_system_control(user_input: str, q_emb) -> bool:
    text = str(user_input or "").strip()
    if not text or q_emb is None:
        return False
    try:
        re = _RouteEmbeddings.get()
        system_score = float(np.dot(re.v("system_control"), q_emb))
        chat_score = float(np.dot(re.v("no_memory_small_talk"), q_emb))
        memory_score = float(np.dot(re.v("memory_recall"), q_emb))
        action_score = float(np.dot(re.v("actionable"), q_emb))
    except Exception:
        return False
    if action_score < settings.local_system_action_min_score:
        return False
    return (
        system_score >= chat_score
        and system_score >= memory_score
        and system_score >= settings.tool_similarity_threshold
    )


def _ensure_local_system_control_tool(selected_tools: list, user_input: str, q_emb, messages: list | None = None) -> list:
    needs_tool = _looks_like_local_system_control(user_input, q_emb)
    if not needs_tool and messages is not None and _last_user_system_request(messages):
        needs_tool = _looks_like_action_correction(user_input, messages)
    if not needs_tool:
        return selected_tools
    tool = get_tool("system_control")
    if not tool:
        return selected_tools
    if any(item.get("name") == "system_control" for item in selected_tools):
        return selected_tools
    boosted = [tool] + list(selected_tools)
    return boosted[: max(1, settings.tool_top_k)]


@lru_cache(maxsize=1)
def _action_correction_text() -> str:
    return _load_routing_policy_section(
        "Action Correction Text",
        "The user says the previous answer was wrong or the action did not work.",
    )


def _looks_like_action_correction(user_input: str, messages: list) -> bool:
    text = str(user_input or "").strip()
    if not text:
        return False
    # Short negatives are corrections when they follow an action
    lower = text.lower().strip("!?. ")
    if lower in ("no", "nope", "nah", "wrong", "not working", "still wrong", "try again", "didnt work"):
        return True
    try:
        re = _RouteEmbeddings.get()
        text_emb = embed(text)
        correction_score = float(np.dot(re.v("correction"), text_emb))
        new_topic_score = float(np.dot(re.v("new_topic"), text_emb))
    except Exception:
        return False
    text_terms = _query_terms(text)
    if new_topic_score > correction_score and len(text_terms) >= 2:
        return False
    if correction_score >= settings.tool_similarity_threshold:
        return True
    return False


def _strip_memory_prefix(content: str) -> str:
    text = str(content or "").strip()
    if text.startswith("[") and "]" in text:
        return text.split("]", 1)[-1].strip()
    return text


def _last_user_system_request(messages: list) -> str:
    for msg in reversed(messages):
        if msg.get("role") != "user":
            continue
        content = _strip_memory_prefix(str(msg.get("content", "")))
        if _looks_like_local_system_control(content, embed(content)):
            return content
    return ""


def _forced_local_system_control_call(
    user_input: str,
    tool_schemas: list,
    messages: list,
    q_emb,
) -> dict | None:
    if not tool_schemas:
        return None
    available = {schema["function"]["name"] for schema in tool_schemas}
    if "system_control" not in available:
        return None

    args: dict = {}
    if _looks_like_local_system_control(user_input, q_emb):
        args = _repair_system_control_args("system_control", {}, user_input)
    elif _looks_like_action_correction(user_input, messages):
        latest_args = _latest_tool_arguments(messages, "system_control")
        if latest_args.get("action"):
            args = dict(latest_args)
        else:
            prior_request = _last_user_system_request(messages)
            if prior_request:
                args = _repair_system_control_args("system_control", {}, prior_request)
    else:
        return None

    args = _repair_system_control_args("system_control", args, user_input)
    action = str(args.get("action", "")).strip()
    if not action:
        return None
    if _tool_supports_parameter("system_control", "response_format"):
        args["response_format"] = "structured"
    return {
        "id": "call_system_control_0",
        "name": "system_control",
        "arguments": json.dumps(args, ensure_ascii=False),
    }


def _repair_system_control_args(tool_name: str, args: dict, user_input: str) -> dict:
    if tool_name != "system_control":
        return args
    from tools import system_control as system_control_mod

    repaired = dict(args)
    config_path = str(repaired.get("config_path", "")).strip()
    if config_path and config_path.lower() not in str(user_input or "").lower():
        repaired.pop("config_path", None)

    action = str(repaired.get("action", "")).strip()
    if action:
        repaired["action"] = system_control_mod._normalize_action_name(action)
        if "level" not in repaired:
            level = system_control_mod._extract_level_from_text(user_input)
            if level is not None:
                repaired["level"] = level
        return repaired

    aliases = system_control_mod._load_action_aliases()
    level = system_control_mod._extract_level_from_text(user_input)
    set_action = system_control_mod._best_set_action_for_text(user_input) if level is not None else ""
    if set_action:
        repaired["action"] = set_action
        repaired["level"] = level
        return repaired
    if aliases:
        alias_keys = list(aliases.keys())
        alias_texts = [key.replace("_", " ") for key in alias_keys]
        try:
            user_emb = embed(str(user_input or ""))
            alias_embs = embed(alias_texts)
            if getattr(alias_embs, "ndim", 1) == 1:
                alias_embs = alias_embs.reshape(1, -1)
            scores = np.dot(alias_embs, user_emb)
            best_index = int(np.argmax(scores))
            candidate_key = alias_keys[best_index]
            candidate_action = aliases[candidate_key]
            user_terms = system_control_mod._term_set(user_input)
            candidate_terms = system_control_mod._term_set(f"{candidate_key} {candidate_action}")
            if (
                float(scores[best_index]) >= settings.tool_argument_grounding_threshold
                and bool(user_terms & candidate_terms)
            ):
                repaired["action"] = candidate_action
        except Exception:
            pass
    if level is not None:
        repaired["level"] = level
    return repaired


def _prepare_tool_args_for_answer(tool_name: str, args: dict) -> dict:
    prepared = dict(args)
    if _tool_supports_parameter(tool_name, "response_format") and "response_format" not in prepared:
        prepared["response_format"] = "structured"
    return prepared


def _tool_call_grounded(user_input: str, args: dict, messages: list, tool_name: str = "") -> bool:
    skip_params, loose_tools = _load_grounding_policy()
    if tool_name in loose_tools:
        return True

    values = []
    for param, value in args.items():
        if param in skip_params:
            continue
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        if isinstance(value, (dict, list)):
            continue
        values.append(str(value))
    if not values and not tool_name:
        return True

    grounding_text = _strip_memory_prefix(user_input)
    recent_tool_context = _recent_tool_context(messages)
    if recent_tool_context:
        grounding_text = f"{grounding_text}\n{recent_tool_context}"
    try:
        grounding_emb = embed(grounding_text)
        embed_values = list(values)
        if tool_name:
            embed_values.append(tool_name)
        value_embs = embed(embed_values)
        if getattr(value_embs, "ndim", 1) == 1:
            value_embs = value_embs.reshape(1, -1)
        similarity = float(np.max(np.dot(value_embs, grounding_emb)))
    except Exception:
        return True
    if similarity >= settings.tool_argument_grounding_threshold:
        return True

    user_terms = _grounding_terms(grounding_text)
    value_terms = set()
    for value in values:
        value_terms.update(_grounding_terms(value))
    return bool(user_terms & value_terms)


def _grounding_terms(text: str) -> set[str]:
    terms = set()
    current = []
    for char in str(text).lower():
        if char.isalnum():
            current.append(char)
            continue
        if current:
            token = "".join(current)
            if len(token) >= 3:
                terms.add(token)
            current = []
    if current:
        token = "".join(current)
        if len(token) >= 3:
            terms.add(token)
    return terms


def _query_terms(text: str) -> set[str]:
    terms = set()
    current = []
    for char in str(text).lower():
        if char.isalnum():
            current.append(char)
            continue
        if current:
            token = "".join(current)
            if len(token) >= 2:
                terms.add(token)
            current = []
    if current:
        token = "".join(current)
        if len(token) >= 2:
            terms.add(token)
    return terms


def _repair_search_query_specificity(tool_name: str, args: dict, user_input: str) -> dict:
    if tool_name != "web_search" or "query" not in args:
        return args
    query = str(args.get("query", "")).strip()
    user_text = str(user_input or "").strip()
    if not query or not user_text:
        return args
    query_terms = _query_terms(query)
    user_terms = _query_terms(user_text)
    missing_terms = user_terms - query_terms
    if query_terms and query_terms <= user_terms and len(missing_terms) >= 2:
        repaired = dict(args)
        repaired["query"] = user_text
        return repaired
    return args


class Agent:
    def __init__(self):
        summary_path = Path(settings.summaries_path)
        self.summary_store = SummaryStore(summary_path, max_summaries=settings.summaries_max_count)
        self.summary_store.load()
        self._summary_context = self.summary_store.context_string(n=settings.summaries_max_context)
        self.session_store = None
        self._session_context = ""
        if settings.session_store_enabled:
            try:
                from memory.session_store import SessionStore

                self.session_store = SessionStore()
                self._session_context = self.session_store.context_string()
                self.session_store.start_session()
                if settings.session_digest_enabled:
                    self._submit_background(self._digest_undigested_sessions)
            except Exception:
                self.session_store = None
                self._session_context = ""
        combined_context = "\n\n".join(part for part in (self._summary_context, self._session_context) if part)
        initial_content = _build_system_prompt([], summary_context=combined_context)
        self.messages = [{"role": "system", "content": initial_content}]
        self.llm = NIMClient()
        self.router = ToolRouter()
        self.validator = ToolValidator()
        self.verifier = Verifier()
        self.brain = Brain()
        self._memory_extraction_messages = []
        self._last_memory_profile_context = False

        if not self.llm.check_api_key():
            console.print("[red]NVIDIA_API_KEY not set![/red]")

        console.print("[dim]Ready[/dim]")

    def _combined_context(self) -> str:
        """Summary context + cross-session recap for system-prompt injection."""
        return "\n\n".join(part for part in (self._summary_context, self._session_context) if part)

    def _digest_undigested_sessions(self):
        """Background: digest any prior sessions that haven't been processed yet."""
        try:
            from memory.session_digest import process_undigested
            results = process_undigested(self.session_store, brain=self.brain)
            if results:
                digested = sum(1 for r in results if r.get("status") == "digested")
                if digested and settings.debug:
                    console.print(f"[dim]Digested {digested} prior session(s)[/dim]")
        except Exception:
            pass

    def _check_proactive(self) -> str | None:
        """Check the cognition proactive queue and return a natural observation
        if one clears the gates, or None to stay quiet.

        If the queue is empty, seeds it from cadence deviations on the fly so
        proactive doesn't depend on daily maintenance having run first.
        """
        try:
            from cognition.proactive import ProactiveEngine
            from cognition.state import load_state, save_state
        except Exception:
            return None
        try:
            state = load_state()
            engine = ProactiveEngine.from_dict(state.get("proactive") or {})
            now = datetime.now()
            if engine.budget_remaining(now) <= 0:
                return None

            # If queue is empty, try to seed from cadence deviations.
            if engine.queue_size() == 0:
                try:
                    from cognition.orchestrator import run_cognition_pass
                    result = run_cognition_pass(self.brain, embed_fn=embed, now=now, persist=True)
                    # Reload state after the pass populated the queue.
                    state = load_state()
                    engine = ProactiveEngine.from_dict(state.get("proactive") or {})
                except Exception:
                    pass

            if engine.queue_size() == 0:
                return None

            candidate = engine.select(situational_fit=0.7, now=now)
            if candidate is None:
                return None
            # Mark delivered and persist.
            engine.mark_delivered(candidate, now=now)
            state["proactive"] = engine.to_dict()
            save_state(state)
            # Return the structured content for the agent to phrase naturally.
            node = candidate.node or "something"
            kind = candidate.source.replace("cadence_", "")
            return f"By the way — I noticed something about {node} ({kind}). Want me to tell you more?"
        except Exception:
            return None

    def _run_proactive(self, content: str, emit_chunk=None):
        """Run proactive check in background thread."""
        try:
            note = self._check_proactive()
            if note:
                if emit_chunk:
                    emit_chunk("\n\n" + note)
                else:
                    print(f"\n{note}", flush=True)
        except Exception:
            pass

    def _submit_background(self, func, *args):
        thread = threading.Thread(target=func, args=args, daemon=True)
        thread.start()
        return thread

    def _maybe_summarize(self):
        if count_messages_tokens(self.messages) < MAX_CONTEXT_TOKENS:
            return
        self._do_summarize()

    def _do_summarize(self):
        if count_messages_tokens(self.messages) < MAX_CONTEXT_TOKENS:
            return

        keep = self.messages[:1]
        recent = self.messages[-4:] if len(self.messages) > 4 else self.messages[1:]
        older = self.messages[1:-4] if len(self.messages) > 4 else []

        if not older:
            return

        summary = self.llm.extract_summary(
            older,
            instruction=_load_chat_polish_section(
                "Session Summary Rules",
                "",
            ),
        )
        if summary:
            console.print(f"[dim]Summarized {len(older)} messages[/dim]")
            self.messages = keep + [
                {"role": "system", "content": f"Previous conversation summary: {summary}"}
            ] + recent
            turn_count = len([m for m in older if m.get("role") == "user"])
            self.summary_store.append(summary, turn_count=turn_count)
            self._summary_context = self.summary_store.context_string(n=settings.summaries_max_context)
        else:
            self.messages = keep + recent

    def _memory_extraction_limit(self) -> int:
        try:
            return max(0, min(int(settings.memory_extraction_context_messages), 40))
        except (TypeError, ValueError):
            return 8

    def _memory_extraction_context(self) -> str:
        limit = self._memory_extraction_limit()
        if limit <= 0:
            return ""
        lines = []
        for item in self._memory_extraction_messages[-limit:]:
            role = str(item.get("role", "")).strip()
            content = str(item.get("content", "")).strip()
            if role and content:
                lines.append(f"{role}: {content}")
        return "\n".join(lines)

    def _remember_plain_turn(self, user_input: str, assistant_response: str):
        if user_input:
            self._memory_extraction_messages.append({"role": "user", "content": str(user_input)})
        if assistant_response:
            self._memory_extraction_messages.append({"role": "assistant", "content": str(assistant_response)})
        limit = self._memory_extraction_limit()
        if limit > 0 and len(self._memory_extraction_messages) > limit:
            self._memory_extraction_messages = self._memory_extraction_messages[-limit:]
        # Persist the full turn to the session transcript (JSONL) so it survives
        # across sessions and can be digested into the memory graph later.
        if self.session_store is not None:
            try:
                self.session_store.log_turn(user_input, assistant_response)
            except Exception:
                pass

    def process(self, user_input: str, emit_chunk=None):
        recent_action_context = _recent_action_context(self.messages)
        q_emb, routing_input = _embedding_query(user_input, self.messages)
        need_context = q_emb is not None

        with ThreadPoolExecutor(max_workers=2) as pool:
            router_future = pool.submit(self.router.select_tools, routing_input, q_emb)
            brain_future = (
                pool.submit(self.brain.recall_context, routing_input, settings.memory_recall_k, q_emb)
                if need_context
                else None
            )

            selected_tools = router_future.result()
            context = brain_future.result() if brain_future else None

        memory_profile_context = False
        memory_context_requested = False
        proactive_context_requested = False
        conversational_context_requested = False
        if need_context:
            memory_context_requested = _should_use_memory_context(user_input, q_emb, selected_tools)
            proactive_context_requested = (
                not memory_context_requested
                and _should_use_proactive_memory_context(user_input, q_emb, selected_tools)
            )
            conversational_context_requested = (
                not selected_tools
                and not memory_context_requested
                and not proactive_context_requested
                and not _looks_like_actionable_request(user_input, q_emb)
            )
            memory_profile_context = _should_use_profile_context(
                user_input,
                q_emb,
                self._last_memory_profile_context,
            )
            if memory_profile_context:
                broad_limit = max(settings.memory_profile_limit, settings.memory_broad_recall_limit)
                profile_context = self.brain.profile_context(limit=broad_limit)
                if profile_context:
                    # Broad recall ("what do you know about me", "dump everything"):
                    # keep the query-specific recall first so a precise fact is
                    # never dropped, then add the full profile breadth behind it.
                    context = _merge_context_facts(context or "", profile_context, limit=broad_limit)

        if context and memory_profile_context:
            pass
        elif not (
            memory_context_requested
            or proactive_context_requested
            or conversational_context_requested
        ) or not _should_keep_memory_context(
            context,
            user_input,
            q_emb,
            selected_tools,
            self.router.last_decision(),
            proactive_context_requested=proactive_context_requested or conversational_context_requested,
        ):
            context = None

        if (
            context
            and selected_tools
            and _selected_tools_allow_memory_context(selected_tools)
            and not _looks_like_actionable_request(user_input, q_emb)
        ):
            selected_tools = []
        elif context and _selected_tools_are_memory_recall_only(selected_tools):
            selected_tools = []

        selected_tools = _filter_tools_for_conversation(user_input, q_emb, selected_tools)
        selected_tools = _prefer_folder_watcher_for_folder_context(user_input, q_emb, selected_tools)
        selected_tools = _maybe_reuse_latest_context_tool(user_input, selected_tools, self.messages)
        selected_tools = _ensure_local_system_control_tool(selected_tools, user_input, q_emb, self.messages)

        conversational_turn = _is_conversational_turn(selected_tools, user_input, q_emb)
        memory_facts = context if context else ""
        broad_recall = bool(memory_profile_context and memory_facts)

        # Progressive disclosure: expose discovery meta-tools only for a true
        # semantic miss. Keeping them out of normal chat and direct-tool turns
        # avoids bloating every model call while preserving the false-negative
        # escape hatch.
        router_decision = self.router.last_decision()
        progressive_names = _progressive_disclosure_tool_names()
        selected_tool_names = {t.get("name") for t in selected_tools}
        disclosure_names = []
        if selected_tool_names & set(progressive_names):
            disclosure_names = progressive_names
        elif (
            not memory_facts
            and _should_expose_progressive_disclosure(selected_tools, router_decision)
            and _looks_like_actionable_request(user_input, q_emb)
        ):
            disclosure_names = progressive_names
        loaded_tool_names: set[str] = set()
        if disclosure_names:
            existing = {t.get("name") for t in selected_tools}
            for meta_name in disclosure_names:
                if meta_name not in existing:
                    meta_tool = get_tool(meta_name)
                    if meta_tool:
                        selected_tools.append(meta_tool)

        capability_hint = None
        selected_names = {t.get("name") for t in selected_tools}
        for hint_tool in selected_names:
            hint = self.router.capability_hint(hint_tool)
            if hint.get("args"):
                capability_hint = {
                    "tool": hint_tool,
                    "args": hint["args"],
                    "direct": bool(hint.get("direct")),
                }
                break

        self.messages.append({"role": "user", "content": user_input})

        self.messages[0] = {
            "role": "system",
            "content": _build_system_prompt(
                selected_tools,
                recent_action_context,
                memory_facts=memory_facts,
                summary_context=self._combined_context(),
                conversational_turn=conversational_turn,
                broad_recall=broad_recall,
                capability_hint=capability_hint,
            ),
        }

        schema_by_name = {t["function"]["name"]: t for t in get_tool_schemas()}
        tool_schemas = [
            schema_by_name[name]
            for name in [x["name"] for x in selected_tools]
            if name in schema_by_name
        ]

        if settings.debug:
            all_names = [t["function"]["name"] for t in get_tool_schemas()]
            sel_names = [t["function"]["name"] for t in tool_schemas]
            console.print(f"[dim] All: {', '.join(all_names)}[/dim]")
            console.print(f"[dim] Selected: {', '.join(sel_names) or 'none'}[/dim]")

        tool_rounds = 0
        hallucination_retries = 0
        correction_retries = 0
        tools_called_this_input = False
        current_turn_called = []
        current_turn_tool_msgs = []
        grounding_rejected = False
        grounding_retry_pending = False
        content = ""
        forced_contextual_call = _forced_hint_tool_call(capability_hint, tool_schemas)
        if not forced_contextual_call:
            forced_contextual_call = _forced_contextual_tool_call(user_input, tool_schemas, self.messages)
        if not forced_contextual_call:
            forced_contextual_call = _forced_local_system_control_call(
                user_input,
                tool_schemas,
                self.messages,
                q_emb,
            )
        if not forced_contextual_call:
            forced_contextual_call = _forced_folder_watcher_call(user_input, tool_schemas, self.messages)

        while True:
            tool_rounds += 1

            content = ""
            tool_calls = {}
            started = False
            buffer_tool_text = bool(tool_schemas) and not tools_called_this_input

            if forced_contextual_call:
                tool_calls[0] = forced_contextual_call
                forced_contextual_call = None
            else:
                stream_attempts = max(1, settings.llm_stream_attempts)
                for attempt in range(stream_attempts):
                    try:
                        stream = self.llm.stream(self.messages, tool_schemas)
                        break
                    except RuntimeError as e:
                        if attempt < stream_attempts - 1:
                            console.print(f"[yellow]{e} Retrying...[/yellow]")
                            time.sleep(3)
                        else:
                            console.print(f"[red]{e}[/red]")
                            stream = None
                            break
                if stream is None:
                    break

                for chunk in stream:
                    if not chunk.choices:
                        continue
                    delta = chunk.choices[0].delta

                    if delta.content:
                        content += delta.content
                        if buffer_tool_text:
                            continue
                        # Never stream a leaked native tool-call token to the
                        # user. If the accumulated content starts looking like a
                        # `[TOOL_CALLS]...` leak, stop emitting and let the
                        # post-stream recovery turn it into a real call.
                        if "[TOOL_CALLS]" in content or _looks_like_tool_token_prefix(content):
                            continue
                        if not started:
                            started = True
                        print(delta.content, end="", flush=True)
                        if emit_chunk:
                            emit_chunk(delta.content)

                    if delta.tool_calls:
                        for tc in delta.tool_calls:
                            idx = tc.index
                            if idx not in tool_calls:
                                tool_calls[idx] = {"id": "", "name": "", "arguments": ""}
                            if tc.id:
                                tool_calls[idx]["id"] = tc.id
                            if tc.function:
                                if tc.function.name:
                                    tool_calls[idx]["name"] += tc.function.name
                                if tc.function.arguments:
                                    tool_calls[idx]["arguments"] += tc.function.arguments

            if started:
                print()

            if tool_calls and content:
                lines_up = max(content.count("\n") + 1, 1)
                clear = "\033[A\033[K" * lines_up
                print(f"\r{clear}\r", end="", flush=True)
                content = ""
                started = False
            elif tool_calls and not content:
                pass

            if not tool_calls and content and "[TOOL_CALLS]" in content and not tool_schemas:
                # A native tool-call token leaked but no tools were active this
                # turn (e.g. a follow-up round). Recover it against the full
                # registry so the user never sees the raw token.
                recovery_schemas = get_tool_schemas()
                token_tc, _ = _try_parse_json_tool_call(content, recovery_schemas)
                if token_tc:
                    if started:
                        lines_up = max(content.count("\n") + 1, 1)
                        clear = "\033[A\033[K" * lines_up
                        print(f"\r{clear}\r", end="")
                    started = False
                    tool_calls[0] = token_tc
                    schema = schema_by_name.get(token_tc["name"])
                    if schema and schema not in tool_schemas:
                        tool_schemas.append(schema)

            if not tool_calls and content and tool_schemas:
                json_tc, err = _try_parse_json_tool_call(content, tool_schemas)
                if json_tc:
                    if started:
                        lines_up = max(content.count("\n") + 1, 1)
                        clear = "\033[A\033[K" * lines_up
                        print(f"\r{clear}\r", end="")
                    started = False
                    tool_calls[0] = json_tc
                elif err:
                    if started:
                        lines_up = max(content.count("\n") + 1, 1)
                        clear = "\033[A\033[K" * lines_up
                        print(f"\r{clear}\r", end="")
                    if correction_retries >= settings.tool_call_retries:
                        content = err
                        if buffer_tool_text:
                            _print_assistant_content(content, emit_chunk=emit_chunk)
                        self.messages.append({"role": "assistant", "content": content})
                        break
                    correction_retries += 1
                    msg = f"{err}\nOutput your response directly without calling a tool if you cannot call one."
                    self.messages.append({"role": "system", "content": msg})
                    continue
                elif _has_backtick_tool_call(content, tool_schemas):
                    if started:
                        lines_up = max(content.count("\n") + 1, 1)
                        clear = "\033[A\033[K" * lines_up
                        print(f"\r{clear}\r", end="")
                    if correction_retries >= settings.tool_call_retries:
                        content = "I'm sorry, sir, but I cannot perform that action directly."
                        if buffer_tool_text:
                            _print_assistant_content(content, emit_chunk=emit_chunk)
                        self.messages.append({"role": "assistant", "content": content})
                        break
                    correction_retries += 1
                    available = [t["function"]["name"] for t in tool_schemas]
                    self.messages.append({
                        "role": "system",
                        "content": f"You wrote a tool command in backticks instead of calling it. Use the actual tool call mechanism for: {', '.join(available)}."
                    })
                    continue

            if not tool_calls:
                if content:
                    leak_message = _json_tool_leak_message(content, tool_schemas)
                    if leak_message:
                        content = leak_message
                    is_action = False
                    if tool_schemas and not tools_called_this_input:
                        is_action = self.verifier.verify(content, [], [], tool_schemas) != "PASS"
                    if is_action and hallucination_retries < settings.tool_call_retries:
                        hallucination_retries += 1
                        if started:
                            lines_up = max(content.count("\n") + 1, 1)
                            clear = "\033[A\033[K" * lines_up
                            print(f"\r{clear}\r", end="")
                        available = [t["function"]["name"] for t in tool_schemas]
                        self.messages.append({
                            "role": "system",
                            "content": f"You described an action without calling a tool. Available tools: {', '.join(available)}. Call one of them to actually perform the action. Try again."
                        })
                        continue
                    if buffer_tool_text:
                        _print_assistant_content(content, emit_chunk=emit_chunk)
                    self.messages.append({"role": "assistant", "content": content})
                break

            if tool_rounds >= settings.max_tool_rounds:
                print()
                console.print("[yellow]Max tool rounds reached. Stopping.[/yellow]")
                if content:
                    self.messages.append({"role": "assistant", "content": content})
                break

            formatted_calls = []
            for idx in sorted(tool_calls.keys()):
                tc = tool_calls[idx]
                # Sanitize streamed arguments before they enter history. A
                # malformed arguments string poisons the conversation: the chat
                # API 400s on every subsequent turn. Repair or fall back to "{}".
                safe_arguments = _sanitize_tool_arguments(tc["arguments"])
                formatted_calls.append({
                    "id": tc["id"],
                    "type": "function",
                    "function": {"name": tc["name"], "arguments": safe_arguments},
                })

            if settings.debug:
                for fc in formatted_calls:
                    console.print(
                        f"[dim] Tool: {_debug_safe(fc['function']['name'])}"
                        f"({_debug_safe(fc['function']['arguments'])})[/dim]"
                    )

            # If a tool call was recovered from leaked content, drop the raw
            # token text so it never persists in history or reaches the user.
            if formatted_calls and content and "[TOOL_CALLS]" in content:
                content = ""

            self.messages.append({
                "role": "assistant",
                "content": content or None,
                "tool_calls": formatted_calls,
            })

            results = {}
            executed_tool_count = 0

            with ThreadPoolExecutor(max_workers=5) as pool:
                futures = {}
                for fc in formatted_calls:
                    name = fc["function"]["name"]
                    try:
                        args = json.loads(fc["function"]["arguments"])
                    except json.JSONDecodeError as e:
                        results[fc["id"]] = f"Error: {e}"
                        continue
                    args = _repair_schema_argument_names(name, args)
                    name, args = _repair_contextual_tool_call(name, args, user_input, self.messages[:-1])
                    args = _repair_schema_argument_names(name, args)
                    fc["function"]["name"] = name
                    fc["function"]["arguments"] = json.dumps(args, ensure_ascii=False)
                    args = _prepare_tool_args_for_answer(name, args)
                    fc["function"]["arguments"] = json.dumps(args, ensure_ascii=False)
                    valid, error = self.validator.validate(name, args)
                    if not valid:
                        results[fc["id"]] = error
                        continue
                    is_discovery = name in disclosure_names
                    if not is_discovery and not _tool_call_grounded(user_input, args, self.messages, name):
                        results[fc["id"]] = "Error: Tool call rejected because its arguments were not grounded in the user request or recent tool results. Ask for the missing target instead of guessing."
                        grounding_rejected = True
                        continue
                    if is_discovery:
                        disc_args = {k: v for k, v in args.items() if k not in ("response_format", "trace_enabled")}
                        future = pool.submit(execute_tool, name, response_format="structured", **disc_args)
                    else:
                        future = pool.submit(execute_tool, name, **args)
                    futures[future] = fc
                    executed_tool_count += 1

                for future in as_completed(futures):
                    fc = futures[future]
                    try:
                        result = future.result()
                    except Exception as e:
                        result = f"Error: {e}"
                    results[fc["id"]] = _tool_result_content(result)

            if grounding_rejected and executed_tool_count == 0:
                if self.messages and self.messages[-1].get("role") == "assistant":
                    self.messages.pop()
                if settings.grounding_retry_without_tools and not grounding_retry_pending:
                    grounding_retry_pending = True
                    tool_schemas = []
                    self.messages[0] = {
                        "role": "system",
                        "content": _build_system_prompt(
                            [],
                            recent_action_context,
                            memory_facts=memory_facts,
                            summary_context=self._combined_context(),
                            conversational_turn=True,
                            broad_recall=broad_recall,
                        ),
                    }
                    self.messages.append({
                        "role": "system",
                        "content": (
                            "The previous tool attempt was not grounded in the user's message. "
                            "Answer conversationally in plain language. Do not call a tool on this turn "
                            "unless the user made a concrete actionable request."
                        ),
                    })
                    continue
                break

            for fc in formatted_calls:
                result = results.get(fc["id"], "Error: No tool result")
                if len(result) > MAX_TOOL_RESULT_CHARS:
                    result = result[:MAX_TOOL_RESULT_CHARS] + "\n...[truncated]"
                if settings.debug:
                    preview = result[:200].replace("\n", " ")
                    console.print(f"[dim] Result: {_debug_safe(preview)}[/dim]")

                self.messages.append({
                    "role": "tool",
                    "tool_call_id": fc["id"],
                    "content": result,
                })
                current_turn_called.append(fc["function"]["name"])
                current_turn_tool_msgs.append(result)

                if fc["function"]["name"] in _FILE_GENERATING_TOOLS:
                    self.router._last_generated_file = result

            tools_called_this_input = True

            # Progressive disclosure: if load_tool ran, expand the active schema
            # set with the newly loaded tools so the model can call them next
            # round. This is the core of the discovery escape hatch.
            disclosure_active = bool(disclosure_names)
            newly_loaded = []
            loaded_slug_notes = []
            if disclosure_active:
                for fc in formatted_calls:
                    if fc["function"]["name"] == "load_tool":
                        loaded_entries = _loaded_tools_from_result(results.get(fc["id"], ""))
                    elif fc["function"]["name"] == "find_tools":
                        loaded_entries = _discovered_tools_from_result(results.get(fc["id"], ""))
                    else:
                        continue
                    for entry in loaded_entries:
                        loaded_name = entry.get("name", "")
                        if not loaded_name or loaded_name in loaded_tool_names:
                            continue
                        schema = schema_by_name.get(loaded_name)
                        if schema and schema not in tool_schemas:
                            tool_schemas.append(schema)
                            loaded_tool_names.add(loaded_name)
                            newly_loaded.append(loaded_name)
                            if entry.get("tool_slug"):
                                loaded_slug_notes.append(
                                    f"{loaded_name} with action=\"execute\" tool_slug=\"{entry['tool_slug']}\""
                                )
            # When the only calls this round were discovery meta-tools, give the
            # model another round to act on what it found instead of finalizing.
            only_discovery = disclosure_active and all(
                fc["function"]["name"] in disclosure_names for fc in formatted_calls
            )
            if only_discovery:
                if newly_loaded:
                    guidance = (
                        "These tools are now callable: "
                        + ", ".join(newly_loaded)
                        + ". Call the right one with its parameters to fulfill the request."
                    )
                    if loaded_slug_notes:
                        guidance += " Use " + "; ".join(loaded_slug_notes) + "."
                    self.messages.append({"role": "system", "content": guidance})
                continue

            if len(formatted_calls) == 1:
                fc = formatted_calls[0]
                direct_answer = _direct_answer_from_tool_result(
                    fc["function"]["name"],
                    results.get(fc["id"], ""),
                )
                if direct_answer:
                    _print_assistant_content(direct_answer, emit_chunk=emit_chunk)
                    content = direct_answer
                    self.messages.append({"role": "assistant", "content": direct_answer})
                    break

            if settings.direct_single_tool_result and len(formatted_calls) == 1:
                direct_result = results.get(formatted_calls[0]["id"], "")
                # Never dump a raw structured-response envelope to the user. When
                # the single tool returned structured JSON, fall through and let
                # the model phrase it naturally instead of printing JSON.
                if (
                    direct_result
                    and not direct_result.startswith("Error")
                    and _structured_result_payload(direct_result) is None
                ):
                    if len(direct_result) > MAX_TOOL_RESULT_CHARS:
                        direct_result = direct_result[:MAX_TOOL_RESULT_CHARS] + "\n...[truncated]"
                    _print_assistant_content(direct_result, emit_chunk=emit_chunk)
                    content = direct_result
                    self.messages.append({"role": "assistant", "content": direct_result})
                    break

            if tools_called_this_input and settings.finalize_tool_results_with_llm:
                tool_schemas = []
                self.messages.append({
                    "role": "system",
                    "content": _build_tool_answer_instruction(
                        user_input,
                        [fc["function"]["name"] for fc in formatted_calls],
                    ),
                })
                continue

        if (
            tool_schemas
            and content
            and not tools_called_this_input
            and not conversational_turn
        ):
            verdict = self.verifier.verify(content, current_turn_called, current_turn_tool_msgs, tool_schemas)
            if verdict != "PASS":
                self.messages.append({
                    "role": "system",
                    "content": (
                        "Your last reply sounded like a completed action without tool evidence. "
                        "Answer again in plain conversational language for this turn."
                    ),
                })
                retry_content = ""
                try:
                    stream = self.llm.stream(self.messages, tools=None)
                    for chunk in stream:
                        if chunk.choices and chunk.choices[0].delta.content:
                            retry_content += chunk.choices[0].delta.content
                except RuntimeError:
                    retry_content = content
                if retry_content.strip():
                    if self.messages and self.messages[-1].get("role") == "assistant":
                        self.messages.pop()
                    content = retry_content.strip()
                    _print_assistant_content(content, emit_chunk=emit_chunk)
                    self.messages.append({"role": "assistant", "content": content})

        extraction_context = self._memory_extraction_context()
        if content and _should_extract_memory(user_input, q_emb):
            self._submit_background(self._extract_and_store, user_input, content, extraction_context)

        if content:
            self._remember_plain_turn(user_input, content)
            self._last_memory_profile_context = bool(memory_profile_context and context)

        # Proactive injection: run in background so it doesn't block the prompt.
        # If a proactive note is ready, it prints after the response.
        if content:
            self._submit_background(self._run_proactive, content, emit_chunk)

        self._maybe_summarize()

        return content

    def _extract_and_store(self, user_input, response, recent_user_context: str = ""):
        if not settings.memory_store_enabled:
            return
        try:
            facts = self.llm.extract_facts(user_input, response, recent_user_context=recent_user_context)
        except TypeError:
            facts = self.llm.extract_facts(user_input, response)
        if facts:
            stored = 0
            for fact in facts:
                if self.brain.commit(fact):
                    stored += 1
            if stored and (settings.memory_store_notify or settings.debug):
                console.print(f"[dim]Stored {stored} memory[/dim]")
