---
name: computer-use
description: Operate native Windows desktop apps through the Windows MCP with reliable observe-act-verify control. Use when the user asks to open or inspect Notepad, File Explorer, Settings, an installer, OBS, a desktop dialog, or another native app window.
---

# Computer Use

## Scope: Native Windows Apps Only

Use this skill for native laptop/desktop apps, OS dialogs, system UI, and an
explicitly requested visible Windows/Chrome window. Do not use this skill for
normal browser/web automation. Browser tasks should use the `browser-use` skill
and Playwright MCP because it is faster and DOM/accessibility-aware. Only fall
back to Windows MCP for browser tasks when Playwright MCP is disconnected or
cannot access the target.

## Preconditions

1. Confirm that `mcp__windows__Snapshot` is available. If it is not, explain that desktop control is disconnected and do not substitute shell automation unless the user asks for it.
2. Follow the user's scope exactly. A request to inspect is read-only; a request to prepare content does not authorize sending, submitting, deleting, purchasing, or changing system settings.
3. Keep the task in the foreground. Do not open unrelated apps or inspect unrelated windows.

## Operating Loop

1. Observe: call `mcp__windows__Snapshot` before the first interaction. Read the active window, focus state, interactive labels, and input-language state.
2. Plan the shortest visible path. Prefer an app action, named UI label, or keyboard shortcut over screen coordinates.
3. Act: make one meaningful UI action at a time. Do not launch repeated duplicate actions or run empty waits.
4. Verify: after an app launch, navigation, save, submit, or other meaningful
   state change, call a fresh snapshot and confirm the expected state. A full
   deterministic text entry into an already focused field does not need a
   separate snapshot before the next known shortcut.
5. Continue only with new evidence. Snapshot labels are valid only for the UI state that produced them. Never reuse a label after a state-changing action.

## Tool Selection

- `Snapshot`: default observation tool. Use it to discover the active app, named controls, focused field, and visible result.
- `App`: launch, focus, move, resize, or close an app window.
- `Click`: use a fresh named label first. Use coordinates only when the UI tree does not expose the control, such as Notepad's editing surface.
- `Type`: type the complete requested text in one call after the target field is focused. Clear existing text only when the user asked to replace it.
- `MultiEdit` and `MultiSelect`: batch related fields or selections that all
  come from the same fresh snapshot. Use one post-batch verification instead
  of one MCP round trip per field or item.
- `Shortcut`: prefer standard shortcuts for stable actions such as `Ctrl+S`, `Ctrl+A`, `Ctrl+L`, and `Alt+F4`; combine a known focus → type → save sequence without redundant observations.
- `Wait` or `WaitFor`: use only for a known app-load or UI-appearance condition. Do not poll without a target condition.
- `Clipboard`: use for short text transfer or independent verification when the normal UI does not expose typed text.

## Verification Rules

- Verify every meaningful desktop mutation independently. Do not trust a success message alone, but do not repeat a snapshot after a no-op focus change or each piece of a known text entry.
- For app launch or navigation, confirm the expected window and active state in a new snapshot.
- For text entry, confirm the target UI state. When exact text matters, select/copy it and inspect the clipboard.
- For a save request, first confirm the app's saved state, then use Ares's read-only file tools to confirm the requested path exists when the path is known.
- Report only what the latest snapshot or file check proves. If verification cannot be completed, say what remains unverified.

## Recovery

1. When an action fails, inspect a new snapshot before retrying.
2. Retry at most twice, changing the approach each time: refresh label, focus the app, use a stable shortcut, then use coordinates only as a final fallback.
3. Stop and state the blocker when the target remains unavailable, a login or permission prompt appears, or the next step needs a consequential decision from the user.

## Completion

Finish with a concise result: completed actions, verification evidence, and any item that could not be verified. Do not mention this skill unless the user asks.
