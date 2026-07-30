---
name: healthcare-retrieval
description: Retrieve authoritative healthcare information from online sources with trust labels, source metadata, and timestamps. Use for "research healthcare topic X" or "find online sources on Y".
category: research
version: 1.0.0
examples:
  - prompt: "Research current diabetes prevalence trends in India with sources."
---

# Healthcare Retrieval

## Inputs
- Query
- Optional: trusted domains, date range, source types

## Procedure
1. Break the query into 2-4 focused healthcare-oriented searches.
2. Prefer primary sources: WHO, CDC, NIH, PubMed, national health portals, peer-reviewed summaries.
3. For each promising result, fetch the source and extract:
   - title
   - URL
   - publication or last-updated date
   - author or organization
   - key statistics or claims
   - study design or evidence type if present
4. Assign trust labels:
   - `high`: official public-health or peer-reviewed source
   - `medium`: reputable medical/news source with clear methodology
   - `low`: opinion, secondary summary, or undated source
5. Build structured findings:
   - claim or finding text
   - supporting source URLs
   - date
   - trust label
   - population or geography if present
6. Return findings plus a source metadata table.

## Output contract
Return:
- `findings`: list of objects with `claim`, `sources`, `date`, `trust`, `population`
- `sources`: numbered source list with `url`, `title`, `organization`, `date`, `trust`
- `note`: one-line summary of source quality and gaps

## Rules
- Use at least 2 sources before summarizing.
- Do not replace current-source retrieval with prior knowledge.
- Flag missing dates, conflicting numbers, or unclear populations explicitly.
