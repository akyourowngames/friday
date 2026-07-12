---
name: browser-content-review
description: Read, extract, or summarize web-page content when the user explicitly asks to "summarize this page", "summarize the latest message", "extract the table", or "review this web content".
category: automation
version: 1.0.0
co-load-with:
  - browser-use
requires-primary:
  - browser-use
co-load-triggers:
  - summarize this page
  - summarize the latest message
  - extract the table
  - review this web content
  - summarize the page
  - read this web page
examples:
  - prompt: Open the dashboard and summarize the latest update.
  - prompt: Read this web page and extract the table into bullets.
test_commands:
  - python -m pytest tests/test_skills.py tests/test_prompts.py tests/test_mcp_client.py -q
---

# Browser Content Review

## Scope

Use alongside Browser Use when the user asks to read, extract, compare, or
summarize content already visible in a web page or web app. Do not use it for a
generic browser navigation request.

## Evidence-First Reading

1. Navigate and take a Playwright snapshot; use the current page text or a
   targeted read-back as the source for the summary.
2. Capture the requested facts, dates, counts, names, and current page state.
   Preserve uncertainty when text is incomplete or the page has not loaded.
3. Summarize only what was observed. Do not infer hidden messages, unread
   content, delivery status, or information outside the current view.
4. Keep the answer short and structured around the user's question. Quote only
   the minimum necessary text; redact private contact values.

## Boundaries

- Reading/summarizing does not authorize replying, liking, posting, sending, or
  changing the page. Those require clear user intent and their own verification.
- If the page changed or a target is stale, obtain one fresh snapshot before
  continuing rather than guessing from prior references.
