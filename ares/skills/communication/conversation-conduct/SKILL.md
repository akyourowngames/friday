---
name: conversation-conduct
description: Compose or reply to a chat, message, or email when the user explicitly asks to "draft a reply", "write a message", "respond to a chat", or "compose an email".
category: communication
version: 1.0.0
co-load-with:
  - browser-use
co-load-triggers:
  - draft a reply
  - write a message
  - respond to a chat
  - compose an email
  - send a message
  - reply to
examples:
  - prompt: Draft a concise reply to this message.
  - prompt: Open the web chat and write a reply, but do not send it.
test_commands:
  - python -m pytest tests/test_skills.py tests/test_prompts.py -q
---

# Conversation Conduct

## Scope

Use this skill only when the user explicitly asks to draft, rewrite, reply to,
or send a communication. It applies to ordinary chat, email, and browser-based
messaging. It is not a general-purpose skill for every user turn.

## Response Quality

1. Follow the current user instruction exactly; do not revive old requests,
   imitate prior emotional context, or add unsolicited topics.
2. Answer directly and concisely. Avoid repeated greetings, filler, pressure,
   guilt, threats, spammy follow-ups, or multiple versions unless requested.
3. Preserve the user's intended tone. If it is unclear, make a neutral,
   respectful draft and label it as a draft.
4. Do not invent facts, relationships, delivery status, or another person's
   thoughts. Treat tool output as evidence and distinguish sent from delivered.
5. Never reveal private contact values or copy private message content into
   durable memory. Use saved aliases through the normal action path.

## External Actions

- Drafting and editing text are reversible. Sending, posting, or submitting is
  external: do it only when the user clearly requests that exact action.
- Before a send/post, verify recipient, destination, and the final visible text.
- Send once. Do not retry, send duplicates, or add follow-up messages unless the
  user asks after seeing the result.
- After success, state only the verified outcome (for example, "sent"); never
  claim a recipient read or received it without evidence.
