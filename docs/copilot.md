# GitHub Copilot provider

Ares supports GitHub Copilot through GitHub's official `github-copilot-sdk`.
It is not GitHub Models and it does not use an undocumented Copilot Chat
endpoint. The SDK runs its bundled Copilot runtime locally and Ares supplies an
explicit OAuth token, never falling back to a token from GitHub CLI or a prior
Copilot login.

Install the optional dependency:

```powershell
pip install -e .[copilot]
```

Create a GitHub OAuth App and enable **Device Flow** in its settings. Then run:

```powershell
ares
# Inside Ares:
/copilot login YOUR_GITHUB_OAUTH_CLIENT_ID
```

The CLI prints a GitHub verification URL and a short one-time code, opens the
URL when possible, and waits for your approval before it saves the access token
locally. A client secret and localhost callback are not needed for this flow.
Token fields are redacted from all exports and `/copilot status` never displays
one. An existing OAuth or fine-grained personal token can instead be connected
with `/copilot token TOKEN`.

Copilot Free uses the `auto` model: GitHub selects the available model and
enforces the plan's request limits. Models such as GPT-5 mini or GPT-4o may be
used by automatic selection, but GitHub does not expose a dependable manual
model picker for Free accounts. After connection, Ares switches to the
`copilot` provider automatically. If the plan's quota is exhausted, Ares shows
the GitHub quota message immediately; wait for it to reset or switch to another
configured provider with `/provider opencode` or `/provider nim`.
