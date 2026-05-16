import json
from concurrent.futures import ThreadPoolExecutor, as_completed

from rich.console import Console

from config import settings
from tools.registry import get_tools, execute_tool, get_tool_schemas
from .llm import NIMClient
from .router import ToolRouter
from .validator import ToolValidator
from memory.brain import Brain

console = Console()

BASE_SYSTEM = "You are KING, an AI assistant. Respond naturally in plain language. No JSON. Today: May 16, 2026."

USE_TOOLS = (
    "When you have tools available and the user's request matches what a tool does, USE IT. "
    "Don't guess or make up answers — call the tool. "
    "If the user is just chatting or sharing personal info, respond naturally without tools."
)


def _build_system_prompt(selected_tools):
    if not selected_tools:
        return BASE_SYSTEM
    lines = [BASE_SYSTEM, "", USE_TOOLS, "", "Available tools:"]
    for t in selected_tools:
        params = ", ".join(t["parameters"]["properties"])
        lines.append(f"- {t['name']}({params}): {t['description']}")
    return "\n".join(lines)


class Agent:
    def __init__(self):
        self.messages = [{"role": "system", "content": BASE_SYSTEM}]
        self.llm = NIMClient()
        self.router = ToolRouter()
        self.validator = ToolValidator()
        self.brain = Brain()

        if not self.llm.check_api_key():
            console.print("[red]NVIDIA_API_KEY not set![/red]")

        console.print("[dim]Ready[/dim]")

    def process(self, user_input: str):
        context = self.brain.recall(user_input)
        if context:
            msg = f"[Memory: {context}]\n{user_input}"
        else:
            msg = user_input

        self.messages.append({"role": "user", "content": msg})

        selected_tools = self.router.select_tools(user_input)
        self.messages[0] = {"role": "system", "content": _build_system_prompt(selected_tools)}

        tool_schemas = get_tool_schemas() if settings.debug else [
            t for t in get_tool_schemas()
            if t["function"]["name"] in {x["name"] for x in selected_tools}
        ]

        if settings.debug:
            names = [t["function"]["name"] for t in tool_schemas]
            console.print(f"[dim] Tools: {', '.join(names) or 'none'}[/dim]")

        tool_rounds = 0
        content = ""

        while True:
            tool_rounds += 1
            console.print("[dim]Thinking...[/dim]", end="\r")
            try:
                stream = self.llm.stream(self.messages, tool_schemas)
            except RuntimeError as e:
                console.print(f"[red]{e}[/red]")
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
                    print(delta.content, end="", flush=True)

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

            if not tool_calls:
                if content:
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
                if settings.debug:
                    preview = result[:200].replace("\n", " ")
                    console.print(f"[dim] Result: {preview}[/dim]")

                self.messages.append({
                    "role": "tool",
                    "tool_call_id": fc["id"],
                    "content": result,
                })

        if content and len(user_input.strip()) > 15:
            facts = self.llm.extract_facts(user_input, content)
            if facts:
                for fact in facts:
                    self.brain.commit(fact)
                console.print(f"[dim]Stored {len(facts)} memory[/dim]")

        return content
