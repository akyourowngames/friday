# Ares Web Search Summarizer + Tavily — Design Spec

**Date:** 2026-06-18
**Status:** Approved
**Scope:** Improve `web_search` quality without adding a new public tool

---

## Problem

The first v2 web search returned raw result snippets. That works for tool execution, but the CLI can become noisy:

- snippets can run together
- the assistant receives a flat text blob instead of structured search evidence
- there is no concise answer/summary before the links
- `ddgs` is useful as a zero-key fallback but is not optimized for LLM answer synthesis

## Design

Keep one public tool: `web_search`.

Internally, make it provider-driven:

- `auto`: use Tavily when an API key is configured, otherwise `ddgs`
- `tavily`: use Tavily only and report a clear error if no key exists
- `ddgs`: use zero-key `ddgs`

Return structured JSON from the tool:

```json
{
  "query": "python 3.13 features",
  "provider": "tavily",
  "summary": "Short synthesized answer.",
  "answer": "Provider answer when available.",
  "results": [
    {"title": "...", "url": "...", "snippet": "..."}
  ],
  "errors": []
}
```

## Tavily Integration

Use Tavily's official `/search` endpoint with:

- `Authorization: Bearer <api key>`
- `query`
- `max_results`
- `search_depth`
- `include_answer: true`
- `include_raw_content: false`

API key resolution:

1. `TAVILY_API_KEY` environment variable
2. `AppConfig.tavily_api_key`

Exports must redact `tavily_api_key`.

## Summarizer

Summary priority:

1. Tavily `answer`, when present
2. Deterministic local summary from top result snippets
3. `"No summary available."`

The deterministic summary must be compact and citation-friendly:

- max 3 bullets
- each bullet derived from a search result snippet
- include source number, e.g. `[1]`

## Rendering

The CLI renderer should show:

1. Summary/answer block
2. Numbered result cards
3. Provider and query in the panel title

Invalid JSON falls back to generic rendering.

## Success Criteria

- `web_search` returns JSON payloads, not flat blobs
- Tavily works when configured
- `ddgs` still works without API keys
- summaries are present for search results
- renderer displays summary and numbered cards
- exports redact Tavily credentials
