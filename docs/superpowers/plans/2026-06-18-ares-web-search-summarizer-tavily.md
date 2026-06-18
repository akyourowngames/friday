# Ares Web Search Summarizer + Tavily — Implementation Plan

**Goal:** Upgrade `web_search` so it returns structured search evidence with a concise summary and optional Tavily provider support.

---

## Tasks

- [x] Add config fields:
  - `web_search_provider`
  - `tavily_api_key`
  - `tavily_search_depth`
- [x] Redact `tavily_api_key` in exports.
- [x] Extend `ares/web.py`:
  - `tavily_search`
  - `ddgs_search`
  - `summarize_results`
  - `web_search_payload`
  - compatibility `web_search`
  - improved `format_results`
- [x] Update `ToolExecutor._web_search` to return JSON payloads.
- [x] Create `ares/renders.py` for UX overhaul renderers.
- [x] Render `web_search` summaries and numbered cards.
- [x] Update CLI tool-token parsing and renderer routing.
- [x] Update agent tool result tokens to include tool names.
- [x] Add tests for Tavily payloads, summaries, redaction, renderers, agent tokens, and CLI routing.
- [x] Run full test suite and compile checks.

## Verification

- `pytest -q`
- `python -m compileall -q ares tests`
- mocked Tavily test proves request shape and response normalization
- `web_search` without Tavily key falls back to `ddgs`
- CLI renders each tool type through the registry
