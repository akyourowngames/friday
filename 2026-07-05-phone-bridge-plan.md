> **Status:** Shipped

# Phone Bridge Plan — Notifications, Contacts, SMS, Calls

**Date:** 2026-07-05
**Status:** Shipped
**Goal:** Give Ares live access to an Android phone — read notifications, look up contacts, read/send SMS, and place phone calls — using only free tools (no paid Tasker, no custom app).

---

## Architecture Summary

Two independent bridges, each wrapped the same way `google_mcp_bridge.py` wraps Gmail/Calendar — thin Python modules that shell out to an external tool and return structured JSON, then get exposed as normal Ares tools via `tools/definitions.py` + `tools/executor.py`.

```
Phone (Android)
  ├── KDE Connect app  ──Wi-Fi──►  KDE Connect desktop daemon ──► kdeconnect_bridge.py ──► Ares tools
  │        (notifications, contacts, SMS)
  └── Wireless debugging (adb) ──Wi-Fi──► adb binary ──► adb_bridge.py ──► Ares tools
           (call placement)
```

Neither bridge needs Ares to run anything on the phone beyond apps/settings that already exist for free. Both are LAN-only — nothing leaves your network.

---

## Prerequisites (no code, do these first)

1. **Install KDE Connect** on the phone (Play Store, free) and the matching desktop component for your OS (native on Linux; official port exists for Windows; macOS has a community build — check current status since it lags the other platforms).
2. **Pair** phone and desktop — both on the same Wi-Fi, accept the pairing prompt on both ends once.
3. On the phone, grant KDE Connect the notification-access permission and contacts permission when it asks (it will prompt on first use of those features).
4. **Enable Developer Options** on the phone (tap Build Number 7 times in Settings → About Phone), then enable **Wireless debugging** under Developer Options.
5. From a terminal on the same machine Ares runs on, install Android platform-tools (`adb`) if not already present, then pair once using the code Android shows you (`adb pair <ip>:<port>` then the 6-digit code), and connect (`adb connect <ip>:<port>`).
6. Confirm both channels work manually before touching any code: `kdeconnect-cli -l` should list your phone; `adb devices` should list it as a connected device.

Everything past this point is Ares-side wiring.

---

## File Structure

| File | Action | Responsibility |
|------|--------|-----------------|
| `ares/tools/kdeconnect_bridge.py` | Create | Wraps `kdeconnect-cli` (or D-Bus directly) for notifications, contacts, SMS |
| `ares/tools/adb_bridge.py` | Create | Wraps `adb shell` calls for placing phone calls and basic device status |
| `ares/tools/definitions.py` | Modify | Add tool schemas: `phone_get_notifications`, `phone_search_contact`, `phone_send_sms`, `phone_call_number`, `phone_status` |
| `ares/tools/executor.py` | Modify | Register handlers routing those tool names to the two bridge modules |
| `ares/models.py` | Modify | Add a `PhoneConfig` block to `AppConfig`: enabled flags, KDE Connect device ID, ADB device address, notification-retention policy |
| `ares/config.py` | Modify | Nothing structural needed beyond picking up the new config block automatically via Pydantic defaults |
| `ares/prompts.py` | Modify | Add a "Phone" section to `SYSTEM_PROMPT` describing the new tools and, critically, the confirmation rule for calls |
| `ares/memory_extractor.py` | Modify (guardrail) | Exclude phone-notification content from automatic fact extraction |
| `ares/cli.py` | Modify | Add a `/phone status` command to check pairing health without invoking the LLM |

---

## Task Breakdown

### Task 1 — `kdeconnect_bridge.py` (notifications, contacts, SMS)

Functions to implement (signatures/behavior only):

- `get_device_id() -> str` — resolve the paired phone's KDE Connect device ID once (from config if set, otherwise auto-detect via `kdeconnect-cli -l` and cache it).
- `get_recent_notifications(limit: int = 20) -> str` — pull the current notification list for the device, return as JSON: package name, app name, title, text, timestamp. KDE Connect's CLI surfaces active notifications; treat this as a snapshot, not a stream — Ares polls it when asked, it doesn't sit and watch.
- `search_contacts(query: str) -> str` — query the synced contact list for a name/number match, return JSON with name + numbers.
- `send_sms(number: str, message: str) -> str` — send a text through the phone via KDE Connect's SMS plugin.
- `get_recent_sms(limit: int = 10) -> str` — read recent SMS conversation content if you want Ares to summarize texts, not just send them.

Error handling: every function should degrade gracefully with a clear "device not paired" or "KDE Connect not running" message rather than throwing, matching the pattern used throughout `google_mcp_bridge.py`.

### Task 2 — `adb_bridge.py` (calls + status)

- `is_device_connected() -> bool` — check `adb devices` output for a connected/authorized entry.
- `call_number(number: str) -> str` — issue the dialer intent (`am start -a android.intent.action.CALL`) via `adb shell`. Note this requires the phone to have a SIM/dialer capable of placing calls and will need a manual tap to confirm on-screen unless you later grant an accessibility service to auto-confirm (not recommended initially — the manual tap is a good safety check).
- `get_battery_status() -> str` — nice-to-have, cheap to add (`adb shell dumpsys battery`), useful for "check my phone" queries.

Keep this module deliberately small — ADB's job here is just "place the call," everything else stays on the KDE Connect side.

### Task 3 — Tool definitions (`tools/definitions.py`)

Add five tool schemas following the existing `_tool(...)` helper pattern already used for every other tool in that file:

- `phone_status` — no args, returns pairing/connection health for both bridges.
- `phone_get_notifications` — optional `limit` int.
- `phone_search_contact` — required `query` string.
- `phone_send_sms` — required `number`, `message` strings.
- `phone_call_number` — required `number` string. Description should explicitly state this places a real phone call and needs the user's clear go-ahead in the conversation before being invoked (mirrors how `delete_file` requires `confirm=true` — consider adding a `confirm: bool` parameter to this tool specifically, defaulting to `False`, so the agent can't dial without an explicit second signal).

### Task 4 — Executor wiring (`tools/executor.py`)

Add the five handler methods to the `handlers` dict in `ToolExecutor.execute`, each a thin pass-through to the corresponding bridge function — identical shape to how `_web_search`/`_fetch_url` wrap `ares/tools/web.py` today.

### Task 5 — Config (`models.py`)

Add a `PhoneConfig` model alongside `VoiceConfig`:
- `enabled: bool = False`
- `kdeconnect_device_id: str = ""` (empty = auto-detect)
- `adb_device_address: str = ""` (empty = use whatever `adb devices` reports)
- `store_notification_content: bool = False` — governs Task 6 below; default off.

Add `phone: PhoneConfig = Field(default_factory=PhoneConfig)` to `AppConfig`.

### Task 6 — Privacy guardrail (`memory_extractor.py`)

This is the one non-optional step. Notification text can include 2FA codes, private DMs, banking alerts — content you do not want silently summarized into `facts_meta` by the automatic extractor. Before wiring notifications into any conversation Ares has, add an explicit check in `MemoryExtractor.extract_and_store` (or wherever conversation text gets assembled) that strips or skips any message whose content originated from a `phone_get_notifications`/`phone_get_recent_sms` tool result, unless `config.phone.store_notification_content` is explicitly set to `True`. Treat this the same way `tool_truncator.py` already treats tool output — as data that's useful in the moment but shouldn't automatically become permanent memory.

### Task 7 — Prompt update (`prompts.py`)

Add a short "Phone" section to `SYSTEM_PROMPT`, modeled on the existing "Command Execution" section's tone: describe the five tools, then state plainly that `phone_call_number` must never be called unless the user has just explicitly asked for that specific call in that message — no inferring "you should probably call them" and dialing on your own initiative.

### Task 8 — CLI check command (`cli.py`)

Add a `/phone` command (no LLM round-trip) that calls `phone_status` directly and prints a simple pass/fail table for "KDE Connect paired" and "ADB connected" — useful for debugging pairing issues without burning a model call every time you're troubleshooting Wi-Fi reconnects.

### Task 9 — Testing checklist

- [ ] `kdeconnect-cli -l` and `adb devices` both show the phone before testing any Ares tool.
- [ ] `phone_status` reports both bridges healthy.
- [ ] `phone_get_notifications` returns real, current notifications.
- [ ] `phone_search_contact` finds a known contact.
- [ ] `phone_send_sms` delivers a test text to a second phone/number you control.
- [ ] `phone_call_number` with `confirm=false` refuses; with `confirm=true` actually dials and requires the on-screen tap.
- [ ] Ask Ares something unrelated afterward and confirm notification text does NOT show up in `/memory search` results (validates Task 6).
- [ ] Reboot the phone, confirm ADB wireless pairing breaks as expected, and re-pairing restores `phone_call_number`.

---

## Known Limitations (be aware, not blockers)

- **Wireless ADB isn't permanent.** A phone reboot or switching Wi-Fi networks usually invalidates the pairing; you'll re-pair periodically. This is the tradeoff for zero cost / zero cable.
- **Calls need a manual tap.** `adb`'s dialer intent opens the call, it doesn't blind-dial without the user seeing it happen on the phone screen — treat this as a safety feature, not a bug to route around.
- **Notifications are a snapshot, not a push stream**, unless you later extend the KDE Connect bridge to listen on D-Bus signals continuously (bigger lift — v2 idea, not in this plan).
- **macOS KDE Connect support lags Linux/Windows** — check current build status if that's your desktop OS before committing to this path.

---

## Self-Review

- Covers all four asked-for capabilities: notifications ✅, contacts ✅, SMS as a bonus ✅, calls ✅.
- No paid tools required anywhere in the chain.
- Follows existing Ares conventions: bridge module pattern from `google_mcp_bridge.py`, tool registration pattern from `definitions.py`/`executor.py`, config pattern from `VoiceConfig`.
- Privacy guardrail (Task 6) is treated as mandatory, matching the project's stated "privacy first, local data stays local" principle — this is the one part of the plan that shouldn't be skipped even to move faster.
