# OpenAI Build Week 2026 — Ares Submission Dossier

## Recommended category

**Developer Tools**

Ares is a local-first developer assistant that combines project-aware chat, tools, memory, voice, remote channels, proactive monitoring, and bounded multi-agent execution in one runnable product.

## Submission positioning

Developers lose time moving between terminals, files, issue trackers, documentation, browsers, notes, and repetitive operational tasks. Ares keeps those workflows in one local-first assistant that can understand project context, take controlled actions, preserve memory, and delegate bounded work to specialist agents.

The strongest Build Week story is not that Ares is another coding chatbot. It is that Ares is an **agentic developer workspace** with explicit safety boundaries, durable context, proactive goal support, and a complete terminal plus web experience.

## Pre-existing project disclosure

Ares existed before the OpenAI Build Week submission period. The entry should be judged on the meaningful extension completed during the submission window, starting **July 13, 2026**.

The repository history clearly separates earlier work from Build Week additions through dated commits and merged pull requests.

## Meaningful extensions completed during the submission period

| Date (UTC) | Pull request | Build Week contribution |
|---|---|---|
| July 13 | [#23 — Add goal watcher signals and research delivery](https://github.com/akyourowngames/friday/pull/23) | Connected proactive watchers to durable goals, added reviewable signals, improved sourced web research, document extraction, and artifact delivery. |
| July 13 | [#24 — Add goal-aware watcher control plane](https://github.com/akyourowngames/friday/pull/24) | Added goal-linked watcher routes, safer evidence routing, monitor relationships, dashboard support, and regression coverage. |
| July 13–14 | [#25 — Improve Ares runtime automation](https://github.com/akyourowngames/friday/pull/25) | Added browser task control, concurrent conversations, live tool progress, hot-reload behavior, and improved document previews. |
| July 14 | [#26 — Add native multi-agent orchestration foundation](https://github.com/akyourowngames/friday/pull/26) | Added bounded parallel specialist execution, dependency-aware waves, role allowlists, timeouts, failure isolation, progress events, and artifact references. |
| July 14–15 | [#27 — Harden native multi-agent execution](https://github.com/akyourowngames/friday/pull/27) | Added mutation review boundaries, exact grants, safer builder tooling, hard timeouts, cancellation behavior, and adversarial tests. |
| July 15 | [#28 — Upgrade Ares workspace, voice, and goal systems](https://github.com/akyourowngames/friday/pull/28) | Integrated goals, commitments, reflection, memory-aware behavior, LiveKit/Sarvam telephony, workspace voice, reconnect logic, and broad regression coverage. |
| July 15 | [#29 — Complete proactive memory workflow](https://github.com/akyourowngames/friday/pull/29) | Added a durable retrieve–decide–act–update loop for deadlines, blockers, commitments, reflection follow-ups, and inactive goals. |
| July 15 | [#30 — Fix remaining proactive memory issues](https://github.com/akyourowngames/friday/pull/30) | Fixed final proactive-memory edge cases and completed the follow-up workflow. |

## Core product experience

### Understand

- Search durable facts, people, conversations, session archives, actions, and project context.
- Explain memory provenance instead of presenting unsupported recollections.
- Load reusable local skills and MCP integrations.

### Act

- Work with files, code, configured project checks, browser tasks, web research, images, schedules, phone controls, and remote channels.
- Keep consequential actions behind explicit capability and confirmation boundaries.
- Record action provenance for review.

### Stay proactive

- Track durable goals and commitments.
- Link monitoring signals to goals without silently changing goal progress.
- Surface bounded, confidence-gated follow-ups while respecting quiet hours, cooldowns, and daily caps.

### Delegate safely

- Launch isolated specialist agents for research, analysis, implementation, review, and synthesis.
- Run independent work concurrently and dependency-bound work in ordered waves.
- Keep the root agent responsible for final synthesis and consequential mutations.

## Technology

- Python 3.11+
- Next.js 16, React 19, and TypeScript
- SQLite and sqlite-vec
- OpenAI-compatible model client
- MCP integrations
- FastAPI and WebSockets
- LiveKit, faster-whisper, Edge TTS, and optional Sarvam integrations
- Pytest and focused adversarial/regression test suites

## Installation and testing path for judges

### Terminal quick start

```bash
git clone https://github.com/akyourowngames/friday.git
cd friday
git checkout ares
pip install -e ".[dev]"
python -m ares
```

### Unified runtime

```bash
python -m ares --all
```

Default local surfaces:

- Power workspace: `http://127.0.0.1:8766`
- Desktop WebSocket API: `ws://127.0.0.1:8765`
- Watcher console: `http://127.0.0.1:8080`

### Focused validation examples

```bash
python -m pytest tests/test_proactive.py -q
python -m pytest tests/test_multi_agent_smoke.py -q
python -m ares.multi_agent_smoke
```

For the web workspace:

```bash
cd ares-workspace
npm install
npm run typecheck
npm run lint
npm run build
```

## How Codex contributed

The Build Week extension was developed through a sequence of scoped branches and pull requests covering research, architecture, implementation, testing, hardening, review fixes, and documentation. The repository history above provides dated evidence of the work completed during the submission period.

Before final submission, add the exact, verified details below:

- The **`/feedback` Codex Session ID** where most core Build Week functionality was produced.
- A precise description of where **GPT-5.6** was used in the workflow or product.
- Screenshots or timestamps from the relevant session, when useful, to connect the session to the dated commits.

Do not claim GPT-5.6 usage unless it is verified from the actual session.

## Recommended demo structure — under 3 minutes

1. **Problem, 15 seconds:** developers lose focus across disconnected tools and repetitive workflows.
2. **Product overview, 20 seconds:** show the terminal and power workspace connected to the same Ares runtime.
3. **Project-aware action, 35 seconds:** ask Ares to inspect a project and run a safe tool or configured check.
4. **Multi-agent workflow, 40 seconds:** launch independent specialists and show the run manifest, progress, and synthesis.
5. **Goal-aware proactive behavior, 35 seconds:** create a goal, link a watcher, and show a reviewable signal without automatic goal mutation.
6. **Memory and follow-up, 25 seconds:** demonstrate durable recall with provenance and a bounded proactive suggestion.
7. **Codex and GPT-5.6 explanation, 30 seconds:** explain exactly what was built during Build Week and how the verified session accelerated the work.
8. **Closing, 10 seconds:** local-first, extensible, and safer than an unrestricted automation agent.

## Devpost draft copy

### Tagline

A local-first developer workspace that remembers context, acts through controlled tools, monitors goals, and delegates work to bounded specialist agents.

### Short pitch

Ares turns scattered developer workflows into one local-first assistant. It combines project-aware chat, durable memory, proactive goal monitoring, voice and remote channels, and a safety-hardened multi-agent runtime that can research, analyze, build, review, and synthesize work without giving child agents unrestricted control.

### What makes it different

Most developer assistants stop at chat or code completion. Ares joins context, action, memory, proactive support, and bounded delegation in a runnable product. It also makes safety visible: specialist agents have isolated context and role-specific tools, mutations require reviewable authorization, watcher signals cannot silently alter goals, and the root agent remains accountable for the final result.

## Final submission checklist

- [ ] The entrant/representative satisfies the official eligibility rules.
- [ ] A parent or guardian is the entrant or authorized representative where required for a participant under 18.
- [ ] The official rules and Devpost terms have been reviewed and explicitly accepted.
- [ ] The repository remains public with an appropriate license, or private access is granted to the required judging accounts.
- [ ] The README clearly explains setup, testing, Build Week extensions, Codex collaboration, and verified GPT-5.6 usage.
- [ ] The public YouTube demo is under three minutes and contains audio.
- [ ] The demo explains both Codex and GPT-5.6 usage.
- [ ] The `/feedback` Session ID is available.
- [ ] A free, practical testing path is available to judges through the end of judging.
- [ ] The Devpost project is not left as a draft.
