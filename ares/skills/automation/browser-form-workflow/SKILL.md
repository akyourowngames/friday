---
name: browser-form-workflow
description: Complete a web form only when the user explicitly asks to "fill a web form", "submit a web form", "complete this form", or "enter these fields".
category: automation
version: 1.0.0
co-load-with:
  - browser-use
requires-primary:
  - browser-use
co-load-triggers:
  - fill a web form
  - fill the web form
  - submit a web form
  - complete this form
  - enter these fields
examples:
  - prompt: Open the portal and fill the web form with these fields.
  - prompt: Complete this registration form, but stop before submitting.
test_commands:
  - python -m pytest tests/test_skills.py tests/test_prompts.py tests/test_mcp_client.py -q
---

# Browser Form Workflow

## Scope

Use alongside Browser Use for a specifically requested web form. Do not load it
for ordinary browsing, reading, search, or clicking a single control.

## Workflow

1. Navigate directly when the destination is known, then take one fresh
   Playwright snapshot.
2. Map only the requested values to fields visible in that snapshot. Do not
   infer missing personal data or fill optional fields without instruction.
3. Fill related fields together when the page supports it, then verify the
   visible values and validation errors once.
4. Treat submit/save/create as an external state change. Stop before it unless
   the user clearly asked to submit; when asked, verify the destination and
   final field values immediately before submitting.
5. After submit, wait for one known confirmation or take a fresh snapshot. If a
   reference becomes stale, follow Browser Use recovery and never retry it.

## Boundaries

- Never enter passwords, one-time codes, payment data, or secrets.
- Never submit duplicate forms or retry a submission without fresh evidence.
- Report validation failures precisely and ask for only the missing information.
