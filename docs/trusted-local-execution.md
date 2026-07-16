# Trusted local execution

`trusted_local` is the opt-in multi-agent execution profile for an Ares owner
who explicitly asks for broad local tool and MCP access.

Use it on `delegate_task` or `delegate_tasks_parallel`:

```json
{
  "agent": "builder",
  "task": "Inspect the connected MCPs and implement the requested local change",
  "execution_profile": "trusted_local"
}
```

The profile is only accepted when the current owner message unambiguously asks
for it (for example, “enable trusted local execution” or “remove restrictions
from Ares tools and MCPs”). Tool-call JSON cannot authorize itself.

## What expands

- Assigned specialists can see every registered local tool and every connected
  MCP schema, including tools outside their normal role template.
- They receive the full non-delegation capability set, so known local work and
  configured MCP integrations do not fail merely because a role was created as
  a researcher, planner, or analyst.
- A configured MCP reconnects once before its first call if its live session is
  absent.
- MCP calls can use `__ares.timeout_seconds` up to the server’s finite
  `max_timeout_seconds` (default: 600 seconds). Windows screenshots still use
  a 15-second default unless a higher per-call timeout is explicitly requested.

## Boundaries that stay in force

- The dedicated reviewer remains read-only, preserving independent patch
  review.
- Child actions that affect browsers, external services, communication, shell
  execution, or unknown/mutating MCP tools still need a root-issued exact
  single-use action grant.
- A child cannot manufacture user confirmation, leave its assigned workspace,
  bypass secret redaction, or use an unconfigured MCP server.
- Trusted-local runs are intentionally not auto-resumed after interruption:
  submit a fresh assignment instead of replaying a potentially consequential
  operation.

To preview the broadened specialist view without launching work, call
`list_agents` with `{"execution_profile": "trusted_local"}`.
