# KING Live Assistant Gauntlet

Purpose: catch user-visible assistant failures before they show up in a manual chat.

The gauntlet covers:

- local time and greeting grounding
- memory remember, list, forget, and temporary-noise filtering
- structured evidence from web, Reddit, Hacker News, file, and terminal tools
- raw payload leak checks
- real CLI subprocess conversations through `main.py`
- ambiguous follow-up behavior

## Fast Deterministic Run

```powershell
python tests\live_gauntlet.py
```

This does not depend on the LLM for conversational behavior. It checks the runtime contracts that the CLI uses.

## Real CLI Basic Run

```powershell
python tests\live_gauntlet.py --cli-basic
```

This runs `main.py` as a subprocess and sends a scripted greeting, time-of-day correction, and `/memory` command.

## Full Live Run

```powershell
python tests\live_gauntlet.py --full-live
```

This runs live CLI scenarios for web search, Hacker News, Reddit, file listing, and ambiguous follow-up handling. It is slower because it uses the configured model and external providers.

## Pass Criteria

- no raw JSON, dict syntax, tool traces, or function-call syntax in the user-facing output
- no warning leaks in normal CLI output
- tool answers are grounded in returned fields
- memory rejects temporary greeting/time corrections
- ambiguous action targets do not get guessed
