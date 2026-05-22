import json
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

console = Console()

PERSONA_PATH = Path(__file__).resolve().parent.parent / "persona.md"
TOOL_POLICY_PATH = Path(__file__).resolve().parent.parent / "tool_policy.md"
ROUTING_POLICY_PATH = Path(__file__).resolve().parent.parent / "routing_policy.md"


def _print_assistant_content(content: str) -> None:
    if not content:
        return
    console.print("          ", end="\r")
    for char in content:
        print(char, end="", flush=True)
        if char not in ("\n", "\r"):
            time.sleep(TYPING_SPEED)
    print()


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
TYPING_SPEED = 0.008

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


def _try_parse_json_tool_call(text: str, schemas: list) -> tuple:
    """Detect JSON-formatted tool call leaked as text content.
    Returns (tool_call_dict, None) on success,
    (None, error_message) if parsed but tool unknown,
    (None, None) if not a JSON tool call."""
    stripped = text.strip()
    json_str = stripped if stripped.startswith("{") else _find_json(stripped)
    if not json_str:
        return None, None
    try:
        parsed = json.loads(json_str)
    except (json.JSONDecodeError, TypeError):
        return None, None
    if not isinstance(parsed, dict):
        return None, None

    func_name = None
    func_args = None

    if "name" in parsed and "parameters" in parsed:
        func_name = parsed["name"]
        func_args = parsed["parameters"]
    elif "name" in parsed and "arguments" in parsed:
        func_name = parsed["name"]
        func_args = parsed["arguments"]
    elif parsed.get("function", {}).get("name"):
        func_name = parsed["function"]["name"]
        try:
            func_args = json.loads(parsed["function"].get("arguments", "{}"))
        except (json.JSONDecodeError, TypeError):
            func_args = {}

    if not func_name:
        return None, None

    if isinstance(func_args, str):
        try:
            func_args = json.loads(func_args)
        except (json.JSONDecodeError, TypeError):
            func_args = {}

    if not isinstance(func_args, dict):
        return None, None

    known = {t["function"]["name"].lower(): t["function"]["name"] for t in schemas}
    actual = known.get(func_name.lower())

    if not actual:
        available = ", ".join(sorted(known.values()))
        msg = f"'{func_name}' is not an available tool. Available: {available}. Use one of them."
        return None, msg

    return {
        "id": f"call_{int(time.time() * 1000)}",
        "name": actual,
        "arguments": json.dumps(func_args),
    }, None


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


def _build_system_prompt(selected_tools, recent_action_context: str = ""):
    lines = [_system_header(), "", _load_tool_policy()]
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
    else:
        lines.append("")
        lines.append(
            "No tools are selected for this turn. Answer normally for conversation; "
            "if the user requested an action, say no selected tool is available instead of pretending."
        )
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


def _looks_like_context_followup(text: str) -> bool:
    text = str(text or "").strip()
    if not text:
        return False
    followup_text, new_topic_text = _context_followup_texts()
    try:
        text_emb = embed(text)
        compare_embs = embed([followup_text, new_topic_text])
        if getattr(compare_embs, "ndim", 1) == 1:
            compare_embs = compare_embs.reshape(1, -1)
        followup_score = float(np.dot(compare_embs[0], text_emb))
        new_topic_score = float(np.dot(compare_embs[1], text_emb))
    except Exception:
        return False
    return followup_score >= new_topic_score


def _should_use_memory_context(user_input: str, q_emb, selected_tools: list) -> bool:
    if selected_tools:
        return True
    if q_emb is None:
        return False
    memory_text, small_talk_text = _memory_context_texts()
    try:
        compare_embs = embed([memory_text, small_talk_text])
        if getattr(compare_embs, "ndim", 1) == 1:
            compare_embs = compare_embs.reshape(1, -1)
        memory_score = float(np.dot(compare_embs[0], q_emb))
        small_talk_score = float(np.dot(compare_embs[1], q_emb))
    except Exception:
        return False
    return memory_score > small_talk_score


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


def _should_suppress_memory_context(router_decision: dict) -> bool:
    return router_decision.get("reason") == "small_talk_contrast_won"


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
        "When the result has a query field, describe that field as the searched query, not the user's "
        "short follow-up wording. "
        f"Tools used: {names}. User request: {user_input}"
    )


def _tool_supports_parameter(tool_name: str, parameter_name: str) -> bool:
    registered = get_tool(tool_name)
    if not registered:
        return False
    properties = registered.get("parameters", {}).get("properties", {})
    return parameter_name in properties


def _prepare_tool_args_for_answer(tool_name: str, args: dict) -> dict:
    prepared = dict(args)
    if _tool_supports_parameter(tool_name, "response_format") and "response_format" not in prepared:
        prepared["response_format"] = "structured"
    return prepared


def _tool_call_grounded(user_input: str, args: dict, messages: list, tool_name: str = "") -> bool:
    values = [tool_name] if tool_name else []
    for value in args.values():
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        values.append(str(value))
    if not values:
        return True
    grounding_text = user_input
    recent_tool_context = _recent_tool_context(messages)
    if recent_tool_context:
        grounding_text = f"{grounding_text}\n{recent_tool_context}"
    try:
        grounding_emb = embed(grounding_text)
        value_embs = embed(values)
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
        self.messages = [{"role": "system", "content": _system_header()}]
        self.llm = NIMClient()
        self.router = ToolRouter()
        self.validator = ToolValidator()
        self.verifier = Verifier()
        self.brain = Brain()
        self._executor = ThreadPoolExecutor(max_workers=3)

        if not self.llm.check_api_key():
            console.print("[red]NVIDIA_API_KEY not set![/red]")

        console.print("[dim]Ready[/dim]")

    def _maybe_summarize(self):
        if count_messages_tokens(self.messages) < MAX_CONTEXT_TOKENS:
            return

        keep = self.messages[:1]
        recent = self.messages[-4:] if len(self.messages) > 4 else self.messages[1:]
        older = self.messages[1:-4] if len(self.messages) > 4 else []

        if not older:
            return

        summary = self.llm.extract_summary(older)
        if summary:
            console.print(f"[dim]Summarized {len(older)} messages[/dim]")
            self.messages = keep + [
                {"role": "system", "content": f"Previous conversation summary: {summary}"}
            ] + recent
        else:
            self.messages = keep + recent

    def process(self, user_input: str):
        recent_action_context = _recent_action_context(self.messages)
        need_context = len(user_input.strip()) >= settings.embedding_min_chars
        q_emb = embed(user_input) if need_context else None

        with ThreadPoolExecutor(max_workers=2) as pool:
            router_future = pool.submit(self.router.select_tools, user_input, q_emb)
            brain_future = pool.submit(self.brain.recall, user_input, 5, q_emb) if need_context else None

            selected_tools = router_future.result()
            context = brain_future.result() if brain_future else None

        if _should_suppress_memory_context(self.router.last_decision()) or not _should_use_memory_context(user_input, q_emb, selected_tools):
            context = None

        selected_tools = _maybe_reuse_latest_context_tool(user_input, selected_tools, self.messages)

        if context:
            msg = f"[FACT (this is the truth — do not contradict or add to it): {context}]\n{user_input}"
        else:
            msg = user_input

        self.messages.append({"role": "user", "content": msg})

        self.messages[0] = {"role": "system", "content": _build_system_prompt(selected_tools, recent_action_context)}

        tool_schemas = [
            t for t in get_tool_schemas()
            if t["function"]["name"] in {x["name"] for x in selected_tools}
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
        content = ""
        forced_contextual_call = _forced_contextual_tool_call(user_input, tool_schemas, self.messages)

        while True:
            tool_rounds += 1

            content = ""
            tool_calls = {}
            started = False
            buffer_tool_text = bool(tool_schemas)

            if forced_contextual_call:
                tool_calls[0] = forced_contextual_call
                forced_contextual_call = None
            else:
                console.print("[dim]Thinking...[/dim]", end="\r")
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
                        if not started:
                            started = True
                            console.print("          ", end="\r")
                        for char in delta.content:
                            print(char, end="", flush=True)
                            if char not in ('\n', '\r'):
                                time.sleep(TYPING_SPEED)

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
                print(f"\r{clear}\r", end="")
                content = ""
                started = False

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
                            _print_assistant_content(content)
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
                            _print_assistant_content(content)
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
                        _print_assistant_content(content)
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
                formatted_calls.append({
                    "id": tc["id"],
                    "type": "function",
                    "function": {"name": tc["name"], "arguments": tc["arguments"]},
                })

            if settings.debug:
                for fc in formatted_calls:
                    console.print(f"[dim] Tool: {fc['function']['name']}({fc['function']['arguments']})[/dim]")

            self.messages.append({
                "role": "assistant",
                "content": content or None,
                "tool_calls": formatted_calls,
            })

            results = {}
            
            with ThreadPoolExecutor(max_workers=5) as pool:
                futures = {}
                for fc in formatted_calls:
                    name = fc["function"]["name"]
                    try:
                        args = json.loads(fc["function"]["arguments"])
                    except json.JSONDecodeError as e:
                        results[fc["id"]] = f"Error: {e}"
                        continue
                    name, args = _repair_contextual_tool_call(name, args, user_input, self.messages[:-1])
                    fc["function"]["name"] = name
                    fc["function"]["arguments"] = json.dumps(args, ensure_ascii=False)
                    args = _prepare_tool_args_for_answer(name, args)
                    fc["function"]["arguments"] = json.dumps(args, ensure_ascii=False)
                    valid, error = self.validator.validate(name, args)
                    if not valid:
                        results[fc["id"]] = error
                        continue
                    if not _tool_call_grounded(user_input, args, self.messages, name):
                        results[fc["id"]] = "Error: Tool call rejected because its arguments were not grounded in the user request or recent tool results. Ask for the missing target instead of guessing."
                        grounding_rejected = True
                        continue
                    future = pool.submit(execute_tool, name, **args)
                    futures[future] = fc

                for future in as_completed(futures):
                    fc = futures[future]
                    try:
                        result = future.result()
                    except Exception as e:
                        result = f"Error: {e}"
                    results[fc["id"]] = _tool_result_content(result)

            for fc in formatted_calls:
                result = results[fc["id"]]
                if len(result) > MAX_TOOL_RESULT_CHARS:
                    result = result[:MAX_TOOL_RESULT_CHARS] + "\n...[truncated]"
                if settings.debug:
                    preview = result[:200].replace("\n", " ")
                    console.print(f"[dim] Result: {preview}[/dim]")

                self.messages.append({
                    "role": "tool",
                    "tool_call_id": fc["id"],
                    "content": result,
                })
                current_turn_called.append(fc["function"]["name"])
                current_turn_tool_msgs.append(result)
                
                # Track file-generating tools for terminal viewing
                if fc["function"]["name"] in _FILE_GENERATING_TOOLS:
                    self.router._last_generated_file = result

            tools_called_this_input = True

            if grounding_rejected:
                content = "I need the exact target before I can use that tool, sir."
                print(content)
                self.messages.append({"role": "assistant", "content": content})
                break

            if tools_called_this_input and settings.finalize_tool_results_with_llm:
                self.messages.append({
                    "role": "system",
                    "content": _build_tool_answer_instruction(
                        user_input,
                        [fc["function"]["name"] for fc in formatted_calls],
                    ),
                })
                continue

            if settings.direct_single_tool_result and len(formatted_calls) == 1:
                direct_result = results.get(formatted_calls[0]["id"], "")
                if direct_result and not direct_result.startswith("Error"):
                    if len(direct_result) > MAX_TOOL_RESULT_CHARS:
                        direct_result = direct_result[:MAX_TOOL_RESULT_CHARS] + "\n...[truncated]"
                    print(direct_result)
                    content = direct_result
                    self.messages.append({"role": "assistant", "content": direct_result})
                    break

        if tool_schemas and content and not tools_called_this_input:
            verdict = self.verifier.verify(content, current_turn_called, current_turn_tool_msgs, tool_schemas)
            if verdict != "PASS":
                refusal = "I'm sorry, I don't have the ability to do that."
                print(f"\n[{refusal}]")
                content = refusal
                if self.messages and self.messages[-1]["role"] == "assistant":
                    self.messages[-1]["content"] = refusal

        if content and len(user_input.strip()) > 15:
            self._executor.submit(self._extract_and_store, user_input, content)

        self._maybe_summarize()

        return content

    def _extract_and_store(self, user_input, response):
        if not settings.memory_store_enabled:
            return
        facts = self.llm.extract_facts(user_input, response)
        if facts:
            stored = 0
            for fact in facts:
                if self.brain.commit(fact):
                    stored += 1
            if stored:
                console.print(f"[dim]Stored {stored} memory[/dim]")
