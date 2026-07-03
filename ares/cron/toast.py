"""Non-intrusive toast notifications for cron job completions."""
from __future__ import annotations

from rich.console import Console
from rich.text import Text


class CronToastManager:
    """Renders a single-line toast when a cron job completes.

    Designed to be passed as a callable to CronRunner's on_complete
    callback. Under prompt_toolkit's patch_stdout(), console.print()
    renders above the active prompt without corrupting user input.
    """

    def __init__(self, console: Console):
        self.console = console

    def __call__(self, job_name: str, summary: str, status: str, duration: float):
        icon = "✅" if status == "completed" else "❌"
        text = Text()
        text.append(f"  {icon} Cron: ", style="dim")
        text.append(job_name, style="bold cyan")
        text.append(f" — {summary[:60]}", style="dim")
        text.append(f" ({duration:.1f}s)", style="dim white")
        self.console.print(text)
