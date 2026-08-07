"""Static terminal UI configuration shared by CLI modules."""

from prompt_toolkit.completion import WordCompleter
from prompt_toolkit.styles import Style
from rich import box


STYLE = Style.from_dict({"prompt": "bold ansicyan"})

# Safe default box for tables; CLI classes should use self._cli_box() for
# Unicode-aware selection at runtime.  Keeping this as ASCII prevents
# UnicodeEncodeError on cp1252 / legacy Windows terminals.
ASCII_BOX = box.ASCII
UNICODE_BOX = box.ROUNDED

COMPLETER = WordCompleter(
    [
        "/help", "/menu", "/memory", "/memory clean", "/memory learning", "/memory learning pending", "/memory learning active", "/memory learning approve", "/memory learning reject", "/memory explain", "/latency", "/model", "/provider", "/copilot", "/copilot login", "/copilot token", "/copilot status", "/clear",
        "/goals", "/goals search", "/goals show", "/goals due", "/goals signals",
        "/forget", "/export", "/import", "/reset", "/exit",
        "/soul", "/profile", "/user_model", "/context",
        "/skills", "/skills list", "/skills search", "/skills install", "/skills create",
        "/skills info", "/skills update", "/skills remove", "/skills publish",
        "/skills login", "/skills whoami", "/skills categories", "/skills load",
        "/setup", "/browser", "/phone", "/phone status",
        "/tools", "/tools summary", "/tools details", "/tools hidden",
        "/agents", "/agents status", "/agents active", "/agents roles",
        "/agents run", "/agents doctor", "/agents smoke-test",
        "/agents runs", "/agents show", "/agents cancel", "/agents on", "/agents off",
        "/mcp", "/mcp status", "/mcp tools", "/mcp health", "/mcp config",
        "/mcp reconnect", "/mcp reload", "/mcp search", "/mcp add", "/mcp list",
        "/mcp info", "/mcp remove", "/mcp test", "/mcp refresh",
    ],
    ignore_case=True,
)

TOOL_OUTPUT_MODES = {"summary", "details", "hidden"}
CLI_BOX = ASCII_BOX
