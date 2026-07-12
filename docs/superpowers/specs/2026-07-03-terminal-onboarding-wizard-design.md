# Terminal Onboarding Wizard — Design Spec

**Date:** 2026-07-03
**Status:** Approved
**Author:** Claude + User

## Summary

Add an interactive terminal-based onboarding wizard to Ares that collects user identity, preferences, model choice, personality, projects, and goals on first launch. Replaces the static `FIRST_RUN_MESSAGE` with a step-by-step Rich panel wizard. Also adds a `/setup` command for re-running the wizard.

## Motivation

Currently, first-run creates empty template `profile.md` and `soul.md` files and shows a static message. The AI has zero context about the user until they manually edit files or chat. An interactive wizard gives Ares rich context from day one — name, coding style, projects, goals — making every subsequent interaction more personalized.

## Architecture

### New file: `ares/onboarding.py`

Standalone module with an `OnboardingWizard` class. Each step is a private method. Uses existing dependencies only (Rich, prompt_toolkit).

```
ares/onboarding.py
├── class OnboardingWizard
│   ├── __init__(console, config, profile_manager, soul_manager)
│   ├── run(re_run=False) → bool
│   ├── _show_welcome()
│   ├── _ask_identity() → {name, pronouns}
│   ├── _ask_preferences() → {coding_style, os, assistant_style}
│   ├── _ask_model() → model_name
│   ├── _ask_personality() → soul_content
│   ├── _ask_projects() → list[{name, description}]
│   ├── _ask_goals() → list[str]
│   ├── _show_summary(data) → confirmed, edits
│   ├── _save(data)
│   ├── _render_progress(step, total, label)
│   ├── _pick_from_list(options, prompt) → selected
│   ├── _pick_model_table(models, current) → selected
│   └── _multi_line_input(prompt, add_another=True) → list[str]
```

### Modified file: `ares/cli.py`

- Import `OnboardingWizard`
- In `AresCLI.__init__()`: after `self.profile_manager.ensure_exists()`, check if profile is populated. If empty, run wizard before showing banner.
- Add `/setup` to `COMPLETER` list
- Add `/setup` handling in command dispatch → calls `OnboardingWizard(console, config, profile_manager, soul_manager).run(re_run=True)`

### Modified file: `ares/profile.py`

- Add `is_populated() → bool` method: checks if `## Identity` section has non-empty name value.

### Modified file: `ares/prompts.py`

- `FIRST_RUN_MESSAGE` remains as fallback but will rarely be shown (wizard replaces it).

## Wizard Steps

### Step 1: Welcome

Rich panel with Ares branding:

```python
Panel(
    "[bold bright_cyan]Ares[/bold bright_cyan]\n"
    "[dim]Personal AI Assistant — First-Time Setup[/dim]\n\n"
    "Let's get to know each other so I can help you better.\n"
    "This takes about 2 minutes. You can re-run anytime with /setup.",
    border_style="bright_cyan",
    padding=(1, 2),
)
```

Press Enter to continue.

### Step 2: Identity

- "What's your name?" → text input (required, re-prompt if empty)
- "Any pronouns you'd like me to use?" → text input (optional, Enter to skip)

### Step 3: Preferences

**Coding style** — numbered list:
1. Clean & minimal
2. Verbose & documented
3. Pragmatic — whatever works
4. Type "custom" to write your own

**OS/Terminal** — auto-detect via `platform.system()` + `sys.platform`, show as default, let user confirm or type override.

**Assistant communication style** — numbered list:
1. Concise (Jarvis-style) — lead with answer, brief explanations
2. Detailed — explain reasoning, show work
3. Casual & friendly — relaxed, uses humor
4. Formal & professional — structured, polite

### Step 4: Model Selection

Table of free models from `FREE_MODELS` (imported from `ares.llm`). Current default highlighted. User picks by number.

```
  #  Model                      Status
  ─────────────────────────────────────
  1  deepseek-v4-flash-free     ← current
  2  mimo-v2.5-free
  3  qwen3.6-plus-free
  ...
```

### Step 5: Personality

Four presets:

1. **Jarvis** (default) — concise, warm, no fluff. Like a trusted advisor.
2. **Mentor** — educational, explains reasoning, patient teacher.
3. **Buddy** — casual, uses humor, relaxed friend.
4. **Custom** — prompts user to describe desired personality in 1-3 sentences, generates soul.md from that.

Each option shows a 2-line preview of what the soul will look like. After selection, shows the full soul.md content and lets user confirm or go back.

### Step 6: Projects

- "What are you currently working on?" → multi-line text input
- "Give a short description (optional)" → text input
- "Add another project? (y/n)" → loop
- Stored as markdown list items under `## Current Projects`

### Step 7: Goals

- "What are your goals right now?" → multi-line text input
- "Add another goal? (y/n)" → loop
- Stored as markdown list items under `## Goals`

### Step 8: Summary

Rich table showing all collected data:

```
  ┌─────────────────┬──────────────────────────────┐
  │ Name            │ Kabir                         │
  │ Pronouns        │ he/him                        │
  │ Coding Style    │ Pragmatic                     │
  │ OS/Terminal     │ Windows / Git Bash            │
  │ Assistant Style │ Concise (Jarvis-style)        │
  │ Model           │ deepseek-v4-flash-free        │
  │ Personality     │ Jarvis                        │
  │ Projects        │ 2 projects                    │
  │ Goals           │ 2 goals                       │
  └─────────────────┴──────────────────────────────┘

  Look good? [Y/n/r to re-edit]
```

- `Y` → save and proceed
- `n` → cancel (don't save)
- `r` → shows numbered list of steps, user picks which to re-edit, then returns to summary

### Step 9: Completion

```python
Panel(
    "[bold green]All set![/bold green] I now know who you are.\n\n"
    "Profile: ~/.ares/data/profile.md\n"
    "Personality: ~/.ares/data/soul.md\n"
    "Model: {model}\n\n"
    "Type your first message to get started!",
    border_style="green",
)
```

## Data Storage

### profile.md

```markdown
# About Me

## Identity
- Name: {name}
- Pronouns: {pronouns}

## Preferences
- Coding style: {coding_style}
- Assistant style: {assistant_style}
- Terminal/OS: {os_terminal}

## Current Projects
{for each project:}
- {name} — {description}

## Goals
{for each goal:}
- {goal}

## Notes
```

### soul.md

Written from selected preset or custom input. Preset content:

**Jarvis:**
```markdown
# Ares - My AI Assistant

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
```

**Mentor:**
```markdown
# Ares - My AI Assistant

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
```

**Buddy:**
```markdown
# Ares - My AI Assistant

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
```

### config.json

Only the `model` field is updated on save. All other config remains untouched.

## UX Details

### Progress indicator

Each step shows: `[Step 3/8] Preferences ━━━━━━━░░░░░░░░░░░ 37%`

### Input patterns

- **Required text:** re-prompt with "[red]Name is required![/red]" if empty
- **Optional text:** show dim hint "[dim]press Enter to skip[/dim]"
- **Multiple choice:** numbered list, user types number
- **Multi-line:** accept lines until empty line or "done"
- **Confirmation:** Y/n/r prompt

### Error handling

- **Ctrl+C at any step:** "Onboarding cancelled. Run `/setup` anytime to try again." — saves any completed steps if partial progress exists
- **Invalid model number:** "Please enter a valid number." — re-prompt
- **Empty name:** "I need at least a name to get started!" — re-prompt

### Re-run behavior (`/setup`)

- Pre-fills all fields with current values shown in brackets: `Name [Kabir]:`
- User presses Enter to keep current, types new value to change
- Summary shows old vs new values highlighted in yellow
- Only writes files that changed

### Skip behavior

- On re-run, user can type `/skip` at any step to skip without changing that field
- On first run, optional fields (pronouns) can be skipped with Enter

## First-Run Detection

In `AresCLI.__init__()`:

```python
if not self.profile_manager.is_populated():
    wizard = OnboardingWizard(
        console=self.console,
        config=self.config,
        profile_manager=self.profile_manager,
        soul_manager=self.soul_manager,
    )
    wizard.run()
```

`ProfileManager.is_populated()` checks if `profile.md` exists AND the line matching `- Name:` under `## Identity` has a non-empty value (i.e., not just `- Name:` or `- Name: `). This covers: fresh install (no file), template-only (empty name), and fully set up (has name).

## Integration Points

| Component | Change |
|-----------|--------|
| `ares/onboarding.py` | **New file** — entire wizard |
| `ares/cli.py` | Import wizard, add first-run check in `__init__`, add `/setup` command |
| `ares/profile.py` | Add `is_populated()` method |
| `ares/prompts.py` | No change (FIRST_RUN_MESSAGE stays as fallback) |
| `ares/models.py` | No change |
| `ares/config.py` | No change |

## Testing Strategy

1. **Unit tests** for `ProfileManager.is_populated()`
2. **Unit tests** for `OnboardingWizard._save()` — verify correct file writes
3. **Manual test** — run `python -m ares` on fresh `~/.ares/data/` directory
4. **Manual test** — run `/setup` on existing installation
5. **Edge cases** — Ctrl+C during wizard, empty inputs, re-run with changes

## Dependencies

None new. Uses existing: `rich`, `prompt_toolkit`, `platform`, `sys`.

## Scope

- This spec covers the CLI wizard only. The data format (profile.md, soul.md) is shared by every Ares surface.
- Voice mode setup is out of scope — handled separately.
