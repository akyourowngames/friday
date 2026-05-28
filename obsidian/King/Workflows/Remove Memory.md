---
type: workflow
status: active
updated: 2026-05-28
---

# Remove Memory

Use this when the user asks KING to forget, delete, remove, or retire a memory.

## Steps

1. Confirm the target claim from [[index]], backlinks, or runtime recall.
2. If the claim exists in runtime memory, call `memory_forget` from
   [[Memory/Memory Tools]] and inspect the result.
3. Retire the vault claim by moving it to [[Removed/README|Removed Memory]] or
   marking the page inactive with a dated reason.
4. Remove active links that would keep the retired claim prominent in Graph view.
5. Update [[index]] and append [[log]].
6. Run [[Workflows/Lint Memory Vault]] to catch broken links.

## Proof Rule

Do not say runtime memory was removed unless the tool result proves it. If only
the vault changed, say it was a vault-only removal.
