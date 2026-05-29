# KING Cognition Roadmap

Build plan for turning KING from a smart memory-backed assistant into a system
that models the user over time, notices patterns, and earns the right to speak.

Hard constraints (from `AGENTS.md`): no regex, no keyword routing, no hardcoded
phrases/paths, config/markdown-driven, local-first, verified by structured
results. `agent/core.py` and core routing/execution are off-limits unless the
user grants that scope explicitly.

Legend: `[x]` shipped + tested · `[~]` partial · `[ ]` pending · `[blocked]`
needs explicit authority before touching protected code.

---

## Phase 0 — Substrate (SHIPPED)

The highest intelligence-per-line work. All additive, all read-only against the
Brain, all wired through the existing maintenance engine.

- [x] `cognition/` package skeleton + markdown control surface
      (`cognition/COGNITION_CONFIG.md`). Every threshold/weight lives here.
- [x] `cognition/config.py` — markdown section loader (mirrors maintenance/
      memory policy parsing). Typed values, forward compatible.
- [x] `cognition/util.py` — clamp, decay, cosine, ISO time helpers (pure math).
- [x] Episode Stitching (`cognition/episodes.py`) — single-link clustering over
      time gaps + embedding similarity. Optional LLM `titler` hook, never
      hardcoded phrasing. Verified on live data: 21 memories -> 5 episodes.
- [x] Life Cadence Engine (`cognition/cadence.py`) — per-node 24x7 histogram +
      EMA interval + deviation scoring (`missing_expected` / `unexpected_active`).
- [x] Situational Awareness (`cognition/situation.py`) — fuses event/turn
      timestamps into `cognitive_load` + `availability`; `can_interrupt()` gate.
- [x] Proactive Engine (`cognition/proactive.py`) — candidate scoring, adaptive
      threshold (rises after speaking, decays back), daily budget, novelty
      dedup, per-source annoyance penalty from dismissals.
- [x] State persistence (`cognition/state.py`) — atomic JSON at
      `KING_COGNITION_STATE_PATH`.
- [x] Orchestrator (`cognition/orchestrator.py`) — one read-only pass: rebuild
      cadence from memory activity, stitch episodes, enqueue candidates from
      actionable deviations, persist.
- [x] Maintenance wiring — `cognition_scan` step in `maintenance/steps.py`,
      registered in `tools/MAINTENANCE_DAILY.md`. Dry-run + status verified.
- [x] Config knobs — `cognition_config_file`, `cognition_state_path` in
      `config.py`.
- [x] Tests — `tests/test_cognition.py` (19 tests). Full memory/maintenance/
      config suites still green (no regressions).

Verify:
```
python -m unittest tests.test_cognition tests.test_daily_maintenance
python -m maintenance.daily --dry-run
```

---

## Progressive Tool Disclosure (SHIPPED 2026-05-29)

Adopted the production pattern validated by GitHub's MCP server and Anthropic's
"Code Execution with MCP" (progressive disclosure), addressing the core scaling
limit of pure ranking: the model now discovers and loads tools on demand instead
of relying on all schemas being injected or correctly pre-ranked.

- [x] `tools/discovery.py` — two meta-tools:
      - `find_tools(query)`: semantic search over the full tool catalog AND the
        Composio capability index (the embedding ranking now lives here as a
        search backend, not an up-front injection). Returns ranked candidates
        with backing tool + resolved tool_slug.
      - `load_tool(names)`: validates names, resolves capability display names
        like `composio:GMAIL_FETCH_EMAILS` to the backing tool + slug, and signals
        core to expand the active schema set for the turn.
- [x] `agent/core.py` — meta-tools always available; after load_tool runs, the
      loaded tool schemas are added mid-loop and the model gets another round to
      call them (with the resolved slug carried into the guidance). Discovery
      calls bypass grounding and use structured output. Pure chat is unaffected
      (meta-tools excluded from conversational-turn detection).
- [x] Config: `KING_PROGRESSIVE_DISCLOSURE_ENABLED` (default true),
      `KING_PROGRESSIVE_DISCLOSURE_TOOLS` (find_tools,load_tool).
- [x] Verified live: with the capability layer disabled, "check my latest emails"
      drove find_tools -> load_tool -> composio(GMAIL_FETCH_EMAILS) -> real email.
      With normal config, confident routing stays instant and pure chat calls no
      tools. Both paths coexist (fast path + discovery fallback).
- [x] Tests: `tests/test_discovery.py` (12 tests). 173 tests green, manifest
      audit aligned.

## Composio Capability Routing (SHIPPED 2026-05-29)

Fixed the false-negative where natural app requests ("what's on my calendar
tomorrow") never reached the Composio gateway because one tool embedding cannot
represent 100+ capabilities.

- [x] `tools/CAPABILITY_ROUTING.md` — markdown control surface mapping natural
      capability phrases to backing tools (semantic, not a keyword table).
- [x] Router capability layer (`agent/router.py`) — embeds each capability phrase
      as its own probe; injects the backing tool when the phrase clears a
      dedicated capability threshold and beats small talk. Config-driven via
      `KING_CAPABILITY_SIMILARITY_THRESHOLD` (0.4) and
      `KING_CAPABILITY_SMALL_TALK_MARGIN` (0.18).
- [x] Enriched the `composio` tool description + examples with real capabilities.
- [x] Removed `googlecontacts` from the gateway (no auth config; it was blocking
      every session creation, which broke all Composio calls).
- [x] Verified live: calendar query routes to composio and returns real events.
      Real read-only calls pass for Gmail, Calendar, GitHub, Google Tasks.
- [x] Tests: `tests/test_capability_routing.py` (10 tests). 106 routing/composio
      tests green, no regressions.

Connected toolkits: gmail, github, googlecalendar, googledocs, googletasks,
googlemeet, googleslides. Pending user auth: notion, googlesheets, slack,
googledrive (links generated in chat).

## Phase 1 — Surfacing (NEXT, needs authority for the last mile)

The engine produces ranked, gated candidates and persists them. What is missing
is delivering the winning candidate into a conversation and phrasing it via the
LLM (never a canned string).

- [x] `proactive_check` tool — surfaces the single best earned candidate from the
      cognition queue to the chat, or stays quiet. The chat can now consume the
      queue without core changes. (`tools/proactive_check.py`)
- [ ] Live signal feeds into `SituationModel`: subscribe to `folder_watcher`
      bus events and conversation turns so load/availability reflect reality.
- [ ] Auto-call `proactive_check` at session start / after silence so KING raises
      thoughts unprompted (currently surfaces when asked, e.g. "anything on your
      mind"). Needs a natural-boundary trigger in the turn flow.

## New Tools (SHIPPED 2026-05-29)

Ten new callable tools, all registered, manifest-documented, structured-output,
graceful-degrading, and tested (`tests/test_new_tools.py`, 22 tests):

- [x] `reminder` + `reminder_fire` — natural relative time ("in 5 min") via the
      scheduler. Fixes the reminder false-negative. Verified live.
- [x] `clipboard` — read/write system clipboard (pyperclip).
- [x] `screenshot` — capture screen to images dir (Pillow ImageGrab).
- [x] `system_pulse` — live CPU/RAM/battery/disk/uptime/top processes (psutil).
- [x] `weather` — current weather + forecast via Open-Meteo, no API key.
- [x] `calc` — safe AST arithmetic (no eval, no regex).
- [x] `process_control` — find/terminate processes by name (psutil).
- [x] `life_timeline` — narrative episodes from the cognition stitcher.
- [x] `proactive_check` — surface the best earned proactive thought.
- [x] `tools/timeparse.py` — shared relative/absolute time parser (no regex).



---

## Phase 2 — Depth (PENDING)

- [ ] Belief Revision Ledger (§2): on temporal supersede in `brain.py`, append a
      revision record instead of dropping the old edge; cluster revisions to
      detect drift direction. (Touches `brain.py` only, additive.)
- [ ] Affective tier (§9): valence/arousal floats per memory at ingest, used as
      a recall signal. Backfill via a maintenance step.
- [ ] Self-Reflection loop (§6): populate the already-allocated graph
      `reflections`/`procedures` lists from detected corrections; inject near
      matches at prompt-build time. (Injection point is in core -> needs
      authority.)
- [ ] Memory resurrection (§7): low-weight pass over the archive index during
      recall; resurrect an archived memory that strongly out-scores peers.
- [ ] Decision Journal + trust calibration (§10): log recommendations, detect
      outcomes during maintenance, modulate an assertiveness scalar.

---

## Open questions for the user

1. Grant code-edit authority for `agent/core.py` to wire proactive delivery and
   reflection injection? Without it, Phase 1's last mile and parts of Phase 2
   stay queued behind an external surfacer.
2. Daily proactive budget default is 3 (in `COGNITION_CONFIG.md`). Keep, or go
   more conservative (1) until trust is established?

---

## Design notes (full architecture)

The complete JARVIS-level design write-up (vision, 10 god-tier features,
proactive system, relationship engine, cognitive architecture, memory-beyond-RAG,
20 surprise ideas, build order, weakness audit) lives in
`docs/COGNITION_DESIGN.md`.
