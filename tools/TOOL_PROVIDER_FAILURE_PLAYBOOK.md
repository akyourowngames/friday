# KING Provider Failure Playbook

This playbook defines how KING should handle tools that depend on external
providers, local players, browser launchers, downloaders, or generated media
backends.

It is a documentation control surface only. It does not add routing logic,
keyword matching, provider preference rules, or hidden fallbacks.

## Core Rule

KING reports the exact observed provider state. It does not convert a narrow
tool result into a broad claim about the world.

## State Vocabulary

- `success` - the tool returned the requested result for the attempted scope.
- `empty` - the attempted provider or local store returned no usable items.
- `partial` - the tool returned some useful evidence, but the result is
  incomplete, degraded, truncated, cached, or fallback-based.
- `blocked` - the request could not be attempted because a permission, scope,
  schema, credential, or safety gate stopped it.
- `timeout` - the attempt exceeded its configured time budget.
- `failed` - the attempt ran and returned an explicit error.
- `unavailable` - the provider, app, dependency, or runtime capability was not
  available in the attempted environment.
- `unknown_after_attempt` - a state-changing operation may have partially
  completed and must be verified before retry.

## Reporting Rules

- For `empty`, say what scope was checked and avoid saying the target does not
  exist globally.
- For `partial`, report which useful fields were returned and which evidence is
  missing.
- For `timeout`, include the bounded timeout if the tool result provides it and
  do not imply the provider is permanently down.
- For `failed`, include the safe error category and next bounded action, not a
  raw traceback or guessed remote cause.
- For `unavailable`, name the unavailable dependency or tool surface only when
  the result proves it.
- For `unknown_after_attempt`, verify target state before any retry or success
  claim.

## Provider-Specific Guidance

### Web Search And Page Fetch

- Distinguish provider failure, fallback use, empty results, HTTP failure, and
  unreadable page text.
- Report observed URL, status, title, result count, provider, fallback state,
  truncation state, and timeout state when available.
- Do not say the web has no result when only one provider or page was checked.

### Browser Extraction

- Distinguish browser engine load, HTTP fallback, target-config lookup, page
  load, selector extraction, meta extraction, text extraction, and empty field
  values.
- Report target, requested URL, final URL, engine used, degraded state, title,
  field count, matched count, and per-field evidence only when returned by the
  tool.
- If Playwright is unavailable and the tool falls back to HTTP, report the
  fallback as partial or degraded for dynamic sites instead of claiming the full
  page was browser-rendered.
- For manual login sessions, report only final URL, title, storage-state path,
  and saved-state status. Do not report credentials, form values, cookies, or
  private account data unless a later extraction tool returns explicit fields.
- Do not claim account-private social details unless the loaded page and
  configured fields returned those values for the attempted URL.

### Reddit And Hacker News

- Distinguish missing input, rate limit, no results, provider error, not found,
  and fallback search.
- Report subreddit, user, story id, query, sort, time range, and item count only
  when returned or directly requested.
- Do not treat one listing page as complete coverage of a topic.

### YouTube, Playlist, And Playback

- Distinguish search result selection, playlist mutation, playback launch, and
  actual playback evidence.
- If a page was opened but audio was not verified, say the page was opened, not
  that it is playing.
- If a playlist mutation times out or fails, inspect playlist state before
  retrying or claiming that tracks were added or removed.

### Image And Gallery

- Distinguish generation request, file save, gallery lookup, view/open action,
  and delete action.
- Report saved path, bytes, backend, fallback state, and open result only when
  the tool result provides them.
- If transport trust is limited, report the generated artifact without claiming
  secure transport.

### Files, Terminal, And Local Apps

- Distinguish command completion, app launch request, existing path open, file
  mutation, and verification after mutation.
- Never convert a command exit code into a broad statement about the whole
  machine.
- If a state-changing command may have partially completed, inspect the target
  before retry.

## Verification Requirements

- Provider tools need tests for input validation, empty results, provider
  failure, timeout or bounded retry, and fallback reporting when those surfaces
  exist.
- State-changing provider tools need an idempotency or target-state check.
- Markdown-only changes to this playbook require the default verification
  pipeline to return `ship`.
- Runtime upgrades must update `TOOL_EVIDENCE_LEDGER.md` and
  `TOOL_UPGRADE_SESSION.md` after verification.

## Claim Boundaries

- Provider summaries must be composed from returned fields, not copied from
  prewritten example sentences.
- Search answers may report provider name, result count, fallback state, and
  query only when those fields are returned.
- Fetch answers may report final URL, status, title, timeout, truncation, and
  readable-text state only when those fields are returned.
- Media and app answers must separate launch evidence from playback or state
  verification evidence.
- Image answers must separate generation request, saved file evidence, backend
  evidence, and transport trust evidence.
- Empty, failed, timeout, unavailable, and partial states must not be rewritten
  into broader absence, success, or permanent provider claims.

## Maintenance

- Update this playbook when a provider-backed tool gains a new structured state.
- Keep provider claims tied to runtime fields and verification evidence.
- Do not add canned user replies or phrase triggers here.
