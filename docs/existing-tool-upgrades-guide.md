# Existing-tool upgrades guide

This guide describes the additive upgrade layer introduced for Ares's existing
tools.  It is designed for callers that need plans, verification metadata, and
machine-readable results without changing a working legacy integration.

## Compatibility contract

- Tool names and legacy required arguments are unchanged.
- A call that uses only the legacy arguments keeps its legacy response shape.
- Set `response_format` to `"structured"` to opt into the common response
  envelope.  Some tools also enter an advanced path when an explicitly new
  argument such as `preview`, `mode`, or `variations` is present.
- Previews are side-effect free.  A preview returns the exact follow-up tool
  call in `next_actions` instead of making a mutation.

Every structured result has exactly these top-level fields:

```text
ok, status, summary, data, artifacts, warnings, errors,
next_actions, provenance, metrics, undo_id
```

`status` is one of `completed`, `partial`, `failed`, `conflict`, `preview`, or
`not_found`.  Consumers should use `ok` for a boolean decision and retain
`warnings`/`errors` rather than parsing the human-readable `summary`.

## High-value opt-in paths

| Family | Start with | What it adds |
| --- | --- | --- |
| Memory and people | `response_format="structured"` | source/provenance, merge and revision states, relationship links, guarded ambiguity resolution |
| Files and runtime | `mode` + `response_format="structured"` | plans, selectors, bounded sessions/jobs, artifacts, checks and undo metadata |
| Web and research | `search_mode` or `response_format="structured"` | source-aware research projections, extraction selection, cache/continuation context |
| Cron, watcher, phone | `preview=true` + `response_format="structured"` | schedule/policy previews, health and event detail, preflight/confirmation states |
| Delegation | `budget`, `evidence_contract`, or `response_format="structured"` | DAG plan, bounded specialist resources, evidence checks, progress and resume metadata |
| Skills and MCP | `task`, `preview`, or `response_format="structured"` | ranked skills, linted draft generation, manifest risk summaries, bounded MCP metadata/cache/pagination |
| Images | `preview=true` or `response_format="structured"` | reproducible generation manifests, validated transform plans, actual output metadata and artifacts |
| Action history | filters plus `response_format="structured"` | privacy-minimized action page, timeline, inferred chains, summary, cursor and date filters |
| Export | `preview=true` + `response_format="structured"` | redaction plan, checksums, section manifest, incremental base information, verification and optional encryption |

## Examples

### Plan a multi-agent task before launching it

```json
{
  "task": "Audit the release checklist and propose missing tests",
  "budget": {"max_agents": 2, "max_runtime_seconds": 300},
  "evidence_contract": {"required_artifact_kinds": ["report"]},
  "response_format": "structured"
}
```

The result exposes a dependency-aware launch plan.  A completed run is marked
`partial` or `failed` if its evidence contract is not satisfied; it is not
silently reported as a success.

### Preview an image transformation

```json
{
  "path": "assets/hero.png",
  "width": 1600,
  "height": 900,
  "fit": "contain",
  "preview": true,
  "response_format": "structured"
}
```

The preview returns source dimensions, target dimensions, warnings, and a
safe replay action.  Re-submit with `preview: false` to write the output; Ares
then decodes the written file and verifies it against the plan.

### Preview and verify a redacted export

```json
{
  "profile": "full",
  "redact": true,
  "include_categories": ["memories", "actions"],
  "since": "2026-07-01T00:00:00Z",
  "preview": true,
  "response_format": "structured"
}
```

The preview shows redaction paths, checksums, section counts, and records
omitted because they have no timestamp inside a date-bounded request.  A real
export writes atomically with a companion manifest; verification compares the
file's actual bytes-as-JSON against the manifest checksum and declared
redactions.

When `encrypt_password` is supplied on a non-preview advanced export, Ares
uses a password-derived AES-256-GCM envelope.  The password is never persisted
or returned.  The encrypted envelope is an export artifact, while the normal
manifest continues to describe the redacted plaintext projection.

## Operational boundaries

The upgrade layer does not relax existing confirmation, permission, path,
network, or child-agent boundaries.  In particular:

- An ambiguous person/contact is never auto-selected for a consequential
  action.
- Phone actions still require the existing explicit confirmation path.
- MCP reserved metadata is separated from server arguments, and output,
  pagination, timeout, and cache behavior are bounded.
- Batch image work requires an explicit execution step after a preview.
- Export redaction remains on by default in the advanced path.  Encryption is
  an additional protection for a deliberately requested export, not a reason
  to bypass redaction.

## Focused regression suites

The upgrade work is covered by family-level suites rather than requiring an
unrelated full repository run during iterative development:

```powershell
python -m pytest -q tests/test_existing_tools_delegation_skills_integration.py tests/test_delegation_upgrades.py tests/test_mcp_upgrades.py tests/test_mcp_client.py
python -m pytest -q tests/test_existing_tools_media_export_integration.py tests/test_media_export_upgrades.py tests/test_image_edit.py tests/test_exporter.py tests/test_export_security.py
```

Run `git diff --check` before committing a change.  The matrix and delivery
gates are recorded in `docs/existing-tools-upgrade-matrix-2026-07-16.md`.
