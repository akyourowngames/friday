# Ares Watcher Service Design

**Date:** 2026-07-12
**Status:** Draft
**Author:** Ares Design Session

---

## Executive Summary

Add a proactive monitoring system to Ares that watches websites, prices, and social media for changes, then notifies the owner through multiple channels (Telegram, Desktop, Email). The system runs as a background service with a web dashboard for management.

---

## Goals

1. **Monitor anything** - websites, prices, content, availability, social media
2. **Notify everywhere** - Telegram, Desktop push, Email
3. **Act smartly** - configurable AI response (notify only, auto-act, suggest actions)
4. **Manage easily** - Terminal, Telegram, and Web Dashboard controls
5. **Run reliably** - background service with failure handling

---

## Non-Goals (v1)

- DM pairing security (not needed for this version)
- Mobile companion apps
- Voice-controlled monitoring
- Multi-user support (single user only)

---

## Architecture

### Overview

```
┌─────────────────────────────────────────────────────────────────┐
│                    Ares Watcher Service                          │
├─────────────────────────────────────────────────────────────────┤
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐             │
│  │   Website   │  │  Instagram  │  │   Custom    │             │
│  │   Watcher   │  │   Watcher   │  │   Watcher   │  ...        │
│  └──────┬──────┘  └──────┬──────┘  └──────┬──────┘             │
│         │                │                │                     │
│         ▼                ▼                ▼                     │
│  ┌──────────────────────────────────────────────────────┐      │
│  │              Watcher Scheduler                        │      │
│  │  - Manages intervals per monitor                      │      │
│  │  - Runs fetchers on schedule                          │      │
│  │  - Handles failures and retries                       │      │
│  └──────────────────────────────────────────────────────┘      │
│         │                                                      │
│         ▼                                                      │
│  ┌──────────────────────────────────────────────────────┐      │
│  │              Change Detection Engine                   │      │
│  │  - Hash comparison (content hash)                     │      │
│  │  - Diff comparison (text changes)                     │      │
│  │  - Threshold comparison (price values)                │      │
│  └──────────────────────────────────────────────────────┘      │
│         │                                                      │
│         ▼                                                      │
│  ┌──────────────────────────────────────────────────────┐      │
│  │              Event Queue (SQLite)                     │      │
│  │  - Stores detected changes                            │      │
│  │  - Tracks notification status                         │      │
│  │  - Supports retry on failure                          │      │
│  └──────────────────────────────────────────────────────┘      │
│         │                                                      │
│         ▼                                                      │
│  ┌──────────────────────────────────────────────────────┐      │
│  │              Notification Dispatcher                   │      │
│  │  - Telegram bot message                               │      │
│  │  - Desktop push notification                          │      │
│  │  - Email via SMTP                                     │      │
│  │  - AI analysis (optional per monitor)                 │      │
│  └──────────────────────────────────────────────────────┘      │
└─────────────────────────────────────────────────────────────────┘
         │
         ▼
┌─────────────────────────────────────────────────────────────────┐
│                    Control Surfaces                              │
├─────────────────────────────────────────────────────────────────┤
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐             │
│  │  Terminal   │  │  Telegram   │  │   Web       │             │
│  │  Commands   │  │  Commands   │  │  Dashboard  │             │
│  └─────────────┘  └─────────────┘  └─────────────┘             │
└─────────────────────────────────────────────────────────────────┘
```

### Design Influences

- **OpenClaw** (383k stars): Gateway architecture, multi-channel inbox, skills system
- **Uptime Kuma** (89k stars): SQLite storage, WebSocket real-time, 20-second intervals
- **n8n**: Workflow automation, human-in-the-loop approvals

---

## Data Model

### SQLite Schema

```sql
-- Monitor configurations
CREATE TABLE monitors (
    id TEXT PRIMARY KEY,                          -- UUID
    name TEXT NOT NULL,                           -- "Amazon PS5 Price"
    type TEXT NOT NULL,                           -- 'website', 'instagram', 'custom'
    url TEXT,                                     -- Target URL (for website/custom)
    config JSON,                                 -- Type-specific config
    interval_seconds INTEGER DEFAULT 900,        -- Check interval (default 15min)
    ai_action TEXT DEFAULT 'notify',             -- 'notify', 'auto', 'suggest'
    ai_prompt TEXT,                              -- Custom AI instruction
    enabled BOOLEAN DEFAULT 1,
    last_checked_at TIMESTAMP,
    last_status TEXT,                            -- 'ok', 'error', 'timeout'
    error_count INTEGER DEFAULT 0,
    created_at TIMESTAMP,
    updated_at TIMESTAMP
);

-- Stored snapshots for change detection
CREATE TABLE snapshots (
    id TEXT PRIMARY KEY,
    monitor_id TEXT REFERENCES monitors(id) ON DELETE CASCADE,
    content_hash TEXT,                           -- SHA256 of content
    content TEXT,                                -- Raw content or extracted text
    price_value REAL,                            -- Extracted price (if applicable)
    metadata JSON,                              -- Extra data (title, image URL, etc.)
    created_at TIMESTAMP
);

-- Detected changes / events
CREATE TABLE events (
    id TEXT PRIMARY KEY,
    monitor_id TEXT REFERENCES monitors(id) ON DELETE CASCADE,
    event_type TEXT NOT NULL,                    -- 'price_change', 'content_change', 'new_message'
    old_value TEXT,                              -- Previous state
    new_value TEXT,                              -- Current state
    change_summary TEXT,                         -- Human-readable summary
    severity TEXT DEFAULT 'info',                -- 'info', 'warning', 'critical'
    notified BOOLEAN DEFAULT 0,
    ai_analyzed BOOLEAN DEFAULT 0,
    created_at TIMESTAMP
);

-- Notification log
CREATE TABLE notifications (
    id TEXT PRIMARY KEY,
    event_id TEXT REFERENCES events(id) ON DELETE CASCADE,
    channel TEXT NOT NULL,                       -- 'telegram', 'desktop', 'email'
    status TEXT DEFAULT 'pending',               -- 'pending', 'sent', 'failed'
    sent_at TIMESTAMP,
    error TEXT
);

-- Instagram-specific state
CREATE TABLE instagram_state (
    id TEXT PRIMARY KEY,
    monitor_id TEXT REFERENCES monitors(id) ON DELETE CASCADE,
    last_dm_id TEXT,                             -- Last processed DM ID
    last_mention_id TEXT,                        -- Last processed mention ID
    last_check_at TIMESTAMP
);

-- Indexes
CREATE INDEX idx_monitors_enabled ON monitors(enabled);
CREATE INDEX idx_snapshots_monitor ON snapshots(monitor_id, created_at);
CREATE INDEX idx_events_monitor ON events(monitor_id, created_at);
CREATE INDEX idx_events_notified ON events(notified, created_at);
```

---

## Monitor Types

### Website Monitor

Tracks changes on web pages.

```python
# Configuration
{
    "type": "website",
    "url": "https://amazon.com/dp/B08N5WRWNW",
    "extractors": [
        {"field": "price", "selector": "#priceblock_ourprice", "type": "price"},
        {"field": "availability", "selector": "#availability", "type": "text"}
    ],
    "change_detection": "threshold",  # or "hash", "diff"
    "thresholds": {
        "price": {"max_change_pct": 10, "alert_below": 499.99}
    },
    "headers": {"User-Agent": "Mozilla/5.0..."},
    "timeout": 30
}
```

**Fetcher:** Uses `httpx` for async HTTP, `BeautifulSoup4` for parsing.

### Instagram Monitor

Monitors Instagram activity (DMs, mentions, comments).

```python
# Configuration
{
    "type": "instagram",
    "target": "username",           # Target account/hashtag
    "monitor": "dm",               # 'dm', 'mentions', 'comments'
    "auto_reply": false,           # Or custom response template
    "keywords_filter": ["help", "order"]  # Only alert on these keywords
}
```

**Fetcher:** Uses Instagram Private API or Playwright for browser automation.

### Custom API Monitor

Monitors any REST API endpoint.

```python
# Configuration
{
    "type": "custom",
    "api_url": "https://api.example.com/status",
    "method": "GET",
    "headers": {"Authorization": "Bearer TOKEN"},
    "extractors": [
        {"field": "status", "json_path": "$.status", "type": "text"}
    ],
    "change_detection": "hash"
}
```

**Fetcher:** Uses `httpx` with JSON path extraction.

---

## Change Detection

### Detection Methods

| Method | Use Case | How It Works |
|--------|----------|--------------|
| `hash` | Content changes | SHA256 hash of extracted content |
| `diff` | Text changes | Line-by-line diff with context |
| `threshold` | Price/value changes | Percentage or absolute threshold |

### Threshold Configuration

```python
{
    "price": {
        "max_change_pct": 10,      # Alert if price changes > 10%
        "alert_below": 499.99,     # Alert if price drops below $499.99
        "alert_above": 999.99      # Alert if price goes above $999.99
    }
}
```

---

## Notification System

### Channels

| Channel | Library | Configuration |
|---------|---------|---------------|
| Telegram | `python-telegram-bot` | Uses existing Ares bot token |
| Desktop | `plyer` or `win10toast` | System notifications |
| Email | `aiosmtplib` | SMTP config in `config.json` |

### Notification Format

```
🔔 Ares Watcher Alert

Monitor: Amazon PS5 Price
Type: Price Change
Time: 2026-07-12 14:30:00

Previous: $549.99
Current: $499.99
Change: -9.1% (-$50.00)

[View in Dashboard] [Pause Monitor]
```

### AI Analysis (Optional)

When `ai_action` is `suggest` or `auto`, the system:

1. Sends change context to LLM
2. Gets analysis/recommendation
3. Includes in notification or takes action

---

## Scheduling

### Interval Management

- Each monitor has its own `interval_seconds`
- Scheduler runs a main loop every 10 seconds
- Checks each monitor's `last_updated_at + interval_seconds`
- Respects enabled/disabled state

### Retry Logic

```python
RETRY_STRATEGY = {
    "max_retries": 3,
    "backoff_multiplier": 2,      # 1min, 2min, 4min
    "reset_after_success": True
}
```

### Failure Handling

- Consecutive failures increment `error_count`
- After 5 failures, monitor is auto-paused
- Notification sent: "Monitor 'X' paused due to repeated failures"

---

## Control Surfaces

### Terminal Commands

```
/monitor add <name> <url> --interval 15m --action notify
/monitor list                     # List all monitors
/monitor status <id>             # Check last check time & status
/monitor pause <id>             # Temporarily stop checking
/monitor resume <id>            # Resume checking
/monitor remove <id>            # Delete monitor
/monitor events <id>           # Show recent changes
/monitor test <id>             # Run immediate check
```

### Telegram Commands

```
/monitors                        # List all monitors
/monitor_add <name> <url>       # Add new monitor
/monitor_pause <id>             # Pause monitor
/monitor_resume <id>            # Resume monitor
/alerts                          # Recent alerts/unread changes
/monitor_status <id>            # Get monitor status
```

### Web Dashboard

**Tech Stack:**
- Backend: FastAPI + WebSocket
- Frontend: React + TailwindCSS
- Real-time: Socket.IO for live updates

**Pages:**
1. **Overview** - Active/Paused/Alerts/Failed counts
2. **Monitors** - Table with status, last check, actions
3. **Alerts** - Recent changes with severity indicators
4. **Add Monitor** - Form to create new monitors
5. **Settings** - Notification preferences, API keys

**Real-time Updates:**
- WebSocket connection for live status
- Auto-refresh when monitors check
- Toast notifications for new alerts

---

## File Structure

```
ares/
├── watcher/
│   ├── __init__.py
│   ├── scheduler.py           # Main scheduler loop
│   ├── models.py              # SQLAlchemy models
│   ├── queue.py               # Event queue management
│   ├── notifier.py            # Multi-channel notifications
│   ├── detectors.py           # Change detection logic
│   ├── ai_analyzer.py         # Optional AI analysis
│   ├── commands.py            # Terminal command handlers
│   ├── fetchers/
│   │   ├── __init__.py
│   │   ├── base.py            # Abstract fetcher
│   │   ├── website.py         # Website fetcher
│   │   ├── instagram.py       # Instagram fetcher
│   │   └── custom.py          # Custom API fetcher
│   └── dashboard/
│       ├── __init__.py
│       ├── app.py             # FastAPI app
│       ├── websocket.py       # WebSocket handlers
│       ├── routes.py          # API routes
│       └── static/            # Frontend assets
│           ├── index.html
│           ├── app.js
│           └── style.css
```

---

## Configuration

### Watcher Config (`~/.ares/config.json` additions)

```json
{
    "watcher": {
        "enabled": true,
        "database_path": "~/.ares/data/watchers.db",
        "dashboard": {
            "enabled": true,
            "port": 8080,
            "host": "127.0.0.1"
        },
        "notifications": {
            "telegram": {
                "enabled": true,
                "chat_id": "YOUR_CHAT_ID"
            },
            "desktop": {
                "enabled": true
            },
            "email": {
                "enabled": false,
                "smtp_host": "smtp.gmail.com",
                "smtp_port": 587,
                "username": "",
                "password": "",
                "to_address": ""
            }
        },
        "defaults": {
            "interval_seconds": 900,
            "ai_action": "notify",
            "timeout": 30,
            "max_retries": 3
        }
    }
}
```

---

## Integration Points

### Existing Ares Systems

| System | Integration |
|--------|-------------|
| Telegram Bot | Reuse for notifications and commands |
| Cron Jobs | Optional: schedule monitor checks |
| LLM Connection | AI analysis for smart notifications |
| Skills System | Watcher as a built-in skill |
| Memory | Store notable alerts as facts |

### New Dependencies

```toml
[project.optional-dependencies]
watcher = [
    "httpx>=0.27.0",
    "beautifulsoup4>=4.12.0",
    "fastapi>=0.110.0",
    "uvicorn>=0.29.0",
    "websockets>=12.0",
    "aiosmtplib>=3.0.0",
    "plyer>=2.1.0",
    "instagrapi>=2.0.0"
]
```

---

## Implementation Phases

### Phase 1: Core Infrastructure (Week 1)
- [ ] SQLite schema and models
- [ ] Scheduler service
- [ ] Website fetcher
- [ ] Hash/diff change detection
- [ ] Basic Telegram notifications

### Phase 2: Monitor Types (Week 2)
- [ ] Instagram fetcher
- [ ] Custom API fetcher
- [ ] Threshold detection for prices
- [ ] Email notifications
- [ ] Desktop notifications

### Phase 3: Control Surfaces (Week 3)
- [ ] Terminal commands
- [ ] Telegram commands
- [ ] Web dashboard (basic)
- [ ] WebSocket real-time updates

### Phase 4: AI Features (Week 4)
- [ ] AI analysis integration
- [ ] Smart notification formatting
- [ ] Auto-action capabilities
- [ ] Dashboard enhancements

---

## Testing Strategy

### Unit Tests
- Change detection algorithms
- Notification formatting
- Model serialization

### Integration Tests
- Scheduler with mock fetchers
- Dashboard API endpoints
- Telegram command handlers

### E2E Tests
- Full monitoring cycle
- Dashboard UI interactions
- Multi-channel notifications

---

## Success Metrics

1. **Reliability**: 99% uptime for monitoring service
2. **Latency**: < 30 seconds from change detection to notification
3. **Accuracy**: < 1% false positive rate for change detection
4. **Coverage**: Support for 10+ monitor types in v1

---

## Open Questions

1. Should we use Playwright for Instagram or a dedicated library?
2. Dashboard: React + Tailwind or simpler vanilla JS?
3. Rate limiting for Instagram API calls?

---

## Appendix

### A. Comparison with Existing Solutions

| Feature | Ares Watcher | Uptime Kuma | OpenClaw |
|---------|--------------|-------------|----------|
| Price monitoring | ✅ | ❌ | ❌ |
| Instagram | ✅ | ❌ | ✅ (DMs) |
| AI analysis | ✅ | ❌ | ✅ |
| Web dashboard | ✅ | ✅ | ❌ |
| Terminal control | ✅ | ❌ | ✅ |
| Multi-channel | ✅ | ✅ | ✅ |

### B. Future Enhancements (v2)

- Mobile companion app
- Voice-controlled monitoring
- Machine learning for anomaly detection
- Multi-user support
- Browser extension for quick monitor creation
