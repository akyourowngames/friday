---
name: web-research
description: Perform current web research with multiple source checks, source quality evaluation, and concise citations. Use for latest/current facts or recommendations.
category: research
version: 1.0.0
examples:
  - prompt: "Research the current safest way to do X and cite sources."
test_commands:
  - "python -m pytest tests/test_skills.py"
---

# Web Research

## Procedure
1. Break the question into focused searches.
2. Prefer primary or authoritative sources.
3. Compare dates, authorship, and source reliability.
4. Synthesize findings with links and clear uncertainty.

## Source Matrix
- Track each useful source with freshness, authority, evidence type, and contradictions.
- Prefer primary sources for rules, prices, APIs, laws, schedules, and medical/legal/financial claims.
- Label source freshness as current, dated, stale, or undated.
- Flag contradiction rows explicitly before giving the final synthesis.

## Pitfalls
- Do not rely on stale knowledge for current facts.
- Avoid over-quoting; summarize instead.
