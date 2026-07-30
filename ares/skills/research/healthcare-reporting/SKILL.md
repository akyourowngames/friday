---
name: healthcare-reporting
description: "End-to-end healthcare reporting: retrieve sources, summarize, extract trends, generate BI charts, and assemble a markdown report with optional document conversion. Use for full healthcare reports."
category: research
version: 1.0.0
examples:
  - prompt: "Generate a healthcare report on diabetes trends with charts."
---

# Healthcare Reporting

## Inputs
- Healthcare query
- Optional: output path, chart preferences, document format

## Defaults
- Output path: `~/.ares/data/healthcare-reports/<timestamp>-<slug>.md`
- Charts: enabled
- Document conversion: markdown only unless `pdf` or `docx` requested

## Procedure
1. Receive user query.
2. Invoke healthcare-retrieval workflow and obtain structured findings.
3. Invoke healthcare-summarization workflow and obtain:
   - structured summary
   - trends
   - contradictions
   - gaps
4. Validate findings:
   - at least 2 sources present
   - trends are well-formed if chart generation is enabled
5. If charts are enabled:
   - choose chart types by data shape:
     - trends over time: line chart
     - category comparisons: bar chart
     - proportions: pie chart
   - call `generate_chart` for each chart with title, labels, values, chart_type, and output path in the report directory
   - verify every referenced chart file exists before continuing
6. Assemble markdown report:
   - title and query
   - executive summary
   - key findings
   - trends section
   - contradictions/uncertainty
   - charts section with embedded or referenced image paths
   - sources
   - gaps/open questions
7. Run final validation:
   - report has required sections
   - at least one chart artifact exists if charts were requested
   - sources section is non-empty
8. If document conversion is requested and pandoc is available:
   - convert the assembled markdown report
   - return both markdown and converted output if present
9. Return report path, chart paths, and a short summary of limitations.

## Validation
- Pre-chart: trend data includes `direction`, `period`, and at least one numeric series or comparable category
- Pre-export: report markdown and all referenced chart files exist
- Final: required sections present, sources listed, artifacts accounted for

## Tooling
- Charts: `generate_chart`
- Document conversion: optional `pandoc` if available

## Rules
- Do not fabricate data to fit a chart.
- If retrieval returns weak sources, state that in the report explicitly.
- Keep outputs local-first; do not upload reports.
