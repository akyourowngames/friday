---
type: workflow
status: active
updated: 2026-05-28
---

# Add Memory

Use this when a new fact should compound into KING memory and be visible in
Obsidian Graph view.

## Steps

1. Decide whether the fact is stable enough to improve future responses.
2. If it should affect assistant recall, use `memory_remember` from
   [[Memory/Memory Tools]] and inspect the returned fields.
3. Create or update the most specific vault page.
4. Add links to the relevant entity, project, source, and workflow pages.
5. Add or update the [[index]] entry.
6. Append a dated entry to [[log]].
7. Run [[Workflows/Lint Memory Vault]] if more than one page changed.

## Reject

- Greetings, filler, temporary mood, or one-off chatter.
- Facts with unclear evidence.
- Duplicate claims already covered by a stronger page.
- Keyword lists meant to steer routing.

## Template

Use [[Templates/Durable Memory]] for a new page.
