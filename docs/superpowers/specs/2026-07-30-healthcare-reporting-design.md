# Healthcare Reporting Pipeline Design

## Summary
Build a hybrid healthcare reporting pipeline inside Ares that retrieves authoritative online healthcare material, summarizes findings, extracts trends, generates BI-style charts, and produces markdown/PDF reports.

## Goals
- Retrieve healthcare information from trusted online sources
- Summarize findings with source provenance and contradiction flags
- Extract temporal/comparative trends
- Generate BI charts: bar, line, pie, dashboard-style
- Assemble a written report with chart artifacts
- Support optional document conversion to PDF or other formats

## Chosen approach
Hybrid: retrieval and summarization as Ares skills; charting and document conversion as MCP servers; one orchestrator skill for sequencing, validation, and output assembly.

## Architecture

### Components
1. **Healthcare Retrieval Skill**
   - Input: healthcare query/topic
   - Output: structured findings with source metadata, trust labels, timestamps
   - Tools: `fetch`, `playwright`, existing web research patterns
   - Rationale: requires workflow control, source trust rules, evidence handling

2. **Healthcare Summarization Skill**
   - Input: raw findings
   - Output: structured healthcare summary with contradictions flagged, trends identified
   - Uses: Ares agent reasoning, memory, context tools
   - Rationale: needs semantic judgment and internal evidence handling

3. **BI/Chart Generation**
   - Input: normalized trend series or comparison tables
   - Output: chart artifacts
   - MCPs: `antvis/mcp-server-chart`, `VisActor/vchart-mcp-server`, optional `quickchart-mcp-server`
   - Rationale: generic rendering concern, easy to swap/extend

4. **Document/Conversion Rendering**
   - Input: markdown report + chart paths
   - Output: markdown bundle, optional PDF/docs
   - MCPs: `vivekVells/mcp-pandoc`, optional `Tele-AI/doc-ops-mcp`
   - Rationale: keeps reporting consistent without custom formatting code

5. **Healthcare Reporting Orchestrator Skill**
   - Input: healthcare query
   - Output: report bundle with charts
   - Responsibilities: validate inputs, sequence pipeline steps, assemble markdown, invoke document conversion, run validation pass
   - Does not perform retrieval/summarization/charting itself

## Data Flow
1. User provides a healthcare query
2. Retrieval skill fetches and filters sources
3. Summarization skill condenses findings and identifies trends
4. Trend data is normalized into chart-ready series
5. Chart MCPs render artifacts
6. Markdown report is assembled with chart paths
7. Optional document MCP converts to final format
8. Orchestrator validates output completeness and returns the bundle

## Validation
- Pre-chart: validate trend data structure and chart-type appropriateness
- Pre-export: validate markdown report and referenced chart files exist
- Final: lightweight self-check confirming report sections, at least one chart artifact, and source list

## Configuration
- MCP servers added to `~/.ares/config.json` under `mcp_servers`
- Required chart/document MCPs only; retrieval uses existing Ares tooling
- No secrets or credentials required for read-only public sources

## Files
- `friday/ares/skills/research/healthcare-retrieval/SKILL.md`
- `friday/ares/skills/research/healthcare-summarization/SKILL.md`
- `friday/ares/skills/research/healthcare-reporting/SKILL.md`
- `~/.ares/config.json` updated with chart/document MCPs
- `friday/ares/tools/healthcare_reporting.py` if shared validation/helpers are needed

## Implementation Order
1. Add MCP servers to `config.json`
2. Create `healthcare-retrieval` skill
3. Create `healthcare-summarization` skill
4. Create `healthcare-reporting` orchestrator skill
5. Verify with a sample healthcare query covering retrieval, summarization, charts, and report assembly

## Non-goals / deferred
- Custom PDF rendering engine
- Clinical EHR integration
- HIPAA-specific compliance tooling
- Real-time streaming chart updates
- Authentication-required healthcare APIs unless later requested
