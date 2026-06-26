# Ares MCP Client Integration

## Overview

Connect Ares to external MCP servers (starting with Google Calendar and Gmail) by
implementing Ares as an MCP **client** using the Streamable HTTP transport. Ares
remains the agent; MCP servers provide additional tools it can call.

## Architecture

One new module `ares/tools/mcp_client.py` containing three components:

### MCPServerConfig
Pydantic model for one configured server:
- `name: str` — short identifier (e.g. "calendar", "gmail")
- `server_url: str` — the MCP endpoint URL
- `oauth_client_id: str` — Google Cloud OAuth client ID
- `oauth_client_secret: str` — Google Cloud OAuth client secret
- `oauth_scopes: list[str]` — required OAuth scopes

Storage: `AppConfig` in `models.py` gains an `mcp_servers: list[dict] = Field(default_factory=list)` field.

### MCPClientManager
Owns connections to all configured servers and exposes tools to the agent.

```
MCPClientManager
├── servers: dict[str, MCPServerConfig]
├── sessions: dict[str, ClientSession]        # live MCP sessions
├── http_clients: dict[str, httpx.AsyncClient] # per-server auth'd clients
├── auth_provider: MCPAuthProvider
│
├── async def start()                          # connect all servers
├── async def close()                          # disconnect all servers
├── tool_definitions: list[dict]               # cached after start(), consumed sync
├── async def call_tool(name, args) -> str     # routes to correct server
└── async def _connect_server(name)            # single-server connect
```

`start()` iterates servers and calls `_connect_server()` for each. After connecting,
it calls `session.list_tools()` on each and caches the resulting OpenAI-format
schemas in `self.tool_definitions`. Failures are logged per-server; one failing
server does not block others. `close()` tears down all sessions and HTTP clients.

Tool definitions are namespaced as `mcp__{server_name}__{tool_name}` (e.g.
`mcp__calendar__list_events`). The cached `tool_definitions` list is a plain
attribute so `Agent.__init__` can consume it synchronously after the manager
has started.

`call_tool(name, args)` parses the `mcp__{server}__{tool}` prefix, looks up the
correct session, and calls `session.call_tool(tool_name, arguments=args)`.

### MCPAuthProvider
Handles OAuth 2.1 PKCE flow for any Streamable HTTP server that requires auth.

```
MCPAuthProvider
├── token_dir: Path = ~/.ares/data/mcp_tokens/
│
├── async def ensure_token(config) -> str       # returns Bearer token
├── async def _discover_endpoints(config)       # /.well-known discovery
├── async def _run_pkce_flow(config) -> dict    # browser + callback
├── async def _refresh_token(config, token) -> dict
├── async def store_token(name, token_data)
└── async def load_token(name) -> dict | None
```

Flow:
1. `ensure_token()` checks `~/.ares/data/mcp_tokens/{name}.json` for a valid token.
2. If missing/expired: call `_discover_endpoints()` to find `/authorize` and `/token`
   via `/.well-known/oauth-authorization-server`, falling back to `{base_url}/authorize`
   and `{base_url}/token`.
3. Generate PKCE `code_verifier` + SHA256 `code_challenge` + opaque `state`.
4. Start a lightweight HTTP server on `127.0.0.1:9865` listening at `/callback`.
5. Open the user's browser to the authorization URL with `code_challenge` and `state`.
6. Receive the auth code on the callback, exchange it for tokens at the token endpoint.
7. Store access + refresh tokens, return the access token.
8. On 401 from a session, attempt refresh; if refresh fails, prompt re-auth.

For Google Calendar/Gmail specifically, Google doesn't use MCP's metadata discovery.
Ares skips discovery and uses the standard Google OAuth endpoints:
- Authorize: `https://accounts.google.com/o/oauth2/v2/auth`
- Token: `https://oauth2.googleapis.com/token`
- The redirect URI is `http://127.0.0.1:9865/callback`, registered in Google Cloud Console.

## Integration Points

### AppConfig (models.py)
```python
class AppConfig(BaseModel):
    # ... existing fields ...
    mcp_servers: list[dict] = Field(
        default_factory=list,
        description="MCP server configurations: [{name, server_url, oauth_client_id, oauth_client_secret, oauth_scopes}]"
    )
```

### Agent (agent.py)
- `Agent.__init__` accepts optional `mcp_manager: MCPClientManager | None`.
- On init, builds `self.tools` as `get_tool_definitions() + mcp_manager.tool_definitions`.
- `process_tool_calls()` checks `tool_name.startswith("mcp__")` before the dispatch dict:
  - Parse server name and tool name from the prefixed string.
  - Call `mcp_manager.call_tool(server, tool, args)`.
  - Return the result string.
- No changes to `run()` or `run_stream()` — they already iterate `self.tools` and route through `process_tool_calls`.

### CLI (cli.py)
- `AresCLI.__init__`: after loading config, instantiate `MCPClientManager(config.mcp_servers)`,
  call `await manager.start()`, pass to `Agent(mcp_manager=manager)`.
- `AresCLI.run()` finally block: call `await manager.close()` alongside existing cleanup.

### Server (server.py)
- `AresServer.__init__`: same pattern — create manager, start it, pass to agent.
- `AresServer.close()`: call `await manager.close()`.

## OAuth Token Storage

Tokens stored as JSON in `~/.ares/data/mcp_tokens/{server_name}.json`:

```json
{
  "access_token": "...",
  "refresh_token": "...",
  "expires_at": "2026-06-27T12:00:00Z",
  "scopes": ["https://www.googleapis.com/auth/calendar.events.readonly"]
}
```

Same data directory pattern as the rest of Ares. Token directory is created on
startup if it doesn't exist.

## Error Handling

- **Server connection failure**: logged, that server's tools are absent, Ares continues.
- **Expired token**: automatic refresh attempt; if refresh fails, user is prompted to
  re-authorize via browser. Ares continues without that server's tools until re-auth.
- **MCP SDK errors**: caught, returned as error strings into the tool result, surfaced
  to the LLM. The LLM can decide to retry or report to the user.
- **No MCP servers configured**: manager is `None`, no tools added, no behavior change.

## Dependencies

Add to `pyproject.toml`:
- `mcp>=1.28.0` — the official Python SDK (provides `ClientSession`, `streamable_http_client`)
- `httpx` — already a dependency, used for OAuth token exchange + auth'd transport

## Files Changed

| File | Change |
|---|---|
| `ares/models.py` | Add `mcp_servers: list[dict]` to `AppConfig` |
| `ares/tools/mcp_client.py` | New file — `MCPClientManager`, `MCPAuthProvider`, `MCPServerConfig` |
| `ares/tools/__init__.py` | No change needed (agent imports directly) |
| `ares/agent.py` | Accept `mcp_manager`, build tools list including MCP tools, route `mcp__` calls |
| `ares/cli.py` | Init/start/close manager in lifecycle |
| `ares/server.py` | Init/start/close manager in lifecycle |
| `pyproject.toml` | Add `mcp>=1.28.0` dependency |

## Future Considerations (not in scope)

- **Dynamic server add/remove** at runtime via slash commands (`/mcp add`, `/mcp remove`).
- **Ares as an MCP server** exposing its own tools (memory, tasks) to other AI tools.
- **Stdio transport support** for open-source MCP servers that don't support HTTP.
- **Tool-level access control** to restrict which MCP tools Ares can call.
