# Skills and MCP Marketplace

Ares can discover community `SKILL.md` workflows and MCP server metadata from
the registries configured in `~/.ares/config.json`. Discovery is read-only;
installing a skill or adding an MCP server is always a separate, reviewable
action.

## Skills

```text
/skills search weather forecast
/skills info @publisher/weather --registry clawhub
/skills install @publisher/weather --registry clawhub
/skills list
/skills create daily-review "Prepare a verified daily engineering review"
/skills update weather
/skills remove weather
/skills login --registry clawhub
/skills publish daily-review --registry clawhub
```

ClawHub can have more than one publisher for the same short slug. Search results
therefore show an `@publisher/slug` reference; use that complete reference for
`info`, `install`, and `update` targets. `/skills install` accepts only a hosted ZIP supplied by a configured registry.
Before writing anything, Ares rejects path traversal, symbolic links,
executables, binary files, archive bombs, malformed `SKILL.md` frontmatter, and
files that are not plain instruction/data files. Installed marketplace skills
receive a local provenance record so `/skills update` can safely refresh only
their original registry source.

If a skill declares an MCP dependency, Ares reports it after installing the
skill. It never adds that MCP server automatically. You can review the proposed
server with `/mcp info NAME`, then explicitly approve `/mcp add NAME`.

Publishing is available for local user skills through ClawHub. `/skills login`
uses hidden terminal input and saves the token only in the local config. Tokens
are redacted from every Ares data export.

## MCP servers

```text
/mcp search memory
/mcp info io.example/memory --registry mcp-registry
/mcp add io.example/memory
/mcp list
/mcp test io.example/memory
/mcp refresh
/mcp remove memory
```

`/mcp add` shows the transport, command or URL, arguments, source registry,
and required environment-variable names before it changes the shared config.
It requires a `y`/`yes` response, or an explicit `--yes` for a non-interactive
run. Adding a standard I/O server means it can be started by Ares during the
next refresh, so inspect that plan carefully.

Only configured registries are queried. Ares converts only constrained metadata
from supported package types (`npm` through `npx`, PyPI through `uvx`, or HTTPS
remote MCP endpoints); it does not execute registry-provided shell commands,
follow arbitrary external download handoffs, or populate secret values from a
registry response.

## Registry configuration

Fresh Ares configurations include:

```json
{
  "skill_registries": [
    {"name": "clawhub", "api_base": "https://clawhub.ai/api/v1", "enabled": true, "priority": 10},
    {"name": "openclaw", "api_base": "https://api.openclaw.ai/v1", "enabled": true, "priority": 5}
  ],
  "mcp_registries": [
    {"name": "mcp-registry", "api_base": "https://registry.modelcontextprotocol.io", "enabled": true, "priority": 10},
    {"name": "smithery", "api_base": "https://api.smithery.ai", "enabled": true, "priority": 5}
  ]
}
```

Use `auth_token` only for a registry that needs it, and keep the config file
private. Smithery's Registry API currently requires a bearer token. Ares keeps
successful MCP discovery responses briefly in process so an intermittent public
registry timeout does not discard a just-seen result.
