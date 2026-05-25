# KING Agent Instructions

- Dont use regex.
- Dont hardcode things.
- Dont add keyword routing or phrase-match shortcuts.
- We are building agi not chatbot.
- Always try not to hardcode.
- For tool behavior, keep markdown control surfaces updated and use code only when the user explicitly grants authority.
- Current authority note: the user explicitly granted code-edit authority for browser automation tooling on 2026-05-21. Code changes are allowed for this scope, but they must stay config-driven, verified, and backward compatible.
- Jaarvis should do what it has, and should not claim false negatives.
- Do not change `agent/core.py` or core routing/execution to fix tool exposure unless the user explicitly grants that scope in the current task.
- Tool exposure fixes must start from registry schemas and markdown control surfaces: `tools/TOOL_MANIFEST.md`, `tool_policy.md`, `routing_policy.md`, and tool-specific catalogs.
- If an active registry tool can perform the requested action, Jaarvis must use that tool or state the exact missing registry/schema evidence. It must not claim incapability when the capability is exposed.
- For local system actions, answer only from structured tool results. A sent key, media key, or hardware key is not a verified state change unless the returned fields prove the state changed.
- Do not solve tool exposure with keyword tables, phrase-match rescue logic, canned tool responses, or hardcoded success/failure text.
