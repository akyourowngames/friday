import json
import time
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from functools import lru_cache
from pathlib import Path

from rich.console import Console

from config import settings
from tools.registry import get_tools, execute_tool, get_tool_schemas
from .embedder import embed
from .llm import NIMClient
from .router import ToolRouter, _FILE_GENERATING_TOOLS
from .tokenizer import count_messages_tokens
from .validator import ToolValidator
from .verifier import Verifier
from memory.brain import Brain

console = Console()

PERSONA_PATH = Path(__file__).resolve().parent.parent / "persona.md"


@lru_cache(maxsize=1)
def _load_persona() -> str:
    """Cached persona loading."""
    if PERSONA_PATH.exists():
        return PERSONA_PATH.read_text(encoding="utf-8").strip()
    return "You are KING, an AI assistant. Respond naturally in plain language."


def _system_header() -> str:
    now = datetime.now().astimezone()
    return f"{_load_persona()}\nCurrent date and time: {now.strftime('%A, %B %d, %Y  %I:%M:%S %p  %Z')}."

USE_TOOLS = (
    "CRITICAL: You MUST use available tools to perform ANY action. Never just describe what you could do. "
    "If you haven't called a tool, you haven't done anything. Never fake tool results. "
    "\n"
    "TERMINAL TOOL MUST BE USED FOR: "
    "- View/open/display any file (image, video, text, document) → use terminal with 'start' or 'open' command "
    "- List/show directory contents → use terminal with 'dir', 'ls', or 'Get-ChildItem' "
    "- Execute any command on system → use terminal tool "
    "- Download/fetch files from web → use terminal (curl, wget, etc.) "
    "- Check if file exists → use terminal "
    "- Any actionable request (open, show, display, view, launch, start, run, execute) → use terminal "
    "\n"
    "CHAIN TOOLS: After a tool returns results, check what you can do next: "
    "- Got a file path? Use terminal to open/view it "
    "- Got search results? Offer to download or display them "
    "- Got directory listing? Offer to show specific files "
    "- Image was generated? Immediately open it with terminal "
    "\n"
    "ABSOLUTE RULES: "
    "1. User says 'view/show/open/display' + file → CALL TERMINAL WITH 'start <filepath>' or 'open <filepath>' "
    "2. User confirms action (yes, ok, sure) after file operation → CALL TERMINAL TO EXECUTE IT "
    "3. Never respond 'I'll open...' without actually calling terminal tool "
    "4. If tool returns error, report it and suggest alternative "
    "5. NEVER output JSON or function call syntax in your text responses. The tool calling mechanism is handled automatically by the system -- you just use it."
)

MAX_CONTEXT_TOKENS = 6000
MAX_TOOL_RESULT_CHARS = 2000
TYPING_SPEED = 0.008

CANNOT_DO = (
    "CRITICAL: If no tools are available to fulfill the user's request, "
    "you MUST explicitly say you cannot do it. Never pretend to perform an action. "
    "Never claim 'done', 'completed', 'launched', 'adjusted', etc. unless you actually "
    "called a tool and got a successful result. Say: 'I'm sorry, I don't have the ability to do that.'"
)

def _try_parse_json_tool_call(text: str, schemas: list) -> tuple:
    """Detect JSON-formatted tool call leaked as text content.
    Returns (tool_call_dict, None) on success,
    (None, error_message) if parsed but tool unknown,
    (None, None) if not a JSON tool call."""
    stripped = text.strip()
    if not stripped.startswith("{"):
        return None, None
    try:
        parsed = json.loads(stripped)
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
        for kl, ka in known.items():
            if func_name.lower() in kl or kl in func_name.lower():
                actual = ka
                break

    if not actual:
        available = ", ".join(sorted(known.values()))
        msg = f"'{func_name}' is not an available tool. Available: {available}. Use one of them."
        return None, msg

    return {
        "id": f"call_{int(time.time() * 1000)}",
        "name": actual,
        "arguments": json.dumps(func_args),
    }, None


import re as _re


def _has_backtick_tool_call(text: str, schemas: list) -> bool:
    """Detect backtick-quoted tool calls or shell commands."""
    blocks = _re.findall(r"`([^`]+)`", text)
    if not blocks:
        return False
    tool_names = {t["function"]["name"] for t in schemas}
    for block in blocks:
        stripped = block.strip()
        for name in tool_names:
            if stripped.startswith(f"{name}("):
                return True
        if _re.match(r"^(start|open|run)\s", stripped, _re.IGNORECASE):
            return True
    return False


def _build_system_prompt(selected_tools):
    lines = [_system_header(), "", USE_TOOLS]
    if selected_tools:
        lines.append("")
        lines.append("Available tools:")
        for t in selected_tools:
            params = ", ".join(t["parameters"]["properties"])
            lines.append(f"- {t['name']}({params}): {t['description']}")
    else:
        lines.append("")
        lines.append(CANNOT_DO)
    return "\n".join(lines)


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
        tools_called_this_input = False
        content = ""

        while True:
            tool_rounds += 1
            console.print("[dim]Thinking...[/dim]", end="\r")
            for attempt in range(2):
                try:
                    stream = self.llm.stream(self.messages, tool_schemas)
                    break
                except RuntimeError as e:
                    if attempt == 0:
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
                    msg = f"{err}\nOutput your response directly without calling a tool if you cannot call one."
                    self.messages.append({"role": "system", "content": msg})
                    continue
                elif _has_backtick_tool_call(content, tool_schemas):
                    lines_up = max(content.count("\n") + 1, 1)
                    clear = "\033[A\033[K" * lines_up
                    print(f"\r{clear}\r", end="")
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
                    if is_action and hallucination_retries < 2:
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
                
                # Track file-generating tools for terminal viewing
                if fc["function"]["name"] in _FILE_GENERATING_TOOLS:
                    self.router._last_generated_file = result

            tools_called_this_input = True

            if len(formatted_calls) == 1:
                direct_result = results.get(formatted_calls[0]["id"], "")
                if direct_result and not direct_result.startswith("Error"):
                    if len(direct_result) > MAX_TOOL_RESULT_CHARS:
                        direct_result = direct_result[:MAX_TOOL_RESULT_CHARS] + "\n...[truncated]"
                    print(direct_result)
                    content = direct_result
                    self.messages.append({"role": "assistant", "content": direct_result})
                    break

        if tool_schemas and content:
            called = []
            for m in self.messages:
                if m.get("tool_calls"):
                    for tc in m["tool_calls"]:
                        called.append(tc["function"]["name"])
            tool_msgs = [m["content"] for m in self.messages if m["role"] == "tool"] if called else []
            verdict = self.verifier.verify(content, called, tool_msgs, tool_schemas)
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
            for fact in facts:
                self.brain.commit(fact)
            console.print(f"[dim]Stored {len(facts)} memory[/dim]")
