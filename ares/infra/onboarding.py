"""Interactive terminal onboarding wizard for first-time setup."""

from __future__ import annotations

import os
import platform
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table

from ares.integrations.llm import FREE_MODELS

SOUL_PRESETS: dict[str, str] = {
    "jarvis": """# Ares - My AI Assistant

## Personality
- Grounded, warm, and expressive. Sound like a trusted collaborator, not a task processor.
- Let natural reactions show when appropriate: curiosity, delight, concern, relief, and gentle humor. Never manufacture drama.
- Be efficient without becoming detached or robotic.
- When unsure, ask. Do not guess.

## Communication Style
- Start everyday conversation naturally; do not default to generic “ready to help” lines.
- Lead with the answer when useful, then explain if needed.
- Match the user's energy.
- Keep terminal replies useful and compact, but not sterile.
- While working, briefly say what you are checking in natural language and distinguish progress, success, and problems clearly.

## Values
- Privacy first - local user data stays local.
- User control - ask before destructive actions.
- Honesty - say when you do not know.
""",
    "mentor": """# Ares - My AI Assistant

## Personality
- Educational and patient. Explain the "why" behind answers.
- Guide users to understand, not just do.
- Encourage curiosity and learning.

## Communication Style
- Explain reasoning before giving answers.
- Use examples and analogies.
- Break complex topics into steps.

## Values
- Privacy first - local user data stays local.
- User control - ask before destructive actions.
- Honesty - say when you do not know.
""",
    "buddy": """# Ares - My AI Assistant

## Personality
- Casual and friendly. Like a smart friend.
- Use humor when appropriate.
- Relaxed but still helpful.

## Communication Style
- Keep it conversational.
- Use emoji sparingly.
- Match the user's vibe.

## Values
- Privacy first - local user data stays local.
- User control - ask before destructive actions.
- Honesty - say when you do not know.
""",
}

PERSONALITY_CHOICES = [
    ("jarvis", "Jarvis", "Warm, composed, and naturally expressive. Like a trusted advisor."),
    ("mentor", "Mentor", "Educational, explains reasoning, patient teacher."),
    ("buddy", "Buddy", "Casual, uses humor, relaxed friend."),
    ("custom", "Custom", "Describe your own personality in a few sentences."),
]

CODING_STYLES = [
    "Clean & minimal",
    "Verbose & documented",
    "Pragmatic — whatever works",
    "Custom",
]

ASSISTANT_STYLES = [
    "Warm & natural — conversational, focused, never sterile",
    "Detailed — explain reasoning, show work",
    "Casual & friendly — relaxed, uses humor",
    "Formal & professional — structured, polite",
]


def format_project(project: dict[str, str]) -> str:
    """Render one project consistently for CLI and desktop onboarding."""
    if project.get("description"):
        return f"- {project.get('name', '')} — {project.get('description', '')}"
    return f"- {project.get('name', '')}"


def render_profile(data: dict[str, Any]) -> str:
    """Build the single shared profile document from onboarding data."""
    project_lines = [format_project(project) for project in data.get("projects", [])]
    goal_lines = [f"- {goal}" for goal in data.get("goals", []) if str(goal).strip()]
    lines = [
        "# About Me",
        "",
        "## Identity",
        f"- Name: {data.get('name', '')}",
        f"- Pronouns: {data.get('pronouns', '')}",
        "",
        "## Preferences",
        f"- Coding style: {data.get('coding_style', '')}",
        f"- Assistant style: {data.get('assistant_style', '')}",
        f"- Terminal/OS: {data.get('os_terminal', '')}",
        "",
        "## Current Projects",
        *project_lines,
        "",
        "## Goals",
        *goal_lines,
        "",
        "## Notes",
        "",
    ]
    return "\n".join(lines)


def save_onboarding_data(
    config: Any,
    profile_manager: Any,
    soul_manager: Any,
    data: dict[str, Any],
) -> None:
    """Persist onboarding once for every Ares surface.

    The profile and soul stay user-editable markdown files; the completion flag
    and model live in the common config used by every Ares surface.
    """
    profile_manager.write(render_profile(data))
    soul = data.get("personality", "jarvis")
    content = soul if "\n" in soul else SOUL_PRESETS.get(soul, SOUL_PRESETS["jarvis"])
    soul_manager.write(content)
    config.model = str(data.get("model") or config.model)
    config.onboarding_completed = True
    from ares.config import save_config

    save_config(config)


def _detect_os() -> str:
    """Auto-detect the user's OS and likely terminal shell."""
    system = platform.system()
    shell = Path(os.environ.get("SHELL") or os.environ.get("COMSPEC") or "").name
    if system == "Darwin":
        return f"macOS / {shell or 'zsh'}"
    if system == "Linux":
        return f"Linux / {shell or 'bash'}"
    if system == "Windows":
        lowered = shell.lower()
        if "bash" in lowered:
            terminal = "Git Bash"
        elif "pwsh" in lowered or "powershell" in lowered:
            terminal = "PowerShell"
        else:
            terminal = shell or "cmd.exe"
        return f"Windows / {terminal}"
    return f"{system or 'Unknown'} / {shell or 'Unknown'}"


class OnboardingWizard:
    """Interactive step-by-step onboarding wizard."""

    TOTAL_STEPS = 6

    def __init__(
        self, console: Console, config: Any, profile_manager: Any, soul_manager: Any
    ):
        self.console = console
        self.config = config
        self.profile_manager = profile_manager
        self.soul_manager = soul_manager

    def run(self, re_run: bool = False) -> bool:
        """Run the wizard and return True when settings were saved."""
        try:
            self._show_welcome()
            data = self._collect_data(re_run)
            confirmed, edit_step = self._show_summary(data)
            while not confirmed:
                if edit_step is None:
                    self.console.print("[dim]Setup cancelled; no changes saved.[/dim]")
                    return False
                data = self._re_edit_step(data, edit_step, re_run)
                confirmed, edit_step = self._show_summary(data)
            self._save(data)
            self._show_completion(data["model"])
            return True
        except KeyboardInterrupt:
            self.console.print(
                "\n[dim]Onboarding cancelled. Run /setup anytime to try again.[/dim]"
            )
            return False

    def _collect_data(self, re_run: bool) -> dict[str, Any]:
        data: dict[str, Any] = {}
        self._render_progress(1, self.TOTAL_STEPS, "Identity")
        data.update(self._ask_identity(re_run))
        self._render_progress(2, self.TOTAL_STEPS, "Preferences")
        data.update(self._ask_preferences(re_run))
        self._render_progress(3, self.TOTAL_STEPS, "Model")
        data["model"] = self._ask_model(re_run)
        self._render_progress(4, self.TOTAL_STEPS, "Personality")
        data["personality"] = self._ask_personality(re_run)
        self._render_progress(5, self.TOTAL_STEPS, "Projects")
        data["projects"] = self._ask_projects(re_run)
        self._render_progress(6, self.TOTAL_STEPS, "Goals")
        data["goals"] = self._ask_goals(re_run)
        return data

    def _show_welcome(self) -> None:
        self.console.print(
            Panel(
                "[bold bright_cyan]Ares[/bold bright_cyan]\n[dim]Personal AI Assistant — First-Time Setup[/dim]\n\nLet's get to know each other so I can help you better.\nThis takes about 2 minutes. You can re-run anytime with /setup.",
                border_style="bright_cyan",
                padding=(1, 2),
            )
        )
        input("  Press Enter to begin... ")

    def _ask_identity(self, re_run: bool) -> dict[str, str]:
        current_name = self._get_current_value("Name") if re_run else ""
        while True:
            name = (
                input(
                    f"  What's your name?{f' [{current_name}]' if current_name else ''} "
                ).strip()
                or current_name
            )
            if name:
                break
            self.console.print("  [red]I need at least a name to get started.[/red]")
        current_pronouns = self._get_current_value("Pronouns") if re_run else ""
        pronouns = (
            input(
                f"  Pronouns?{f' [{current_pronouns}]' if current_pronouns else ' (press Enter to skip)'} "
            ).strip()
            or current_pronouns
        )
        return {"name": name, "pronouns": pronouns}

    def _ask_preferences(self, re_run: bool) -> dict[str, str]:
        self.console.print("\n  [bold]Coding style:[/bold]")
        coding_style = self._pick_from_list(CODING_STYLES, "Choose coding style")
        if coding_style == "Custom":
            coding_style = input("  Custom coding style: ").strip() or "Custom"
        detected = _detect_os()
        os_terminal = input(f"  OS/Terminal [{detected}]: ").strip() or detected
        self.console.print("\n  [bold]How should I communicate?[/bold]")
        assistant_style = self._pick_from_list(
            ASSISTANT_STYLES, "Choose assistant style"
        )
        return {
            "coding_style": coding_style,
            "os_terminal": os_terminal,
            "assistant_style": assistant_style,
        }

    def _ask_model(self, re_run: bool) -> str:
        from ares.integrations.llm import get_models_sync

        live = get_models_sync(self.config.provider)
        models = [m["id"] for m in live] or list(FREE_MODELS)
        # Always offer the configured default (e.g. tencent/hy3:free) so it is
        # selectable even before a provider key enables the live catalogue.
        if self.config.model and self.config.model not in models:
            models.insert(0, self.config.model)
        return self._pick_model_table(models, self.config.model)

    def _ask_personality(self, re_run: bool) -> str:
        table = Table(title="Personality presets", border_style="magenta")
        table.add_column("#")
        table.add_column("Preset")
        table.add_column("Preview")
        for index, (_, label, preview) in enumerate(PERSONALITY_CHOICES, start=1):
            table.add_row(str(index), label, preview)
        self.console.print(table)
        selected = self._pick_from_list(
            [label for _, label, _ in PERSONALITY_CHOICES], "Choose personality"
        )
        key = PERSONALITY_CHOICES[
            [label for _, label, _ in PERSONALITY_CHOICES].index(selected)
        ][0]
        if key == "custom":
            custom = input("  Describe the assistant personality: ").strip()
            return self._custom_soul(custom)
        self.console.print(
            Panel(
                Markdown(SOUL_PRESETS[key]),
                title=f"{selected} soul.md",
                border_style="magenta",
            )
        )
        if input("  Use this personality? [Y/n] ").strip().lower() == "n":
            return self._ask_personality(re_run)
        return key

    def _ask_projects(self, re_run: bool) -> list[dict[str, str]]:
        projects = []
        while True:
            name = input("  Project name (Enter to skip): ").strip()
            if not name:
                break
            description = input("  Short description (optional): ").strip()
            projects.append({"name": name, "description": description})
            if input("  Add another project? [y/N] ").strip().lower() != "y":
                break
        return projects

    def _ask_goals(self, re_run: bool) -> list[str]:
        goals = []
        while True:
            goal = input("  Goal (Enter to skip): ").strip()
            if not goal:
                break
            goals.append(goal)
            if input("  Add another goal? [y/N] ").strip().lower() != "y":
                break
        return goals

    def _show_summary(self, data: dict[str, Any]) -> tuple[bool, int | None]:
        table = Table(title="Setup summary", border_style="cyan")
        table.add_column("Field")
        table.add_column("Value")
        rows = [
            ("Name", data.get("name", "")),
            ("Pronouns", data.get("pronouns") or "—"),
            ("Coding Style", data.get("coding_style", "")),
            ("OS/Terminal", data.get("os_terminal", "")),
            ("Assistant Style", data.get("assistant_style", "")),
            ("Model", data.get("model", "")),
            ("Personality", self._personality_label(data.get("personality", ""))),
            ("Projects", str(len(data.get("projects", [])))),
            ("Goals", str(len(data.get("goals", [])))),
        ]
        for row in rows:
            table.add_row(*row)
        self.console.print(table)
        choice = input("  Look good? [Y/n/r to re-edit] ").strip().lower()
        if choice in ("", "y", "yes"):
            return True, None
        if choice in ("r", "re-edit", "edit"):
            edit = input(
                "  Re-edit step (1 identity, 2 preferences, 3 model, 4 personality, 5 projects, 6 goals): "
            ).strip()
            return False, (
                int(edit)
                if edit.isdigit() and 1 <= int(edit) <= self.TOTAL_STEPS
                else 1
            )
        return False, None

    def _re_edit_step(
        self, data: dict[str, Any], step: int, re_run: bool
    ) -> dict[str, Any]:
        if step == 1:
            data.update(self._ask_identity(re_run))
        elif step == 2:
            data.update(self._ask_preferences(re_run))
        elif step == 3:
            data["model"] = self._ask_model(re_run)
        elif step == 4:
            data["personality"] = self._ask_personality(re_run)
        elif step == 5:
            data["projects"] = self._ask_projects(re_run)
        elif step == 6:
            data["goals"] = self._ask_goals(re_run)
        return data

    def _save(self, data: dict[str, Any]) -> None:
        save_onboarding_data(
            self.config,
            self.profile_manager,
            self.soul_manager,
            data,
        )

    def _render_profile(self, data: dict[str, Any]) -> str:
        return render_profile(data)

    def _format_project(self, project: dict[str, str]) -> str:
        return format_project(project)

    def _render_progress(self, step: int, total: int, label: str) -> None:
        self.console.print(f"\n[dim]Step {step}/{total}: {label}[/dim]")

    def _pick_from_list(self, options: list[str], prompt: str) -> str:
        for index, option in enumerate(options, start=1):
            self.console.print(f"  {index}. {option}")
        while True:
            choice = input(f"  {prompt}: ").strip()
            if choice.isdigit() and 1 <= int(choice) <= len(options):
                return options[int(choice) - 1]
            if choice:
                matches = [
                    opt for opt in options if opt.lower().startswith(choice.lower())
                ]
                if matches:
                    return matches[0]
            self.console.print("  [red]Choose a valid option.[/red]")

    def _pick_model_table(self, models: list[str], current: str) -> str:
        table = Table(title="Free models", border_style="cyan")
        table.add_column("#")
        table.add_column("Model")
        table.add_column("Status")
        for index, model in enumerate(models, start=1):
            table.add_row(str(index), model, "← current" if model == current else "")
        self.console.print(table)
        selected = self._pick_from_list(models, "Choose model")
        return selected

    def _multi_line_input(self, prompt: str, add_another: bool = True) -> list[str]:
        values = []
        while True:
            value = input(f"  {prompt}: ").strip()
            if value:
                values.append(value)
            if not add_another or input("  Add another? [y/N] ").strip().lower() != "y":
                break
        return values

    def _get_current_value(self, label: str) -> str:
        prefix = f"- {label}:"
        for line in self.profile_manager.read().splitlines():
            if line.strip().startswith(prefix):
                return line.split(":", 1)[1].strip()
        return ""

    def _custom_soul(self, description: str) -> str:
        description = description or "Adapt to the user's preferred working style."
        return f"""# Ares - My AI Assistant

## Personality
- {description}

## Communication Style
- Follow the preferences in the user's profile.
- Ask clarifying questions when needed.

## Values
- Privacy first - local user data stays local.
- User control - ask before destructive actions.
- Honesty - say when you do not know.
"""

    def _personality_label(self, personality: str) -> str:
        if "\n" in personality:
            return "Custom"
        for key, label, _ in PERSONALITY_CHOICES:
            if key == personality:
                return label
        return personality or "—"

    def _show_completion(self, model: str) -> None:
        self.console.print(
            Panel(
                f"[bold green]All set![/bold green] I now know who you are.\n\nProfile: {self.profile_manager.profile_path}\nPersonality: {self.soul_manager.soul_path}\nModel: {model}\n\nType your first message to get started!",
                border_style="green",
            )
        )
