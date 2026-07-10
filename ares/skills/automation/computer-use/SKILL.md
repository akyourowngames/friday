---
name: computer-use
description: Operate Windows desktop apps through the Windows MCP with reliable observe-act-verify control. Use when the user asks to open an app, inspect an active window, click UI controls, type text, navigate a desktop workflow, save through an app dialog, manage windows, or verify an on-screen result.
---

# Computer Use

## Preconditions

1. Confirm that `mcp__windows__Snapshot` is available. If it is not, explain that desktop control is disconnected and do not substitute shell automation unless the user asks for it.
2. Follow the user's scope exactly. A request to inspect is read-only; a request to prepare content does not authorize sending, submitting, deleting, purchasing, or changing system settings.
3. Keep the task in the foreground. Do not open unrelated apps or inspect unrelated windows.

## Operating Loop

1. Observe: call `mcp__windows__Snapshot` before the first interaction. Read the active window, focus state, interactive labels, and input-language state.
2. Plan the shortest visible path. Prefer an app action, named UI label, or keyboard shortcut over screen coordinates.
3. Act: make one meaningful UI action at a time. Do not launch repeated duplicate actions or run empty waits.
4. Verify: after an app launch, navigation, text entry, save, or other state change, call a fresh snapshot and confirm the expected state.
5. Continue only with new evidence. Snapshot labels are valid only for the UI state that produced them. Never reuse a label after a state-changing action.

## Tool Selection

- `Snapshot`: default observation tool. Use it to discover the active app, named controls, focused field, and visible result.
- `App`: launch, focus, move, resize, or close an app window.
- `Click`: use a fresh named label first. Use coordinates only when the UI tree does not expose the control, such as Notepad's editing surface.
- `Type`: type only after the target field is focused. Clear existing text only when the user asked to replace it.
- `Shortcut`: prefer standard shortcuts for stable actions such as `Ctrl+S`, `Ctrl+A`, `Ctrl+L`, and `Alt+F4`.
- `Wait` or `WaitFor`: use only for a known app-load or UI-appearance condition. Do not poll without a target condition.
- `Clipboard`: use for short text transfer or independent verification when the normal UI does not expose typed text.

## Verification Rules

- Verify every desktop mutation independently. Do not trust a success message alone.
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
