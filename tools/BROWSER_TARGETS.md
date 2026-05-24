# KING Browser Targets

This file is the editable target and extraction contract for the
`browser_extract` tool.

It is not a router, keyword table, or canned response source. The runtime uses
these entries only after the tool is selected and called.

## How To Add A Target

Create a target section, set the page URL, then define fields with labels or CSS
selectors.

```text
## my_profile
url: https://example.com/profile
login_url: https://example.com/login
storage_state: storage/browser_auth/my_profile.json
wait_until: domcontentloaded
field: followers | source: meta | label: followers
field: following | source: meta | label: following
field: posts | source: meta | label: posts
```

For direct pages, callers can pass `url` and `fields` to the tool without adding
a target here. For repeated pages such as social profiles, use a named target so
KING can load the same source consistently.

For login-gated pages, set `login_url` and optionally `storage_state`. Use
`browser_login_session` first; it opens a visible browser for manual login and
saves the storage state without returning credentials. Leave the browser open
for at least a few seconds after login so the session can be saved; closing the
window later is fine once a save has happened.

## Field Options

- `source`: `auto`, `selector`, `meta`, `text`, `title`, or `url`.
- `label`: text near the value in meta text or visible page text.
- `selector`: CSS selector for a direct DOM lookup. Use `||` between multiple
  selectors.
- `attribute`: optional attribute to read from matched selector nodes.

## Page Reading

- `browser_read_page` loads a URL and returns visible text plus DOM blocks.
- `read_mode` on `browser_extract`: `fields` (default), `text`, `dom`, or `full`.
- DOM limits and selectors live in `tools/BROWSER_DOM_POLICY.md`.

## Example Target

Replace this URL with your real profile URL before using it.

## instagram_profile
url: https://www.instagram.com/your_username/
login_url: https://www.instagram.com/accounts/login/
storage_state: storage/browser_auth/instagram_profile.json
wait_until: domcontentloaded
field: followers | source: meta | label: followers
field: following | source: meta | label: following
field: posts | source: meta | label: posts
