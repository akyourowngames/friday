# Composio Gateway

This file is the markdown control surface for KING's Composio bridge. It keeps external app capabilities limited, auditable, and easy to expand without exposing every Composio toolkit directly to the model.

## Runtime

- enabled: true
- base_url: https://backend.composio.dev/api/v3.1
- api_key_env: COMPOSIO_API_KEY
- user_id_env: KING_COMPOSIO_USER_ID
- session_id_env: KING_COMPOSIO_SESSION_ID
- default_timeout_ms: 20000
- max_response_chars: 12000
- create_sessions_with_search: true
- create_sessions_with_manage_connections: true
- create_sessions_with_workbench: false

## Enabled Toolkits

- github

## Enabled Tools

- GITHUB_GET_A_REPOSITORY | toolkit: github | risk: read | enabled: true | note: read repository metadata after GitHub is connected
- GITHUB_LIST_REPOSITORY_ISSUES | toolkit: github | risk: read | enabled: true | note: read repository issues after GitHub is connected
- GITHUB_LIST_STARGAZERS | toolkit: github | risk: read | enabled: true | note: read repository stargazers after GitHub is connected

## Argument Defaults

- GITHUB_GET_A_REPOSITORY | owner: local.owner | repo: local.repo
- GITHUB_LIST_REPOSITORY_ISSUES | owner: local.owner | repo: local.repo
- GITHUB_LIST_STARGAZERS | owner: local.owner | repo: local.repo

## Risk Policy

- read: allow after the tool slug is listed under Enabled Tools.
- write: require confirm=true in the tool call.
- destructive: require confirm=true and keep disabled until explicitly added to Enabled Tools.
- auth: only generate Composio hosted auth links for toolkits listed under Enabled Toolkits.

## Usage Contract

- KING exposes one registry tool named `composio`; it must not expose the whole Composio catalog as direct callable schemas.
- Session creation must use only the markdown-enabled toolkits and enabled tool slugs.
- Direct execution must reject any tool slug that is not listed as enabled here.
- Provider errors, missing API keys, missing sessions, and auth requirements must return structured errors instead of false-negative capability claims.
- Argument defaults are resolved by the gateway from local repo evidence such as `git remote.origin.url`; they fill only missing values and must not override user-supplied arguments.
- Never store Composio API keys, OAuth tokens, connected account secrets, or private provider responses in this markdown file.

## Local Test Prompts

- `composio status`
- `create a composio session for github`
- `connect github through composio`
- `use composio to get repo details for owner akyourowngames repo friday`
- `use composio to list issues for owner akyourowngames repo friday`
