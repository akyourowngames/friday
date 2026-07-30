---
name: healthcare-summarization
description: Summarize retrieved healthcare findings into structured reports with contradictions flagged and trends identified. Use after healthcare-retrieval.
category: research
version: 1.0.0
examples:
  - prompt: "Summarize these healthcare findings and identify trends."
---

# Healthcare Summarization

## Inputs
- Findings from healthcare-retrieval
- Optional: focus on temporal trends, comparisons, or specific populations

## Procedure
1. Group findings by theme, geography, population, or time period.
2. Write structured summary:
   - ## Executive Summary
   - ## Key Findings
   - ## Trends
   - ## Contradictions or Uncertainty
   - ## Source Quality Notes
   - ## Gaps
3. For trends, identify:
   - direction: `increasing`, `decreasing`, `stable`, or `unclear`
   - time range if available
   - geography or population
   - strength of evidence
4. For contradictions, list conflicting claims with their sources and trust labels.
5. Reduce findings into concise, non-technical or technical summary based on need.

## Output contract
Return:
- `summary`: structured markdown
- `trends`: list of `{direction, period, region, population, evidence}`
- `contradictions`: list of `{claim_a, claim_b, sources, likely_resolution}`
- `gaps`: list of missing data or open questions

## Rules
- Do not invent statistics not present in findings.
- Keep source provenance visible in summary sections.
- If evidence is weak for a trend, mark it `unclear` instead of forcing a direction.
