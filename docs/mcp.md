# MCP Configuration

Ares connects Model Context Protocol servers from the `mcp_servers` array in
`~/.ares/config.json`. Connected tools are exposed to the agent as
`mcp__server__tool` and refresh when a server reconnects.

## CLI Management

Use the terminal controls to inspect and recover integrations:

| Command | Purpose |
|---|---|
| `/mcp status` | Show readiness, transport, target, tool count, timeout, and any safe error text. |
| `/mcp tools [SERVER]` | List discovered tools grouped by server, or only one server. |
| `/mcp reconnect SERVER` | Restart one server and refresh its available tools. |
| `/mcp health` | Probe every connected server and refresh readiness. |
| `/mcp reload` | Rebuild all MCP connections from the current shared config. |
| `/mcp config` | Show safe configuration metadata; arguments, environment values, and OAuth settings remain hidden. |

## Server Examples

Add entries to the `mcp_servers` array in `~/.ares/config.json`.

### Playwright over stdio

```json
{
  "name": "playwright",
  "transport": "stdio",
  "command": "npx",
  "args": ["@playwright/mcp@latest"]
}
```

### Filesystem over stdio

```json
{
  "name": "filesystem",
  "transport": "stdio",
  "command": "npx",
  "args": ["@modelcontextprotocol/server-filesystem", "C:/Users/YOUR_NAME"]
}
```

### Streamable HTTP

```json
{
  "name": "some_server",
  "transport": "streamable_http",
  "server_url": "http://localhost:3000/mcp"
}
```

Use `transport: "sse"` for servers that only support legacy Server-Sent Events.
For local command-based servers, Ares automatically treats a configured command
as `stdio` when no HTTP endpoint is supplied.

## Security

Keep credentials in the server's `env` object or OAuth configuration; do not put
them in source control. Ares stores OAuth tokens in `~/.ares/data/mcp_tokens/`.
The `/mcp config` and `/mcp status` displays redact token-like values and never
print environment values. Use `/mcp reconnect SERVER` after updating an MCP
server's configuration.
