"""Static terminal UI configuration shared by CLI modules."""

from prompt_toolkit.completion import WordCompleter
from prompt_toolkit.styles import Style
from rich import box


STYLE = Style.from_dict({"prompt": "bold ansicyan"})

COMPLETER = WordCompleter(
    [
        "/help", "/memory", "/memory clean", "/model", "/clear",
        "/forget", "/export", "/import", "/reset", "/exit",
        "/soul", "/profile", "/context",
        "/skills", "/skills search", "/skills categories", "/skills load",
        "/setup", "/browser", "/phone", "/phone status",
        "/tools", "/tools summary", "/tools details", "/tools hidden",
        "/mcp", "/mcp status", "/mcp tools", "/mcp health", "/mcp config",
        "/mcp reconnect", "/mcp reload",
    ],
    ignore_case=True,
)

TOOL_OUTPUT_MODES = {"summary", "details", "hidden"}
CLI_BOX = box.ROUNDED
