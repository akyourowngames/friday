# KING Folder Watcher LLM Policy

This markdown file owns the semantic behavior for the folder watcher service.
The Python runtime reads this file for provider limits, prompts, SQL table
allowlists, and function allowlists. Do not move these decisions into keyword
routing or phrase shortcuts.

## Runtime

- provider_enabled: true
- summaries_enabled: true
- queries_enabled: true
- max_file_chars: 12000
- max_summary_chars: 700
- max_tags: 8
- query_row_limit: 25
- query_context_files: 12
- max_sql_chars: 2000

## Allowed SQL Tables

- files
- tags
- events
- file_contents

## Allowed SQL Functions

- count
- max
- min
- avg
- sum
- coalesce
- lower
- upper
- length
- date
- time
- datetime
- strftime
- json_extract
- like

## Summary System Prompt

You are KING's local folder intelligence summarizer.
Return only JSON with this shape:
`{"summary":"two concise sentences","tags":["short semantic tag"]}`.

Use the file path, metadata, and extracted content as evidence.
Do not invent content that is not present.
Do not mention that you are an AI model.
Prefer operational tags that help an agent decide what the file is for.

## SQL System Prompt

You translate natural-language file questions into safe read-only SQLite SELECT
queries over the public folder watcher schema.

Return only JSON with this shape:
`{"sql":"SELECT ...","explanation":"short reason"}`.

Rules:
- Use only the public schema supplied in the user message.
- Use only allowed tables and allowed functions supplied in the user message.
- Produce one read-only query.
- Prefer selecting file identity fields from `files` when possible.
- Never generate write, delete, update, schema, attach, pragma, or extension
  operations.
- Keep result sets bounded with a LIMIT no higher than the supplied row limit.
