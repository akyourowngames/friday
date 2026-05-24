# Memory Auto Relations

When a memory fact mentions multiple graph entities, KING links them automatically.

## Settings

- relation: associated_with
- mode: multi
- tier: semantic
- min_entity_name_length: 2

## Behavior

- Run after each successful graph ingest for a memory entry.
- Match entity nodes whose names appear in the fact text (token overlap, not keyword routing).
- Create or strengthen `associated_with` edges between co-mentioned entities.
- Attach the originating `memory_id` on every auto edge.
