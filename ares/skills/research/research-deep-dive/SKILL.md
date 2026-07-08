---
name: research-deep-dive
description: Multi-source research on any topic — run targeted searches, fetch sources, evaluate quality, compile findings into a structured markdown report file. Use for "research X", "write a report on Y", "deep dive into Z".
category: research
version: 1.0.0
examples:
  - prompt: "Deep dive into the market for personal AI assistants with evidence links."
test_commands:
  - "python -m pytest tests/test_skills.py"
---

# Research Deep Dive

## Procedure

1. **Plan searches** — Break the topic into 2-4 key questions or angles. Run focused `web_search` calls for each. Don't repeat the same query — vary the wording to get different sources.

2. **Fetch depth** — For the most promising results, use `fetch_url` to get full page content where the auto-fetch snippet was insufficient.

3. **Synthesize** — Compare sources, note contradictions, identify authoritative sources, flag uncertainty.

   Build a claim-level evidence map before writing:
   - Claim
   - Supporting source URLs
   - Source freshness and authority
   - Contradictions or caveats
   - Confidence: high, medium, or low

4. **Write report** — Use `write_file` to save a structured markdown report covering:
   - ## Summary (key findings in 2-3 paragraphs)
   - ## Key Findings (detailed breakdown per research angle)
   - ## Sources (numbered list with URLs and why each is credible)
   - ## Evidence Map (claims linked to source numbers)
   - ## Open Questions (what's unclear or contradictory)

5. **Report back** — Tell the user where the report was saved and the top 3 findings.

## Rules
- At least 2 `web_search` calls before writing — one search is not deep.
- Always include source URLs in the report.
- Flag uncertainty explicitly: "Source X says A, but source Y says B — X is more recent (2026) so that's more reliable."
- Do not use your own knowledge instead of search for current topics.
