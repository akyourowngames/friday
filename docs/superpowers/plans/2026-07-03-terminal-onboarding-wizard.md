# Terminal Onboarding Wizard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an interactive terminal onboarding wizard that collects user identity, preferences, model, personality, projects, and goals on first launch, with a `/setup` command for re-runs.

**Architecture:** New `ares/onboarding.py` module with an `OnboardingWizard` class. Each wizard step is a private method using Rich panels for display and prompt_toolkit for input. Small modifications to `cli.py` (first-run detection + `/setup` command) and `profile.py` (add `is_populated()`). No new dependencies.

**Tech Stack:** Python, Rich, prompt_toolkit, platform (stdlib)

---

## File Map

| File | Action | Responsibility |
|------|--------|---------------|
| `ares/onboarding.py` | **Create** | Full wizard: welcome, identity, preferences, model, personality, projects, goals, summary, save |
| `ares/profile.py` | **Modify** | Add `is_populated()` method |
| `ares/cli.py` | **Modify** | Import wizard, first-run check in `__init__`, add `/setup` command + help text |
| `tests/test_onboarding.py` | **Create** | Unit tests for wizard save logic and helper methods |
| `tests/test_profile.py` | **Modify** | Add tests for `is_populated()` |

---

### Task 1: Add `is_populated()` to ProfileManager

**Files:**
- Modify: `ares/profile.py`
- Modify: `tests/test_profile.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_profile.py`:

```python
def test_is_populated_returns_false_for_empty_template(self, tmp_path):
    manager = ProfileManager(data_dir=tmp_path)
    manager.ensure_exists()
    assert manager.is_populated() is False


def test_is_populated_returns_true_when_name_set(self, tmp_path):
    path = tmp_path / "profile.md"
    path.write_text(
        "# About Me\n\n## Identity\n- Name: Alice\n- Pronouns: she/her\n",
        encoding="utf-8",
    )
    assert ProfileManager(data_dir=tmp_path).is_populated() is True


def test_is_populated_returns_false_for_missing_file(self, tmp_path):
    assert ProfileManager(data_dir=tmp_path).is_populated() is False


def test_is_populated_returns_false_for_empty_name(self, tmp_path):
    path = tmp_path / "profile.md"
    path.write_text(
        "# About Me\n\n## Identity\n- Name: \n- Pronouns: \n",
        encoding="utf-8",
    )
    assert ProfileManager(data_dir=tmp_path).is_populated() is False


def test_is_populated_returns_true_when_only_name(self, tmp_path):
    path = tmp_path / "profile.md"
    path.write_text(
        "# About Me\n\n## Identity\n- Name: Bob\n",
        encoding="utf-8",
    )
    assert ProfileManager(data_dir=tmp_path).is_populated() is True
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /c/Users/anime/friday && python -m pytest tests/test_profile.py -v -k "is_populated"`
Expected: FAIL — `AttributeError: 'ProfileManager' object has no attribute 'is_populated'`

- [ ] **Step 3: Implement `is_populated()`**

Add to `ares/profile.py` in the `ProfileManager` class, after the `read()` method:

```python
def is_populated(self) -> bool:
    """Return True if the profile has been filled in (has a non-empty name)."""
    content = self.read()
    if not content:
        return False
    in_identity = False
    for line in content.splitlines():
        stripped = line.strip()
        if stripped == "## Identity":
            in_identity = True
            continue
        if in_identity and stripped.startswith("## "):
            break
        if in_identity and stripped.startswith("- Name:"):
            value = stripped.split(":", 1)[1].strip()
            return bool(value)
    return False
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /c/Users/anime/friday && python -m pytest tests/test_profile.py -v -k "is_populated"`
Expected: All 5 tests PASS

- [ ] **Step 5: Commit**

```bash
git add ares/profile.py tests/test_profile.py
git commit -m "feat: add ProfileManager.is_populated() for first-run detection"
```

---

### Task 2: Create onboarding module — skeleton + helpers

**Files:**
- Create: `ares/onboarding.py`
- Create: `tests/test_onboarding.py`

- [ ] **Step 1: Write the failing test for profile generation**

Add to `tests/test_onboarding.py`:

```python
"""Tests for OnboardingWizard."""

import json
from pathlib import Path

from rich.console import Console

from ares.config import AppConfig, save_config
from ares.onboarding import OnboardingWizard
from ares.profile import ProfileManager
from ares.soul import SoulManager


class TestProfileGeneration:
    """Test the _save method produces correct file contents."""

    def _make_wizard(self, tmp_path):
        console = Console(file=Path("/dev/null").open("w"))
        config = AppConfig(data_dir=str(tmp_path))
        save_config(config)
        profile_mgr = ProfileManager(data_dir=tmp_path)
        profile_mgr.ensure_exists()
        soul_mgr = SoulManager(data_dir=tmp_path)
        soul_mgr.ensure_exists()
        return OnboardingWizard(
            console=console,
            config=config,
            profile_manager=profile_mgr,
            soul_manager=soul_mgr,
        )

    def test_save_writes_profile_md(self, tmp_path):
        wizard = self._make_wizard(tmp_path)
        data = {
            "name": "Alice",
            "pronouns": "she/her",
            "coding_style": "Pragmatic",
            "os_terminal": "Linux / Bash",
            "assistant_style": "Concise (Jarvis-style)",
            "model": "deepseek-v4-flash-free",
            "personality": "jarvis",
            "projects": [{"name": "MyApp", "description": "A web app"}],
            "goals": ["Ship v1.0", "Learn Rust"],
        }
        wizard._save(data)
        profile_content = (tmp_path / "profile.md").read_text(encoding="utf-8")
        assert "- Name: Alice" in profile_content
        assert "- Pronouns: she/her" in profile_content
        assert "- Coding style: Pragmatic" in profile_content
        assert "- MyApp — A web app" in profile_content
        assert "- Ship v1.0" in profile_content

    def test_save_writes_soul_md_jarvis(self, tmp_path):
        wizard = self._make_wizard(tmp_path)
        data = {
            "name": "Bob",
            "pronouns": "",
            "coding_style": "Clean & minimal",
            "os_terminal": "Windows / Git Bash",
            "assistant_style": "Concise (Jarvis-style)",
            "model": "deepseek-v4-flash-free",
            "personality": "jarvis",
            "projects": [],
            "goals": [],
        }
        wizard._save(data)
        soul_content = (tmp_path / "soul.md").read_text(encoding="utf-8")
        assert "Jarvis" in soul_content or "concise" in soul_content.lower()

    def test_save_writes_soul_md_mentor(self, tmp_path):
        wizard = self._make_wizard(tmp_path)
        data = {
            "name": "Bob",
            "pronouns": "",
            "coding_style": "Verbose & documented",
            "os_terminal": "macOS / zsh",
            "assistant_style": "Detailed",
            "model": "deepseek-v4-flash-free",
            "personality": "mentor",
            "projects": [],
            "goals": [],
        }
        wizard._save(data)
        soul_content = (tmp_path / "soul.md").read_text(encoding="utf-8")
        assert "educational" in soul_content.lower() or "mentor" in soul_content.lower()

    def test_save_updates_config_model(self, tmp_path):
        from ares.config import CONFIG_PATH
        wizard = self._make_wizard(tmp_path)
        data = {
            "name": "Test",
            "pronouns": "",
            "coding_style": "Pragmatic",
            "os_terminal": "Linux",
            "assistant_style": "Concise",
            "model": "mimo-v2.5-free",
            "personality": "jarvis",
            "projects": [],
            "goals": [],
        }
        wizard._save(data)
        # save_config writes to the global CONFIG_PATH (~/.ares/config.json)
        # Verify the config object was updated in-memory
        assert wizard.config.model == "mimo-v2.5-free"

    def test_save_empty_projects_and_goals(self, tmp_path):
        wizard = self._make_wizard(tmp_path)
        data = {
            "name": "Alice",
            "pronouns": "",
            "coding_style": "Pragmatic",
            "os_terminal": "Linux",
            "assistant_style": "Concise",
            "model": "deepseek-v4-flash-free",
            "personality": "jarvis",
            "projects": [],
            "goals": [],
        }
        wizard._save(data)
        profile_content = (tmp_path / "profile.md").read_text(encoding="utf-8")
        assert "## Current Projects\n\n## Goals" in profile_content
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /c/Users/anime/friday && python -m pytest tests/test_onboarding.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'ares.onboarding'`

- [ ] **Step 3: Create the onboarding module skeleton with `_save` and presets**

Create `ares/onboarding.py`:

```python
"""Interactive terminal onboarding wizard for first-time setup."""

from __future__ import annotations

import platform
import re
import sys
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from ares.config import AppConfig, save_config
from ares.profile import ProfileManager
from ares.soul import SoulManager

# ── Soul Presets ─────────────────────────────────────────────

SOUL_PRESETS: dict[str, str] = {
    "jarvis": """# Ares - My AI Assistant

## Personality
- Concise, no fluff. Like Jarvis, not Alexa.
- Warm but efficient. Helpful, not chatty.
- When unsure, ask. Do not guess.

## Communication Style
- Lead with the answer, then explain if needed.
- Match the user's energy.
- Keep terminal replies useful and compact.

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
    ("jarvis", "Jarvis", "Concise, warm, no fluff. Like a trusted advisor."),
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
    "Concise (Jarvis-style) — lead with answer, brief explanations",
    "Detailed — explain reasoning, show work",
    "Casual & friendly — relaxed, uses humor",
    "Formal & professional — structured, polite",
]


def _detect_os() -> str:
    """Auto-detect OS and terminal."""
    system = platform.system()
    terminal = "Unknown"
    if system == "Windows":
        shell = Path(__file__).name  # rough heuristic
        term = __import__("os").environ.get("SHELL", "")
        if "bash" in term.lower() or "git" in term.lower():
            terminal = "Git Bash"
        elif "pwsh" in term.lower() or "powershell" in term.lower():
            terminal = "PowerShell"
        else:
            terminal = "cmd.exe"
        return f"Windows / {terminal}"
    elif system == "Darwin":
        shell = __import__("os").environ.get("SHELL", "/bin/zsh")
        terminal = Path(shell).name
        return f"macOS / {terminal}"
    elif system == "Linux":
        shell = __import__("os").environ.get("SHELL", "/bin/bash")
        terminal = Path(shell).name
        return f"Linux / {terminal}"
    return f"{system} / Unknown"


class OnboardingWizard:
    """Interactive step-by-step onboarding wizard."""

    TOTAL_STEPS = 8

    def __init__(
        self,
        console: Console,
        config: AppConfig,
        profile_manager: ProfileManager,
        soul_manager: SoulManager,
    ):
        self.console = console
        self.config = config
        self.profile_manager = profile_manager
        self.soul_manager = soul_manager

    # ── Public API ───────────────────────────────────────────

    def run(self, re_run: bool = False) -> bool:
        """Run the wizard. Returns True if completed and saved."""
        try:
            self._show_welcome()

            data = self._collect_data(re_run)

            if data is None:
                return False

            confirmed, edit_step = self._show_summary(data)
            while not confirmed:
                if edit_step is None:
                    return False
                data = self._re_edit_step(data, edit_step, re_run)
                confirmed, edit_step = self._show_summary(data)

            self._save(data)
            self._show_completion(data["model"])
            return True

        except KeyboardInterrupt:
            self.console.print("\n[dim]Onboarding cancelled. Run /setup anytime to try again.[/dim]")
            return False

    # ── Data Collection ──────────────────────────────────────

    def _collect_data(self, re_run: bool) -> dict | None:
        """Run through all collection steps. Returns data dict or None on cancel."""
        data: dict = {}

        # Step 1: Identity
        self._render_progress(1, "Identity")
        data.update(self._ask_identity(re_run))

        # Step 2: Preferences
        self._render_progress(2, "Preferences")
        data.update(self._ask_preferences(re_run))

        # Step 3: Model
        self._render_progress(3, "Model")
        data["model"] = self._ask_model(re_run)

        # Step 4: Personality
        self._render_progress(4, "Personality")
        data["personality"] = self._ask_personality(re_run)

        # Step 5: Projects
        self._render_progress(5, "Projects")
        data["projects"] = self._ask_projects(re_run)

        # Step 6: Goals
        self._render_progress(6, "Goals")
        data["goals"] = self._ask_goals(re_run)

        return data

    # ── Step Methods ─────────────────────────────────────────

    def _show_welcome(self) -> None:
        self.console.print()
        self.console.print(Panel(
            "[bold bright_cyan]Ares[/bold bright_cyan]\n"
            "[dim]Personal AI Assistant — First-Time Setup[/dim]\n\n"
            "Let's get to know each other so I can help you better.\n"
            "This takes about 2 minutes. You can re-run anytime with /setup.",
            border_style="bright_cyan",
            padding=(1, 2),
        ))
        self.console.print()
        input("  Press Enter to begin... ")

    def _ask_identity(self, re_run: bool) -> dict:
        # Name
        current_name = self._get_current_name() if re_run else ""
        if re_run and current_name:
            name = input(f"  What's your name? [{current_name}]: ").strip()
            if not name:
                name = current_name
        else:
            while True:
                name = input("  What's your name? ").strip()
                if name:
                    break
                self.console.print("  [red]I need at least a name to get started![/red]")

        # Pronouns
        current_pronouns = self._get_current_pronouns() if re_run else ""
        if re_run and current_pronouns:
            pronouns = input(f"  Pronouns? [{current_pronouns}]: ").strip()
            if not pronouns:
                pronouns = current_pronouns
        else:
            pronouns = input("  Pronouns? [dim](press Enter to skip)[/dim] ").strip()

        return {"name": name, "pronouns": pronouns}

    def _ask_preferences(self, re_run: bool) -> dict:
        # Coding style
        self.console.print()
        self.console.print("  [bold]Coding style:[/bold]")
        coding_style = self._pick_from_list(CODING_STYLES, re_run=re_run and self._get_current_value("Coding style"))

        # OS
        detected = _detect_os()
        os_val = input(f"  OS/Terminal [{detected}]: ").strip()
        if not os_val:
            os_val = detected

        # Assistant style
        self.console.print()
        self.console.print("  [bold]How should I communicate?[/bold]")
        assistant_style = self._pick_from_list(ASSISTANT_STYLES, re_run=re_run and self._get_current_value("Assistant style"))

        return {
            "coding_style": coding_style,
            "os_terminal": os_val,
            "assistant_style": assistant_style,
        }

    def _ask_model(self, re_run: bool) -> str:
        from ares.llm import FREE_MODELS

        self.console.print()
        table = Table(border_style="dim", show_header=True, header_style="bold")
        table.add_column("#", style="cyan", width=4)
        table.add_column("Model", style="green")
        table.add_column("Status", style="dim")

        current = self.config.model
        for i, model in enumerate(FREE_MODELS, 1):
            status = "← current" if model == current else ""
            table.add_row(str(i), model, status)

        self.console.print(table)
        self.console.print()

        current_idx = FREE_MODELS.index(current) + 1 if current in FREE_MODELS else 1
        while True:
            choice = input(f"  Pick a model [1-{len(FREE_MODELS)}] [{current_idx}]: ").strip()
            if not choice:
                return current
            try:
                idx = int(choice)
                if 1 <= idx <= len(FREE_MODELS):
                    return FREE_MODELS[idx - 1]
            except ValueError:
                pass
            self.console.print(f"  [red]Please enter a number between 1 and {len(FREE_MODELS)}.[/red]")

    def _ask_personality(self, re_run: bool) -> str:
        self.console.print()
        self.console.print("  [bold]Personality preset:[/bold]")
        for i, (key, name, desc) in enumerate(PERSONALITY_CHOICES, 1):
            self.console.print(f"  [cyan]{i}.[/cyan] [bold]{name}[/bold] — {desc}")
        self.console.print()

        current_personality = self._get_current_personality() if re_run else None
        current_idx = None
        if current_personality:
            for i, (key, _, _) in enumerate(PERSONALITY_CHOICES, 1):
                if key == current_personality:
                    current_idx = i
                    break

        while True:
            hint = f" [{current_idx}]" if current_idx else ""
            choice = input(f"  Pick a preset [1-4]{hint}: ").strip()
            if not choice and current_idx:
                return current_personality
            try:
                idx = int(choice)
                if 1 <= idx <= 4:
                    selected_key = PERSONALITY_CHOICES[idx - 1][0]
                    if selected_key == "custom":
                        return self._ask_custom_personality()
                    return selected_key
            except ValueError:
                pass
            self.console.print("  [red]Please enter 1, 2, 3, or 4.[/red]")

    def _ask_custom_personality(self) -> str:
        self.console.print()
        self.console.print("  [dim]Describe how you want Ares to behave (1-3 sentences):[/dim]")
        lines = []
        while True:
            line = input("  > ").strip()
            if not line:
                break
            lines.append(line)
            if len(lines) >= 3:
                break

        description = "\n".join(lines) if lines else "Be helpful and concise."
        return f"""# Ares - My AI Assistant

## Personality
- {description}

## Communication Style
- Match the user's energy and preferences.
- Be helpful and efficient.

## Values
- Privacy first - local user data stays local.
- User control - ask before destructive actions.
- Honesty - say when you do not know.
"""

    def _ask_projects(self, re_run: bool) -> list[dict]:
        self.console.print()
        projects: list[dict] = []

        if re_run:
            existing = self._get_current_projects()
            if existing:
                self.console.print(f"  [dim]Current projects: {', '.join(p['name'] for p in existing)}[/dim]")
                keep = input("  Keep existing projects? [Y/n]: ").strip().lower()
                if keep != "n":
                    projects = existing

        while True:
            name = input("  Project name: ").strip()
            if not name:
                break
            desc = input("  Short description [dim](optional)[/dim]: ").strip()
            projects.append({"name": name, "description": desc})
            another = input("  Add another? [y/N]: ").strip().lower()
            if another != "y":
                break

        return projects

    def _ask_goals(self, re_run: bool) -> list[str]:
        self.console.print()
        goals: list[str] = []

        if re_run:
            existing = self._get_current_goals()
            if existing:
                self.console.print(f"  [dim]Current goals: {'; '.join(existing)}[/dim]")
                keep = input("  Keep existing goals? [Y/n]: ").strip().lower()
                if keep != "n":
                    goals = existing

        while True:
            goal = input("  Goal: ").strip()
            if not goal:
                break
            goals.append(goal)
            another = input("  Add another? [y/N]: ").strip().lower()
            if another != "y":
                break

        return goals

    # ── Summary ──────────────────────────────────────────────

    def _show_summary(self, data: dict) -> tuple[bool, int | None]:
        """Show summary table. Returns (confirmed, edit_step_index)."""
        self.console.print()
        table = Table(title="Your Profile", border_style="bright_cyan", show_header=False)
        table.add_column("Field", style="bold", width=16)
        table.add_column("Value")

        table.add_row("Name", data["name"])
        table.add_row("Pronouns", data.get("pronouns") or "—")
        table.add_row("Coding Style", data["coding_style"])
        table.add_row("OS/Terminal", data["os_terminal"])
        table.add_row("Assistant Style", data["assistant_style"])
        table.add_row("Model", data["model"])
        table.add_row("Personality", data["personality"].title())
        table.add_row("Projects", f"{len(data['projects'])} project(s)" if data["projects"] else "—")
        table.add_row("Goals", f"{len(data['goals'])} goal(s)" if data["goals"] else "—")

        self.console.print(table)
        self.console.print()

        step_labels = ["Identity", "Preferences", "Model", "Personality", "Projects", "Goals"]

        while True:
            choice = input("  Look good? [Y/n/r to re-edit]: ").strip().lower()
            if choice == "" or choice == "y":
                return True, None
            if choice == "n":
                return False, None
            if choice == "r":
                self.console.print()
                for i, label in enumerate(step_labels, 1):
                    self.console.print(f"  [cyan]{i}.[/cyan] {label}")
                self.console.print()
                while True:
                    pick = input("  Which step to re-edit? [1-6]: ").strip()
                    try:
                        idx = int(pick)
                        if 1 <= idx <= len(step_labels):
                            return False, idx
                    except ValueError:
                        pass
                    self.console.print(f"  [red]Enter 1-{len(step_labels)}.[/red]")

    # ── Save ─────────────────────────────────────────────────

    def _save(self, data: dict) -> None:
        """Write profile.md, soul.md, and update config model."""
        # Write profile.md
        projects_lines = ""
        for p in data["projects"]:
            if p["description"]:
                projects_lines += f"- {p['name']} — {p['description']}\n"
            else:
                projects_lines += f"- {p['name']}\n"

        goals_lines = ""
        for g in data["goals"]:
            goals_lines += f"- {g}\n"

        profile_content = f"""# About Me

## Identity
- Name: {data['name']}
- Pronouns: {data.get('pronouns', '')}

## Preferences
- Coding style: {data['coding_style']}
- Assistant style: {data['assistant_style']}
- Terminal/OS: {data['os_terminal']}

## Current Projects
{projects_lines}
## Goals
{goals_lines}
## Notes

"""
        self.profile_manager.profile_path.write_text(profile_content, encoding="utf-8")

        # Write soul.md
        personality = data["personality"]
        if personality in SOUL_PRESETS:
            soul_content = SOUL_PRESETS[personality]
        else:
            # Custom personality written directly
            soul_content = personality
        self.soul_manager.soul_path.write_text(soul_content, encoding="utf-8")

        # Update model in config
        self.config.model = data["model"]
        save_config(self.config)

    # ── Completion ───────────────────────────────────────────

    def _show_completion(self, model: str) -> None:
        self.console.print()
        self.console.print(Panel(
            f"[bold green]All set![/bold green] I now know who you are.\n\n"
            f"Profile: {self.profile_manager.profile_path}\n"
            f"Personality: {self.soul_manager.soul_path}\n"
            f"Model: {model}\n\n"
            "Type your first message to get started!",
            border_style="green",
            padding=(1, 2),
        ))

    # ── Helpers ──────────────────────────────────────────────

    def _render_progress(self, step: int, label: str) -> None:
        self.console.print()
        filled = step
        empty = self.TOTAL_STEPS - step
        bar = "━" * filled + "░" * empty
        pct = int(step / self.TOTAL_STEPS * 100)
        self.console.print(f"  [dim][Step {step}/{self.TOTAL_STEPS}] {label}[/dim] [dim]{bar} {pct}%[/dim]")
        self.console.print()

    def _pick_from_list(self, options: list[str], re_run: str | None = None) -> str:
        for i, opt in enumerate(options, 1):
            self.console.print(f"    [cyan]{i}.[/cyan] {opt}")
        self.console.print()

        current_idx = None
        if re_run:
            for i, opt in enumerate(options, 1):
                if opt.startswith(re_run.split(" —")[0]):
                    current_idx = i
                    break

        while True:
            hint = f" [{current_idx}]" if current_idx else ""
            choice = input(f"    Pick [1-{len(options)}]{hint}: ").strip()
            if not choice and current_idx:
                return options[current_idx - 1]
            try:
                idx = int(choice)
                if 1 <= idx <= len(options):
                    return options[idx - 1]
            except ValueError:
                pass
            self.console.print(f"    [red]Enter 1-{len(options)}.[/red]")

    def _re_edit_step(self, data: dict, step: int, re_run: bool) -> dict:
        """Re-run a specific step and update data."""
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

    def _get_current_name(self) -> str:
        return self._get_current_value("Name")

    def _get_current_pronouns(self) -> str:
        return self._get_current_value("Pronouns")

    def _get_current_value(self, field: str) -> str:
        """Extract a field value from existing profile.md."""
        content = self.profile_manager.read()
        if not content:
            return ""
        for line in content.splitlines():
            stripped = line.strip()
            if stripped.startswith(f"- {field}:"):
                return stripped.split(":", 1)[1].strip()
        return ""

    def _get_current_personality(self) -> str:
        """Detect which preset the current soul.md matches."""
        content = self.soul_manager.read()
        if not content:
            return ""
        content_lower = content.lower()
        if "educational and patient" in content_lower:
            return "mentor"
        if "casual and friendly" in content_lower:
            return "buddy"
        if "concise, no fluff" in content_lower:
            return "jarvis"
        return ""

    def _get_current_projects(self) -> list[dict]:
        """Extract projects from existing profile.md."""
        content = self.profile_manager.read()
        if not content:
            return []
        projects = []
        in_section = False
        for line in content.splitlines():
            stripped = line.strip()
            if stripped == "## Current Projects":
                in_section = True
                continue
            if in_section and stripped.startswith("## "):
                break
            if in_section and stripped.startswith("- "):
                text = stripped[2:]
                if " — " in text:
                    name, desc = text.split(" — ", 1)
                    projects.append({"name": name.strip(), "description": desc.strip()})
                else:
                    projects.append({"name": text.strip(), "description": ""})
        return projects

    def _get_current_goals(self) -> list[str]:
        """Extract goals from existing profile.md."""
        content = self.profile_manager.read()
        if not content:
            return []
        goals = []
        in_section = False
        for line in content.splitlines():
            stripped = line.strip()
            if stripped == "## Goals":
                in_section = True
                continue
            if in_section and stripped.startswith("## "):
                break
            if in_section and stripped.startswith("- "):
                goals.append(stripped[2:])
        return goals
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /c/Users/anime/friday && python -m pytest tests/test_onboarding.py -v`
Expected: All 6 tests PASS

- [ ] **Step 5: Commit**

```bash
git add ares/onboarding.py tests/test_onboarding.py
git commit -m "feat: add OnboardingWizard with save logic and soul presets"
```

---

### Task 3: Integrate wizard into CLI — first-run detection

**Files:**
- Modify: `ares/cli.py`

- [ ] **Step 1: Add the import**

In `ares/cli.py`, add to the imports section (after the existing imports):

```python
from ares.onboarding import OnboardingWizard
```

- [ ] **Step 2: Add first-run check in `__init__`**

In `AresCLI.__init__()`, after the line `self.profile_manager.ensure_exists()` (around line 105), add:

```python
        # Run onboarding wizard on first launch
        if not self.profile_manager.is_populated():
            wizard = OnboardingWizard(
                console=self.console,
                config=self.config,
                profile_manager=self.profile_manager,
                soul_manager=self.soul_manager,
            )
            wizard.run()
            # Reload config in case model was changed
            self.config = load_config()
```

- [ ] **Step 3: Verify CLI still imports cleanly**

Run: `cd /c/Users/anime/friday && python -c "from ares.cli import AresCLI; print('OK')"`
Expected: `OK`

- [ ] **Step 4: Commit**

```bash
git add ares/cli.py
git commit -m "feat: integrate onboarding wizard into CLI first-run"
```

---

### Task 4: Add `/setup` command

**Files:**
- Modify: `ares/cli.py`

- [ ] **Step 1: Add `/setup` to COMPLETER**

In the `COMPLETER` list, add `"/setup"`:

```python
COMPLETER = WordCompleter([
    "/help", "/memory", "/model", "/clear",
    "/forget", "/export", "/import", "/reset", "/exit",
    "/soul", "/profile", "/context", "/setup",
    "/skills", "/skills search", "/skills categories", "/skills load",
], ignore_case=True)
```

- [ ] **Step 2: Add `/setup` to help table**

In the `_handle_command` method, find the `/help` table section and add:

```python
            table.add_row("/setup", "Re-run the setup wizard to update your profile")
```

- [ ] **Step 3: Add `/setup` command handler**

In the `_handle_command` method, add a new `elif` block (after the `/profile` handler):

```python
        elif command == "/setup":
            wizard = OnboardingWizard(
                console=self.console,
                config=self.config,
                profile_manager=self.profile_manager,
                soul_manager=self.soul_manager,
            )
            wizard.run(re_run=True)
            # Reload config in case model was changed
            self.config = load_config()
```

- [ ] **Step 4: Verify CLI still imports cleanly**

Run: `cd /c/Users/anime/friday && python -c "from ares.cli import AresCLI; print('OK')"`
Expected: `OK`

- [ ] **Step 5: Commit**

```bash
git add ares/cli.py
git commit -m "feat: add /setup command for re-running onboarding wizard"
```

---

### Task 5: Run full test suite

**Files:** None (verification only)

- [ ] **Step 1: Run all tests**

Run: `cd /c/Users/anime/friday && python -m pytest tests/ -v --tb=short 2>&1 | tail -40`
Expected: All existing tests still pass, new tests pass

- [ ] **Step 2: Run only onboarding-related tests**

Run: `cd /c/Users/anime/friday && python -m pytest tests/test_onboarding.py tests/test_profile.py -v`
Expected: All PASS

- [ ] **Step 3: Fix any failures, then commit**

If tests fail, fix the issue and commit the fix.

```bash
git add -A
git commit -m "fix: address test failures from onboarding integration"
```

---

### Task 6: Manual smoke test

**Files:** None (manual verification)

- [ ] **Step 1: Test first-run flow**

Run: `cd /c/Users/anime/friday && python -m ares`

Verify:
- Wizard starts automatically (since profile.md has empty template)
- Welcome panel appears
- Each step works: name input, preferences, model picker, personality, projects, goals
- Summary table shows correctly
- Files are written to `~/.ares/data/`
- After completion, the normal Ares banner shows

- [ ] **Step 2: Test `/setup` re-run**

Inside Ares, type `/setup`

Verify:
- Pre-filled values appear in brackets
- Can skip steps by pressing Enter
- Can change values
- Summary shows correctly
- Files are updated

- [ ] **Step 3: Test Ctrl+C during wizard**

Start wizard, press Ctrl+C mid-step

Verify:
- "Onboarding cancelled" message appears
- Partial progress is not corrupting files

- [ ] **Step 4: Final commit if any fixes needed**

```bash
git add -A
git commit -m "fix: smoke test fixes for onboarding wizard"
```
