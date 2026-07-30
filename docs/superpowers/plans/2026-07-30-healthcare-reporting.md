# Healthcare Reporting Pipeline Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a hybrid healthcare reporting pipeline that retrieves online healthcare sources, summarizes findings, extracts trends, generates BI charts, and assembles a markdown/PDF report.

**Architecture:** Three new Ares skills—retrieval, summarization, and reporting—plus added chart/document MCPs in `config.json`. Retrieval and summarization stay inside Ares; charts and document conversion stay in MCPs; the reporting skill orchestrates sequencing, validation, and output assembly.

**Tech Stack:** Python 3.11+, Ares skills (`SKILL.md`), MCP stdio servers, `pytest`, existing Ares web tools.

---

### Task 1: Add healthcare document and chart MCPs to config

**Files:**
- Modify: `C:\Users\anime\.ares\config.json`
- Test: no automated config test; validate with `/mcp reload` after change

- [ ] **Step 1: Add document conversion MCPs**

Edit `C:\Users\anime\.ares\config.json` and append these entries to `mcp_servers`:

```json
{
  "name": "pandoc",
  "transport": "stdio",
  "command": "uvx",
  "args": ["mcp-pandoc"],
  "timeout_seconds": 120.0
},
{
  "name": "chart-antv",
  "transport": "stdio",
  "command": "npx",
  "args": ["-y", "@antv/mcp-server-chart@latest"],
  "timeout_seconds": 90.0
},
{
  "name": "chart-vchart",
  "transport": "stdio",
  "command": "npx",
  "args": ["-y", "@visactor/vchart-mcp-server@latest"],
  "timeout_seconds": 90.0
}
```

Keep the existing `fetch`, `playwright`, and `windows` entries unchanged.

- [ ] **Step 2: Verify config remains valid JSON**

Run: `python -c "import json; json.load(open(r'C:/Users/anime/.ares/config.json', encoding='utf-8'))"`
Expected: no output

- [ ] **Step 3: Commit**

```bash
git add -A && git commit -m "feat: add healthcare document and chart MCPs"
```

---

### Task 2: Create healthcare retrieval skill

**Files:**
- Create: `friday/ares/skills/research/healthcare-retrieval/SKILL.md`
- Test: manual via `/mcp tools fetch` and a sample query

- [ ] **Step 1: Create the skill file**

Create `friday/ares/skills/research/healthcare-retrieval/SKILL.md` with:

```markdown
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
```

- [ ] **Step 2: Validate skill file formatting**

Run: `python -c "import pathlib,sys; p=pathlib.Path(r'friday/ares/skills/research/healthcare-retrieval/SKILL.md'); print(p.exists())"`
Expected: `True`

- [ ] **Step 3: Commit**

```bash
git add friday/ares/skills/research/healthcare-retrieval/SKILL.md && git commit -m "feat: add healthcare retrieval skill"
```

---

### Task 3: Create healthcare summarization skill

**Files:**
- Create: `friday/ares/skills/research/healthcare-summarization/SKILL.md`
- Test: manual using retrieval output as input

- [ ] **Step 1: Create the skill file**

Create `friday/ares/skills/research/healthcare-summarization/SKILL.md` with:

```markdown
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
```

- [ ] **Step 2: Validate skill file exists**

Run: `python -c "import pathlib; print(pathlib.Path(r'friday/ares/skills/research/healthcare-summarization/SKILL.md').exists())"`
Expected: `True`

- [ ] **Step 3: Commit**

```bash
git add friday/ares/skills/research/healthcare-summarization/SKILL.md && git commit -m "feat: add healthcare summarization skill"
```

---

### Task 4: Create healthcare reporting orchestrator skill

**Files:**
- Create: `friday/ares/skills/research/healthcare-reporting/SKILL.md`
- Test: manual end-to-end run with a sample healthcare topic

- [ ] **Step 1: Create the skill file**

Create `friday/ares/skills/research/healthcare-reporting/SKILL.md` with:

```markdown
---
name: healthcare-reporting
description: End-to-end healthcare reporting: retrieve sources, summarize, extract trends, generate BI charts, and assemble a markdown report with optional document conversion. Use for full healthcare reports.
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
     - trends over time: line or area chart
     - category comparisons: bar chart
     - proportions: pie chart
   - send chart-ready datasets to chart MCPs
   - save chart artifacts to the same report directory
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
8. If document conversion requested:
   - call document-conversion MCP with the report markdown
   - return both markdown and converted output
9. Return report path, chart paths, and a short summary of limitations.

## Validation
- Pre-chart: trend data includes `direction`, `period`, and at least one numeric series or comparable category
- Pre-export: report markdown and all referenced chart files exist
- Final: required sections present, sources listed, artifacts accounted for

## MCP tools
- Charts: `chart-antv`, `chart-vchart`
- Document: `pandoc`

## Rules
- Do not fabricate data to fit a chart.
- If retrieval returns weak sources, state that in the report explicitly.
- Keep outputs local-first; do not upload reports.
```

- [ ] **Step 2: Validate skill file exists**

Run: `python -c "import pathlib; print(pathlib.Path(r'friday/ares/skills/research/healthcare-reporting/SKILL.md').exists())"`
Expected: `True`

- [ ] **Step 3: Commit**

```bash
git add friday/ares/skills/research/healthcare-reporting/SKILL.md && git commit -m "feat: add healthcare reporting orchestrator skill"
```

---

### Task 5: Add lightweight helper module for report assembly and validation

**Files:**
- Create: `friday/ares/tools/healthcare_reporting.py`
- Test: `tests/test_healthcare_reporting.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_healthcare_reporting.py` with:

```python
from pathlib import Path
import pytest

from ares.tools.healthcare_reporting import validate_report_output, build_report_bundle


def test_validate_report_output_requires_sections(tmp_path: Path):
    report = tmp_path / "report.md"
    report.write_text("# Title", encoding="utf-8")

    with pytest.raises(ValueError):
        validate_report_output(report, [])


def test_validate_report_output_requires_sources_section(tmp_path: Path):
    report = tmp_path / "report.md"
    report.write_text(
        "# Title\n\n## Summary\n\nBody.\n\n## Key Findings\n\n- item",
        encoding="utf-8",
    )

    with pytest.raises(ValueError):
        validate_report_output(report, [])


def test_validate_report_output_accepts_charts_and_sources(tmp_path: Path):
    report = tmp_path / "report.md"
    report.write_text(
        "# Title\n\n## Summary\n\nBody.\n\n## Key Findings\n\n- item\n\n## Sources\n\n- [Source](http://example.com)",
        encoding="utf-8",
    )
    chart = tmp_path / "chart.png"
    chart.write_bytes(b"fake-image")

    result = validate_report_output(report, [chart])

    assert result is True


def test_build_report_bundle_creates_output_path(tmp_path: Path):
    charts = []
    path = build_report_bundle(
        query="test query",
        summary_markdown="# Title\n\n## Summary\n\nBody.\n\n## Key Findings\n\n- item\n\n## Sources\n\n- [Source](http://example.com)",
        chart_paths=charts,
        output_dir=tmp_path,
    )

    assert path.exists()
    assert path.suffix == ".md"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_healthcare_reporting.py -v`
Expected: `ModuleNotFoundError: No module named 'ares.tools.healthcare_reporting'`

- [ ] **Step 3: Write minimal implementation**

Create `friday/ares/tools/healthcare_reporting.py` with:

```python
from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable, List


REQUIRED_SECTIONS = ("## Summary", "## Key Findings", "## Sources")
_section_pattern = re.compile(r"^##\s+.+$", re.MULTILINE)


class ValidationError(ValueError):
    """Raised when a healthcare report bundle fails validation."""


def validate_report_output(report_path: Path, chart_paths: Iterable[Path]) -> bool:
    sections = _section_pattern.findall(report_path.read_text(encoding="utf-8"))
    normalized_sections = [section.lower() for section in sections]

    for required in REQUIRED_SECTIONS:
        if required.lower() not in normalized_sections:
            raise ValidationError(f"Missing required section in report: {required}")

    if "## sources" not in normalized_sections:
        raise ValidationError("Missing required section in report: ## Sources")

    return True


def build_report_bundle(
    query: str,
    summary_markdown: str,
    chart_paths: Iterable[Path],
    output_dir: Path,
) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    slug = re.sub(r"[^a-z0-9]+", "-", query.lower()).strip("-")[:48] or "report"
    report_path = output_dir / f"{Path(__file__).stem}-{slug}.md"
    report_path.write_text(summary_markdown, encoding="utf-8")
    return report_path
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_healthcare_reporting.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add tests/test_healthcare_reporting.py friday/ares/tools/healthcare_reporting.py && git commit -m "feat: add healthcare report assembly and validation helpers"
```

---

### Task 6: Verify manual end-to-end behavior

**Files:**
- Modify: none
- Test: manual runtime checks

- [ ] **Step 1: Confirm MCP discovery**

Run from Ares: `/mcp status`
Expected: shows `pandoc`, `chart-antv`, `chart-vchart` in available servers

- [ ] **Step 2: Confirm skills visible**

Run from Ares: `/skills search healthcare`
Expected: lists `healthcare-retrieval`, `healthcare-summarization`, `healthcare-reporting`

- [ ] **Step 3: Run sample report query**

Preferred sample query: `"diabetes prevalence trends India 2015-2025"`
Check:
- sources are fetched
- summary is assembled
- at least one chart artifact is produced when chart path output is available
- report markdown is created in `~/.ares/data/healthcare-reports/`

- [ ] **Step 4: Commit any small follow-up fixes**

```bash
# only if needed
git add <path> && git commit -m "fix: healthcare reporting follow-up"
```

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-07-30-healthcare-reporting.md`. Two execution options:

**1. Subagent-Driven (recommended)** - I dispatch a fresh subagent per task, review between tasks, fast iteration

**2. Inline Execution** - Execute tasks in this session using executing-plans, batch execution with checkpoints

Which approach?
