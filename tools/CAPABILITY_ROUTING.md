# Capability Routing

This markdown is the control surface for capability-level tool routing.

Problem it solves: some registry tools are gateways that expose many distinct
capabilities behind one callable (for example `composio` reaches Gmail, Google
Calendar, Drive, Docs, Sheets, Tasks, Meet, Slides, Notion, Slack, and GitHub).
A single tool embedding cannot represent all of those capabilities, so natural
requests like "what's on my calendar tomorrow" never reach the gateway.

How it works: the router builds one routing probe per capability and embeds each
phrase. When a user query is closer to a capability phrase than to any direct
tool (above a dedicated capability threshold), the router selects the backing
tool and pre-fills any resolved argument hints (such as the exact Composio
tool_slug). This is pure semantic similarity (embeddings), not a keyword table or
phrase-match shortcut.

Two rule sources are merged in `tools/capabilities.py`:

1. Provider rules auto-generated from each multi-capability tool's own config.
   For Composio, every enabled tool in `tools/COMPOSIO_GATEWAY.md` becomes a
   capability phrase (from its note) that carries the exact `tool_slug`. Adding a
   Composio slug to the gateway automatically gives it a routing probe and a
   resolved slug, with no hand-maintenance here.
2. Static overrides in the `## Capabilities` section below. Use these only to add
   a phrasing the auto-generated notes do not cover, or to route a capability to
   a different backing tool. Format: `- phrase => backing_tool`.

Because provider rules already cover the enabled Composio tools (and carry the
resolved slug, which static rules do not), keep this static list minimal. Prefer
improving a tool's note in its own config over adding a static phrase here.

## Capabilities

- what's on my calendar today or tomorrow => composio | tool_slug: GOOGLECALENDAR_EVENTS_LIST
- show my upcoming calendar events => composio | tool_slug: GOOGLECALENDAR_EVENTS_LIST
- check my schedule for the week => composio | tool_slug: GOOGLECALENDAR_EVENTS_LIST
- check my latest emails or inbox => composio | tool_slug: GMAIL_FETCH_EMAILS
- search my email for a message => composio | tool_slug: GMAIL_FETCH_EMAILS
- find my recent google drive files => composio | tool_slug: GOOGLEDRIVE_FIND_FILE
- list my google tasks => composio | tool_slug: GOOGLETASKS_LIST_ALL_TASKS
