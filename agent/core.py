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


def _system_header() -> str:
    now = datetime.now().astimezone()
    return f"{_load_persona()}\nCurrent date and time: {now.strftime('%A, %B %d, %Y  %I:%M:%S %p  %Z')}."

MAX_CONTEXT_TOKENS = 6000
MAX_TOOL_RESULT_CHARS = 2000
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
    schema_texts = []
    for schema in schemas:
        function = schema.get("function", {})
        params = function.get("parameters", {}).get("properties", {})
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


def _build_system_prompt(selected_tools):
    lines = [_system_header(), "", _load_tool_policy()]
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
    return similarity >= settings.tool_argument_grounding_threshold


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
        need_context = len(user_input.strip()) >= settings.embedding_min_chars
        q_emb = embed(user_input) if need_context else None

        with ThreadPoolExecutor(max_workers=2) as pool:
            router_future = pool.submit(self.router.select_tools, user_input, q_emb)
            brain_future = pool.submit(self.brain.recall, user_input, 5, q_emb) if need_context else None

            selected_tools = router_future.result()
            context = brain_future.result() if brain_future else None

        if context:
            msg = f"[FACT (this is the truth — do not contradict or add to it): {context}]\n{user_input}"
        else:
            msg = user_input

        self.messages.append({"role": "user", "content": msg})

        self.messages[0] = {"role": "system", "content": _build_system_prompt(selected_tools)}

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

        while True:
            tool_rounds += 1
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

            content = ""
            tool_calls = {}
            started = False

            for chunk in stream:
                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta

                if delta.content:
                    if not started:
                        started = True
                        console.print("          ", end="\r")
                    content += delta.content
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

            if not tool_calls and content and tool_schemas:
                json_tc, err = _try_parse_json_tool_call(content, tool_schemas)
                if json_tc:
                    lines_up = max(content.count("\n") + 1, 1)
                    clear = "\033[A\033[K" * lines_up
                    print(f"\r{clear}\r", end="")
                    started = False
                    tool_calls[0] = json_tc
                elif err:
                    lines_up = max(content.count("\n") + 1, 1)
                    clear = "\033[A\033[K" * lines_up
                    print(f"\r{clear}\r", end="")
                    if correction_retries >= settings.tool_call_retries:
                        content = err
                        self.messages.append({"role": "assistant", "content": content})
                        break
                    correction_retries += 1
                    msg = f"{err}\nOutput your response directly without calling a tool if you cannot call one."
                    self.messages.append({"role": "system", "content": msg})
                    continue
                elif _has_backtick_tool_call(content, tool_schemas):
                    lines_up = max(content.count("\n") + 1, 1)
                    clear = "\033[A\033[K" * lines_up
                    print(f"\r{clear}\r", end="")
                    if correction_retries >= settings.tool_call_retries:
                        available = [t["function"]["name"] for t in tool_schemas]
                        content = f"I need to use one of these tools directly, sir: {', '.join(available)}."
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
                        lines_up = max(content.count("\n") + 1, 1)
                        clear = "\033[A\033[K" * lines_up
                        print(f"\r{clear}\r", end="")
                        available = [t["function"]["name"] for t in tool_schemas]
                        self.messages.append({
                            "role": "system",
                            "content": f"You described an action without calling a tool. Available tools: {', '.join(available)}. Call one of them to actually perform the action. Try again."
                        })
                        continue
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
                    results[fc["id"]] = str(result) if result is not None else "Done"

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

            if settings.direct_single_tool_result and len(formatted_calls) == 1:
                direct_result = results.get(formatted_calls[0]["id"], "")
                if direct_result and not direct_result.startswith("Error"):
                    if len(direct_result) > MAX_TOOL_RESULT_CHARS:
                        direct_result = direct_result[:MAX_TOOL_RESULT_CHARS] + "\n...[truncated]"
                    print(direct_result)
                    content = direct_result
                    self.messages.append({"role": "assistant", "content": direct_result})
                    break

        if tool_schemas and content:
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
        facts = self.llm.extract_facts(user_input, response)
        if facts:
            stored = 0
            for fact in facts:
                if self.brain.commit(fact):
                    stored += 1
            if stored:
                console.print(f"[dim]Stored {stored} memory[/dim]")
