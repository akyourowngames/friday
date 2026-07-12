# Watcher Core Infrastructure Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the foundational SQLite schema, models, scheduler, and change detection engine for the Ares Watcher Service.

**Architecture:** A background service with SQLite storage that runs monitor checks on configurable intervals. Each monitor type has a fetcher that retrieves data, and a detector that compares against stored snapshots to find changes.

**Tech Stack:** Python 3.11+, SQLite, SQLAlchemy (async), httpx, hashlib

---

## File Structure

```
ares/watcher/
├── __init__.py              # Package exports
├── models.py                # SQLAlchemy models for all tables
├── database.py              # Database connection and initialization
├── scheduler.py             # Main scheduler loop
├── detectors.py             # Change detection (hash, diff, threshold)
├── queue.py                 # Event queue management
├── fetchers/
│   ├── __init__.py          # Fetcher registry
│   ├── base.py              # Abstract base fetcher
│   ├── website.py           # Website fetcher (httpx + BeautifulSoup)
│   └── custom.py            # Custom API fetcher
tests/watcher/
├── __init__.py
├── test_models.py           # Model serialization tests
├── test_database.py         # Database initialization tests
├── test_detectors.py        # Change detection tests
├── test_scheduler.py        # Scheduler logic tests
└── test_fetchers.py         # Fetcher tests
```

---

## Task 1: Database Models

**Files:**
- Create: `ares/watcher/__init__.py`
- Create: `ares/watcher/models.py`
- Create: `tests/watcher/__init__.py`
- Create: `tests/watcher/test_models.py`

- [ ] **Step 1: Create package init**

```python
# ares/watcher/__init__.py
"""Ares Watcher Service - Proactive monitoring system."""

from ares.watcher.models import Monitor, Snapshot, Event, Notification, InstagramState
from ares.watcher.database import WatcherDatabase

__all__ = [
    "Monitor",
    "Snapshot",
    "Event",
    "Notification",
    "InstagramState",
    "WatcherDatabase",
]
```

- [ ] **Step 2: Write failing test for Monitor model**

```python
# tests/watcher/test_models.py
"""Tests for watcher data models."""

import pytest
from datetime import datetime
from ares.watcher.models import Monitor


def test_monitor_creation():
    """Test creating a Monitor with required fields."""
    monitor = Monitor(
        id="test-123",
        name="Amazon PS5 Price",
        type="website",
        url="https://amazon.com/dp/B08N5WRWNW",
        config={"extractors": [{"field": "price", "selector": "#price"}]},
        interval_seconds=900,
        ai_action="notify",
        enabled=True,
        created_at=datetime.now(),
        updated_at=datetime.now(),
    )
    assert monitor.id == "test-123"
    assert monitor.name == "Amazon PS5 Price"
    assert monitor.type == "website"
    assert monitor.interval_seconds == 900
    assert monitor.enabled is True


def test_monitor_defaults():
    """Test Monitor default values."""
    monitor = Monitor(
        id="test-456",
        name="Test Monitor",
        type="custom",
        created_at=datetime.now(),
        updated_at=datetime.now(),
    )
    assert monitor.interval_seconds == 900
    assert monitor.ai_action == "notify"
    assert monitor.enabled is True
    assert monitor.error_count == 0
    assert monitor.last_status is None


def test_monitor_to_dict():
    """Test Monitor serialization to dictionary."""
    monitor = Monitor(
        id="test-789",
        name="Test",
        type="website",
        url="https://example.com",
        config={"key": "value"},
        created_at=datetime(2026, 1, 1),
        updated_at=datetime(2026, 1, 1),
    )
    data = monitor.to_dict()
    assert data["id"] == "test-789"
    assert data["name"] == "Test"
    assert data["config"] == {"key": "value"}
    assert "created_at" in data
```

- [ ] **Step 3: Run test to verify it fails**

Run: `cd friday && python -m pytest tests/watcher/test_models.py::test_monitor_creation -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'ares.watcher.models'`

- [ ] **Step 4: Implement Monitor model**

```python
# ares/watcher/models.py
"""SQLAlchemy models for the Watcher Service."""

from datetime import datetime
from typing import Optional, Dict, Any
from dataclasses import dataclass, field
import json


@dataclass
class Monitor:
    """Monitor configuration model."""

    id: str
    name: str
    type: str  # 'website', 'instagram', 'custom'
    url: Optional[str] = None
    config: Dict[str, Any] = field(default_factory=dict)
    interval_seconds: int = 900
    ai_action: str = "notify"  # 'notify', 'auto', 'suggest'
    ai_prompt: Optional[str] = None
    enabled: bool = True
    last_checked_at: Optional[datetime] = None
    last_status: Optional[str] = None  # 'ok', 'error', 'timeout'
    error_count: int = 0
    created_at: datetime = field(default_factory=datetime.now)
    updated_at: datetime = field(default_factory=datetime.now)

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to dictionary."""
        return {
            "id": self.id,
            "name": self.name,
            "type": self.type,
            "url": self.url,
            "config": self.config,
            "interval_seconds": self.interval_seconds,
            "ai_action": self.ai_action,
            "ai_prompt": self.ai_prompt,
            "enabled": self.enabled,
            "last_checked_at": self.last_checked_at.isoformat() if self.last_checked_at else None,
            "last_status": self.last_status,
            "error_count": self.error_count,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Monitor":
        """Deserialize from dictionary."""
        return cls(
            id=data["id"],
            name=data["name"],
            type=data["type"],
            url=data.get("url"),
            config=data.get("config", {}),
            interval_seconds=data.get("interval_seconds", 900),
            ai_action=data.get("ai_action", "notify"),
            ai_prompt=data.get("ai_prompt"),
            enabled=data.get("enabled", True),
            last_checked_at=datetime.fromisoformat(data["last_checked_at"]) if data.get("last_checked_at") else None,
            last_status=data.get("last_status"),
            error_count=data.get("error_count", 0),
            created_at=datetime.fromisoformat(data["created_at"]),
            updated_at=datetime.fromisoformat(data["updated_at"]),
        )


@dataclass
class Snapshot:
    """Stored snapshot for change detection."""

    id: str
    monitor_id: str
    content_hash: Optional[str] = None
    content: Optional[str] = None
    price_value: Optional[float] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=datetime.now)

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to dictionary."""
        return {
            "id": self.id,
            "monitor_id": self.monitor_id,
            "content_hash": self.content_hash,
            "content": self.content,
            "price_value": self.price_value,
            "metadata": self.metadata,
            "created_at": self.created_at.isoformat(),
        }


@dataclass
class Event:
    """Detected change event."""

    id: str
    monitor_id: str
    event_type: str  # 'price_change', 'content_change', 'new_message'
    old_value: Optional[str] = None
    new_value: Optional[str] = None
    change_summary: Optional[str] = None
    severity: str = "info"  # 'info', 'warning', 'critical'
    notified: bool = False
    ai_analyzed: bool = False
    created_at: datetime = field(default_factory=datetime.now)

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to dictionary."""
        return {
            "id": self.id,
            "monitor_id": self.monitor_id,
            "event_type": self.event_type,
            "old_value": self.old_value,
            "new_value": self.new_value,
            "change_summary": self.change_summary,
            "severity": self.severity,
            "notified": self.notified,
            "ai_analyzed": self.ai_analyzed,
            "created_at": self.created_at.isoformat(),
        }


@dataclass
class Notification:
    """Notification delivery record."""

    id: str
    event_id: str
    channel: str  # 'telegram', 'desktop', 'email'
    status: str = "pending"  # 'pending', 'sent', 'failed'
    sent_at: Optional[datetime] = None
    error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to dictionary."""
        return {
            "id": self.id,
            "event_id": self.event_id,
            "channel": self.channel,
            "status": self.status,
            "sent_at": self.sent_at.isoformat() if self.sent_at else None,
            "error": self.error,
        }


@dataclass
class InstagramState:
    """Instagram-specific monitor state."""

    id: str
    monitor_id: str
    last_dm_id: Optional[str] = None
    last_mention_id: Optional[str] = None
    last_check_at: Optional[datetime] = None

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to dictionary."""
        return {
            "id": self.id,
            "monitor_id": self.monitor_id,
            "last_dm_id": self.last_dm_id,
            "last_mention_id": self.last_mention_id,
            "last_check_at": self.last_check_at.isoformat() if self.last_check_at else None,
        }
```

- [ ] **Step 5: Run test to verify it passes**

Run: `cd friday && python -m pytest tests/watcher/test_models.py::test_monitor_creation tests/watcher/test_models.py::test_monitor_defaults tests/watcher/test_models.py::test_monitor_to_dict -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
cd friday && git add ares/watcher/__init__.py ares/watcher/models.py tests/watcher/__init__.py tests/watcher/test_models.py
git commit -m "feat(watcher): add core data models

Add Monitor, Snapshot, Event, Notification, and InstagramState
dataclasses with serialization support."
```

---

## Task 2: Database Initialization

**Files:**
- Create: `ares/watcher/database.py`
- Create: `tests/watcher/test_database.py`

- [ ] **Step 1: Write failing test for database initialization**

```python
# tests/watcher/test_database.py
"""Tests for watcher database initialization."""

import pytest
import tempfile
import os
from ares.watcher.database import WatcherDatabase


@pytest.fixture
def temp_db():
    """Create a temporary database for testing."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test_watchers.db")
        db = WatcherDatabase(db_path)
        yield db
        db.close()


def test_database_initialization(temp_db):
    """Test that database creates all tables on init."""
    # Database should be initialized with all tables
    assert temp_db is not None
    # Check tables exist by querying sqlite_master
    import sqlite3
    conn = sqlite3.connect(temp_db.db_path)
    cursor = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    )
    tables = [row[0] for row in cursor.fetchall()]
    conn.close()

    assert "monitors" in tables
    assert "snapshots" in tables
    assert "events" in tables
    assert "notifications" in tables
    assert "instagram_state" in tables


def test_database_insert_monitor(temp_db):
    """Test inserting a monitor into the database."""
    from ares.watcher.models import Monitor
    from datetime import datetime

    monitor = Monitor(
        id="test-insert-1",
        name="Test Monitor",
        type="website",
        url="https://example.com",
        created_at=datetime.now(),
        updated_at=datetime.now(),
    )

    temp_db.insert_monitor(monitor)

    # Retrieve and verify
    retrieved = temp_db.get_monitor("test-insert-1")
    assert retrieved is not None
    assert retrieved.name == "Test Monitor"
    assert retrieved.type == "website"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd friday && python -m pytest tests/watcher/test_database.py::test_database_initialization -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'ares.watcher.database'`

- [ ] **Step 3: Implement database initialization**

```python
# ares/watcher/database.py
"""Database connection and initialization for Watcher Service."""

import sqlite3
import json
from datetime import datetime
from typing import Optional, List
from pathlib import Path

from ares.watcher.models import Monitor, Snapshot, Event, Notification, InstagramState


SCHEMA_SQL = """
-- Monitor configurations
CREATE TABLE IF NOT EXISTS monitors (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    type TEXT NOT NULL,
    url TEXT,
    config JSON,
    interval_seconds INTEGER DEFAULT 900,
    ai_action TEXT DEFAULT 'notify',
    ai_prompt TEXT,
    enabled BOOLEAN DEFAULT 1,
    last_checked_at TIMESTAMP,
    last_status TEXT,
    error_count INTEGER DEFAULT 0,
    created_at TIMESTAMP,
    updated_at TIMESTAMP
);

-- Stored snapshots for change detection
CREATE TABLE IF NOT EXISTS snapshots (
    id TEXT PRIMARY KEY,
    monitor_id TEXT REFERENCES monitors(id) ON DELETE CASCADE,
    content_hash TEXT,
    content TEXT,
    price_value REAL,
    metadata JSON,
    created_at TIMESTAMP
);

-- Detected changes / events
CREATE TABLE IF NOT EXISTS events (
    id TEXT PRIMARY KEY,
    monitor_id TEXT REFERENCES monitors(id) ON DELETE CASCADE,
    event_type TEXT NOT NULL,
    old_value TEXT,
    new_value TEXT,
    change_summary TEXT,
    severity TEXT DEFAULT 'info',
    notified BOOLEAN DEFAULT 0,
    ai_analyzed BOOLEAN DEFAULT 0,
    created_at TIMESTAMP
);

-- Notification log
CREATE TABLE IF NOT EXISTS notifications (
    id TEXT PRIMARY KEY,
    event_id TEXT REFERENCES events(id) ON DELETE CASCADE,
    channel TEXT NOT NULL,
    status TEXT DEFAULT 'pending',
    sent_at TIMESTAMP,
    error TEXT
);

-- Instagram-specific state
CREATE TABLE IF NOT EXISTS instagram_state (
    id TEXT PRIMARY KEY,
    monitor_id TEXT REFERENCES monitors(id) ON DELETE CASCADE,
    last_dm_id TEXT,
    last_mention_id TEXT,
    last_check_at TIMESTAMP
);

-- Indexes
CREATE INDEX IF NOT EXISTS idx_monitors_enabled ON monitors(enabled);
CREATE INDEX IF NOT EXISTS idx_snapshots_monitor ON snapshots(monitor_id, created_at);
CREATE INDEX IF NOT EXISTS idx_events_monitor ON events(monitor_id, created_at);
CREATE INDEX IF NOT EXISTS idx_events_notified ON events(notified, created_at);
"""


class WatcherDatabase:
    """SQLite database for the Watcher Service."""

    def __init__(self, db_path: str):
        """Initialize database with schema."""
        self.db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self._initialize_schema()

    def _initialize_schema(self):
        """Create all tables if they don't exist."""
        self.conn.executescript(SCHEMA_SQL)
        self.conn.commit()

    def close(self):
        """Close database connection."""
        if self.conn:
            self.conn.close()

    def insert_monitor(self, monitor: Monitor) -> None:
        """Insert a monitor configuration."""
        self.conn.execute(
            """INSERT INTO monitors (id, name, type, url, config, interval_seconds,
               ai_action, ai_prompt, enabled, last_checked_at, last_status,
               error_count, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                monitor.id,
                monitor.name,
                monitor.type,
                monitor.url,
                json.dumps(monitor.config),
                monitor.interval_seconds,
                monitor.ai_action,
                monitor.ai_prompt,
                monitor.enabled,
                monitor.last_checked_at.isoformat() if monitor.last_checked_at else None,
                monitor.last_status,
                monitor.error_count,
                monitor.created_at.isoformat(),
                monitor.updated_at.isoformat(),
            ),
        )
        self.conn.commit()

    def get_monitor(self, monitor_id: str) -> Optional[Monitor]:
        """Retrieve a monitor by ID."""
        cursor = self.conn.execute("SELECT * FROM monitors WHERE id = ?", (monitor_id,))
        row = cursor.fetchone()
        if row is None:
            return None
        return Monitor(
            id=row["id"],
            name=row["name"],
            type=row["type"],
            url=row["url"],
            config=json.loads(row["config"]) if row["config"] else {},
            interval_seconds=row["interval_seconds"],
            ai_action=row["ai_action"],
            ai_prompt=row["ai_prompt"],
            enabled=bool(row["enabled"]),
            last_checked_at=datetime.fromisoformat(row["last_checked_at"]) if row["last_checked_at"] else None,
            last_status=row["last_status"],
            error_count=row["error_count"],
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )

    def list_monitors(self, enabled_only: bool = False) -> List[Monitor]:
        """List all monitors."""
        if enabled_only:
            cursor = self.conn.execute("SELECT * FROM monitors WHERE enabled = 1")
        else:
            cursor = self.conn.execute("SELECT * FROM monitors")
        rows = cursor.fetchall()
        return [
            Monitor(
                id=row["id"],
                name=row["name"],
                type=row["type"],
                url=row["url"],
                config=json.loads(row["config"]) if row["config"] else {},
                interval_seconds=row["interval_seconds"],
                ai_action=row["ai_action"],
                ai_prompt=row["ai_prompt"],
                enabled=bool(row["enabled"]),
                last_checked_at=datetime.fromisoformat(row["last_checked_at"]) if row["last_checked_at"] else None,
                last_status=row["last_status"],
                error_count=row["error_count"],
                created_at=datetime.fromisoformat(row["created_at"]),
                updated_at=datetime.fromisoformat(row["updated_at"]),
            )
            for row in rows
        ]

    def update_monitor(self, monitor: Monitor) -> None:
        """Update an existing monitor."""
        monitor.updated_at = datetime.now()
        self.conn.execute(
            """UPDATE monitors SET name=?, type=?, url=?, config=?, interval_seconds=?,
               ai_action=?, ai_prompt=?, enabled=?, last_checked_at=?, last_status=?,
               error_count=?, updated_at=?
               WHERE id=?""",
            (
                monitor.name,
                monitor.type,
                monitor.url,
                json.dumps(monitor.config),
                monitor.interval_seconds,
                monitor.ai_action,
                monitor.ai_prompt,
                monitor.enabled,
                monitor.last_checked_at.isoformat() if monitor.last_checked_at else None,
                monitor.last_status,
                monitor.error_count,
                monitor.updated_at.isoformat(),
                monitor.id,
            ),
        )
        self.conn.commit()

    def delete_monitor(self, monitor_id: str) -> None:
        """Delete a monitor and its related data."""
        self.conn.execute("DELETE FROM monitors WHERE id = ?", (monitor_id,))
        self.conn.commit()

    def insert_snapshot(self, snapshot: Snapshot) -> None:
        """Insert a snapshot."""
        self.conn.execute(
            """INSERT INTO snapshots (id, monitor_id, content_hash, content,
               price_value, metadata, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                snapshot.id,
                snapshot.monitor_id,
                snapshot.content_hash,
                snapshot.content,
                snapshot.price_value,
                json.dumps(snapshot.metadata),
                snapshot.created_at.isoformat(),
            ),
        )
        self.conn.commit()

    def get_latest_snapshot(self, monitor_id: str) -> Optional[Snapshot]:
        """Get the most recent snapshot for a monitor."""
        cursor = self.conn.execute(
            "SELECT * FROM snapshots WHERE monitor_id = ? ORDER BY created_at DESC LIMIT 1",
            (monitor_id,),
        )
        row = cursor.fetchone()
        if row is None:
            return None
        return Snapshot(
            id=row["id"],
            monitor_id=row["monitor_id"],
            content_hash=row["content_hash"],
            content=row["content"],
            price_value=row["price_value"],
            metadata=json.loads(row["metadata"]) if row["metadata"] else {},
            created_at=datetime.fromisoformat(row["created_at"]),
        )

    def insert_event(self, event: Event) -> None:
        """Insert an event."""
        self.conn.execute(
            """INSERT INTO events (id, monitor_id, event_type, old_value, new_value,
               change_summary, severity, notified, ai_analyzed, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                event.id,
                event.monitor_id,
                event.event_type,
                event.old_value,
                event.new_value,
                event.change_summary,
                event.severity,
                event.notified,
                event.ai_analyzed,
                event.created_at.isoformat(),
            ),
        )
        self.conn.commit()

    def get_unnotified_events(self) -> List[Event]:
        """Get all events that haven't been notified yet."""
        cursor = self.conn.execute(
            "SELECT * FROM events WHERE notified = 0 ORDER BY created_at"
        )
        rows = cursor.fetchall()
        return [
            Event(
                id=row["id"],
                monitor_id=row["monitor_id"],
                event_type=row["event_type"],
                old_value=row["old_value"],
                new_value=row["new_value"],
                change_summary=row["change_summary"],
                severity=row["severity"],
                notified=bool(row["notified"]),
                ai_analyzed=bool(row["ai_analyzed"]),
                created_at=datetime.fromisoformat(row["created_at"]),
            )
            for row in rows
        ]

    def mark_event_notified(self, event_id: str) -> None:
        """Mark an event as notified."""
        self.conn.execute("UPDATE events SET notified = 1 WHERE id = ?", (event_id,))
        self.conn.commit()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd friday && python -m pytest tests/watcher/test_database.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
cd friday && git add ares/watcher/database.py tests/watcher/test_database.py
git commit -m "feat(watcher): add database initialization and CRUD operations

SQLite database with schema creation, monitor CRUD, snapshot storage,
and event queue management."
```

---

## Task 3: Change Detection Engine

**Files:**
- Create: `ares/watcher/detectors.py`
- Create: `tests/watcher/test_detectors.py`

- [ ] **Step 1: Write failing tests for detectors**

```python
# tests/watcher/test_detectors.py
"""Tests for change detection algorithms."""

import pytest
from ares.watcher.detectors import HashDetector, DiffDetector, ThresholdDetector


def test_hash_detector_no_change():
    """Test hash detector with identical content."""
    detector = HashDetector()
    result = detector.detect(
        old_content="Hello World",
        new_content="Hello World",
        config={},
    )
    assert result.changed is False
    assert result.old_hash == result.new_hash


def test_hash_detector_with_change():
    """Test hash detector with different content."""
    detector = HashDetector()
    result = detector.detect(
        old_content="Hello World",
        new_content="Hello Changed",
        config={},
    )
    assert result.changed is True
    assert result.old_hash != result.new_hash


def test_threshold_detector_price_drop():
    """Test threshold detector with price below threshold."""
    detector = ThresholdDetector()
    result = detector.detect(
        old_value=549.99,
        new_value=499.99,
        config={"alert_below": 500.0},
    )
    assert result.changed is True
    assert result.change_type == "below_threshold"


def test_threshold_detector_price_increase():
    """Test threshold detector with price above threshold."""
    detector = ThresholdDetector()
    result = detector.detect(
        old_value=499.99,
        new_value=549.99,
        config={"alert_above": 500.0},
    )
    assert result.changed is True
    assert result.change_type == "above_threshold"


def test_threshold_detector_no_change():
    """Test threshold detector with no threshold breach."""
    detector = ThresholdDetector()
    result = detector.detect(
        old_value=450.0,
        new_value=475.0,
        config={"alert_below": 400.0, "alert_above": 500.0},
    )
    assert result.changed is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd friday && python -m pytest tests/watcher/test_detectors.py::test_hash_detector_no_change -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'ares.watcher.detectors'`

- [ ] **Step 3: Implement detectors**

```python
# ares/watcher/detectors.py
"""Change detection algorithms for the Watcher Service."""

import hashlib
from dataclasses import dataclass
from typing import Any, Optional
from abc import ABC, abstractmethod


@dataclass
class DetectionResult:
    """Result of a change detection check."""

    changed: bool
    old_hash: Optional[str] = None
    new_hash: Optional[str] = None
    old_value: Optional[Any] = None
    new_value: Optional[Any] = None
    change_type: Optional[str] = None
    change_summary: Optional[str] = None


class BaseDetector(ABC):
    """Abstract base class for change detectors."""

    @abstractmethod
    def detect(
        self,
        old_content: Any = None,
        new_content: Any = None,
        config: dict = None,
    ) -> DetectionResult:
        """Detect if content has changed."""
        pass


class HashDetector(BaseDetector):
    """Detects changes by comparing content hashes."""

    def _hash(self, content: str) -> str:
        """Generate SHA256 hash of content."""
        return hashlib.sha256(content.encode("utf-8")).hexdigest()

    def detect(
        self,
        old_content: Any = None,
        new_content: Any = None,
        config: dict = None,
    ) -> DetectionResult:
        """Compare hashes of old and new content."""
        old_str = str(old_content) if old_content else ""
        new_str = str(new_content) if new_content else ""

        old_hash = self._hash(old_str)
        new_hash = self._hash(new_str)

        changed = old_hash != new_hash

        return DetectionResult(
            changed=changed,
            old_hash=old_hash,
            new_hash=new_hash,
            old_value=old_content,
            new_value=new_content,
            change_type="hash_change" if changed else None,
            change_summary=f"Content changed" if changed else None,
        )


class DiffDetector(BaseDetector):
    """Detects changes by comparing text line-by-line."""

    def detect(
        self,
        old_content: Any = None,
        new_content: Any = None,
        config: dict = None,
    ) -> DetectionResult:
        """Compare old and new content line-by-line."""
        old_str = str(old_content) if old_content else ""
        new_str = str(new_content) if new_content else ""

        old_lines = old_str.splitlines()
        new_lines = new_str.splitlines()

        if old_lines == new_lines:
            return DetectionResult(
                changed=False,
                old_value=old_content,
                new_value=new_content,
            )

        # Calculate diff summary
        added = len(new_lines) - len(old_lines)
        change_type = "content_change"

        if added > 0:
            summary = f"{added} lines added"
        elif added < 0:
            summary = f"{abs(added)} lines removed"
        else:
            summary = "Content modified"

        return DetectionResult(
            changed=True,
            old_value=old_content,
            new_value=new_content,
            change_type=change_type,
            change_summary=summary,
        )


class ThresholdDetector(BaseDetector):
    """Detects when values cross configured thresholds."""

    def detect(
        self,
        old_value: Any = None,
        new_value: Any = None,
        config: dict = None,
    ) -> DetectionResult:
        """Check if value has crossed any configured threshold."""
        config = config or {}

        try:
            old_num = float(old_value) if old_value is not None else None
            new_num = float(new_value) if new_value is not None else None
        except (TypeError, ValueError):
            return DetectionResult(
                changed=False,
                old_value=old_value,
                new_value=new_value,
            )

        if old_num is None or new_num is None:
            return DetectionResult(
                changed=False,
                old_value=old_value,
                new_value=new_value,
            )

        # Check thresholds
        alert_below = config.get("alert_below")
        alert_above = config.get("alert_above")
        max_change_pct = config.get("max_change_pct")

        # Check below threshold
        if alert_below is not None and new_num < alert_below:
            if old_num is None or old_num >= alert_below:
                return DetectionResult(
                    changed=True,
                    old_value=old_value,
                    new_value=new_value,
                    change_type="below_threshold",
                    change_summary=f"Value {new_num} is below threshold {alert_below}",
                )

        # Check above threshold
        if alert_above is not None and new_num > alert_above:
            if old_num is None or old_num <= alert_above:
                return DetectionResult(
                    changed=True,
                    old_value=old_value,
                    new_value=new_value,
                    change_type="above_threshold",
                    change_summary=f"Value {new_num} is above threshold {alert_above}",
                )

        # Check percentage change
        if max_change_pct is not None and old_num != 0:
            change_pct = abs((new_num - old_num) / old_num * 100)
            if change_pct > max_change_pct:
                direction = "increased" if new_num > old_num else "decreased"
                return DetectionResult(
                    changed=True,
                    old_value=old_value,
                    new_value=new_value,
                    change_type="threshold_exceeded",
                    change_summary=f"Value {direction} by {change_pct:.1f}% (threshold: {max_change_pct}%)",
                )

        return DetectionResult(
            changed=False,
            old_value=old_value,
            new_value=new_value,
        )


def get_detector(detector_type: str) -> BaseDetector:
    """Factory function to get detector by type."""
    detectors = {
        "hash": HashDetector,
        "diff": DiffDetector,
        "threshold": ThresholdDetector,
    }
    detector_class = detectors.get(detector_type)
    if detector_class is None:
        raise ValueError(f"Unknown detector type: {detector_type}")
    return detector_class()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd friday && python -m pytest tests/watcher/test_detectors.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
cd friday && git add ares/watcher/detectors.py tests/watcher/test_detectors.py
git commit -m "feat(watcher): add change detection engine

Hash, diff, and threshold detectors for monitoring changes."
```

---

## Task 4: Fetcher Base and Website Fetcher

**Files:**
- Create: `ares/watcher/fetchers/__init__.py`
- Create: `ares/watcher/fetchers/base.py`
- Create: `ares/watcher/fetchers/website.py`
- Create: `tests/watcher/test_fetchers.py`

- [ ] **Step 1: Write failing test for website fetcher**

```python
# tests/watcher/test_fetchers.py
"""Tests for watcher fetchers."""

import pytest
from ares.watcher.fetchers.base import FetcherResult
from ares.watcher.fetchers.website import WebsiteFetcher


def test_fetcher_result_creation():
    """Test creating a FetcherResult."""
    result = FetcherResult(
        success=True,
        content="<h1>Hello</h1>",
        extracted={"title": "Hello"},
        metadata={"status_code": 200},
    )
    assert result.success is True
    assert result.content == "<h1>Hello</h1>"
    assert result.extracted["title"] == "Hello"


def test_website_fetcher_init():
    """Test initializing WebsiteFetcher."""
    fetcher = WebsiteFetcher()
    assert fetcher is not None


def test_website_fetcher_extract_price():
    """Test price extraction from HTML."""
    fetcher = WebsiteFetcher()
    html = '<span id="price">$499.99</span>'
    price = fetcher.extract_value(html, {"selector": "#price", "type": "price"})
    assert price == 499.99
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd friday && python -m pytest tests/watcher/test_fetchers.py::test_fetcher_result_creation -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'ares.watcher.fetchers'`

- [ ] **Step 3: Implement fetcher base and website fetcher**

```python
# ares/watcher/fetchers/__init__.py
"""Fetcher modules for different monitor types."""

from ares.watcher.fetchers.base import BaseFetcher, FetcherResult
from ares.watcher.fetchers.website import WebsiteFetcher

__all__ = ["BaseFetcher", "FetcherResult", "WebsiteFetcher"]
```

```python
# ares/watcher/fetchers/base.py
"""Abstract base fetcher class."""

from dataclasses import dataclass, field
from typing import Any, Dict, Optional
from abc import ABC, abstractmethod


@dataclass
class FetcherResult:
    """Result from a fetcher operation."""

    success: bool
    content: Optional[str] = None
    extracted: Dict[str, Any] = field(default_factory=dict)
    metadata: Dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None


class BaseFetcher(ABC):
    """Abstract base class for all fetchers."""

    @abstractmethod
    async def fetch(self, config: dict) -> FetcherResult:
        """Fetch data from the source."""
        pass

    def extract_value(self, content: str, extractor: dict) -> Any:
        """Extract a value from content based on extractor config."""
        # Default implementation - override in subclasses
        return content
```

```python
# ares/watcher/fetchers/website.py
"""Website fetcher using httpx and BeautifulSoup."""

import re
import httpx
from typing import Any, Dict, Optional
from bs4 import BeautifulSoup

from ares.watcher.fetchers.base import BaseFetcher, FetcherResult


class WebsiteFetcher(BaseFetcher):
    """Fetches and parses website content."""

    DEFAULT_HEADERS = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    }

    async def fetch(self, config: dict) -> FetcherResult:
        """Fetch website content."""
        url = config.get("url")
        if not url:
            return FetcherResult(success=False, error="No URL provided")

        headers = {**self.DEFAULT_HEADERS, **config.get("headers", {})}
        timeout = config.get("timeout", 30)

        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(
                    url,
                    headers=headers,
                    timeout=timeout,
                    follow_redirects=True,
                )
                response.raise_for_status()

                content = response.text
                extractors = config.get("extractors", [])

                extracted = {}
                for ext in extractors:
                    field_name = ext.get("field", "value")
                    extracted[field_name] = self.extract_value(content, ext)

                return FetcherResult(
                    success=True,
                    content=content,
                    extracted=extracted,
                    metadata={"status_code": response.status_code, "url": str(response.url)},
                )

        except httpx.TimeoutException:
            return FetcherResult(success=False, error="Request timed out")
        except httpx.HTTPStatusError as e:
            return FetcherResult(success=False, error=f"HTTP error: {e.response.status_code}")
        except Exception as e:
            return FetcherResult(success=False, error=str(e))

    def extract_value(self, content: str, extractor: dict) -> Any:
        """Extract a value from HTML content."""
        selector = extractor.get("selector")
        extract_type = extractor.get("type", "text")

        if not selector:
            return content

        soup = BeautifulSoup(content, "html.parser")
        element = soup.select_one(selector)

        if element is None:
            return None

        if extract_type == "price":
            return self._extract_price(element.get_text())
        elif extract_type == "text":
            return element.get_text(strip=True)
        elif extract_type == "html":
            return str(element)
        else:
            return element.get_text(strip=True)

    def _extract_price(self, text: str) -> Optional[float]:
        """Extract numeric price from text."""
        # Remove currency symbols and whitespace
        cleaned = re.sub(r'[^\d.,]', '', text)
        # Handle different price formats
        cleaned = cleaned.replace(',', '')
        try:
            return float(cleaned)
        except ValueError:
            return None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd friday && python -m pytest tests/watcher/test_fetchers.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
cd friday && git add ares/watcher/fetchers/ tests/watcher/test_fetchers.py
git commit -m "feat(watcher): add base fetcher and website fetcher

Abstract base fetcher with website implementation using httpx
and BeautifulSoup for HTML parsing and price extraction."
```

---

## Task 5: Event Queue

**Files:**
- Create: `ares/watcher/queue.py`
- Add tests to: `tests/watcher/test_database.py`

- [ ] **Step 1: Write failing test for event queue**

```python
# Add to tests/watcher/test_database.py

def test_event_queue_add_and_get(temp_db):
    """Test adding events to queue and retrieving them."""
    from ares.watcher.queue import EventQueue
    from ares.watcher.models import Event
    from datetime import datetime

    queue = EventQueue(temp_db)

    event = Event(
        id="event-1",
        monitor_id="monitor-1",
        event_type="price_change",
        old_value="$549.99",
        new_value="$499.99",
        change_summary="Price dropped by $50",
        severity="info",
        created_at=datetime.now(),
    )

    queue.add_event(event)

    # Get unnotified events
    events = queue.get_unnotified_events()
    assert len(events) == 1
    assert events[0].id == "event-1"

    # Mark as notified
    queue.mark_notified("event-1")

    # Should be empty now
    events = queue.get_unnotified_events()
    assert len(events) == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd friday && python -m pytest tests/watcher/test_database.py::test_event_queue_add_and_get -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'ares.watcher.queue'`

- [ ] **Step 3: Implement event queue**

```python
# ares/watcher/queue.py
"""Event queue management for the Watcher Service."""

from typing import List
from ares.watcher.database import WatcherDatabase
from ares.watcher.models import Event


class EventQueue:
    """Manages the event queue for detected changes."""

    def __init__(self, db: WatcherDatabase):
        """Initialize with database connection."""
        self.db = db

    def add_event(self, event: Event) -> None:
        """Add an event to the queue."""
        self.db.insert_event(event)

    def get_unnotified_events(self) -> List[Event]:
        """Get all events that haven't been notified yet."""
        return self.db.get_unnotified_events()

    def mark_notified(self, event_id: str) -> None:
        """Mark an event as notified."""
        self.db.mark_event_notified(event_id)

    def get_events_for_monitor(self, monitor_id: str, limit: int = 10) -> List[Event]:
        """Get recent events for a specific monitor."""
        cursor = self.db.conn.execute(
            "SELECT * FROM events WHERE monitor_id = ? ORDER BY created_at DESC LIMIT ?",
            (monitor_id, limit),
        )
        rows = cursor.fetchall()
        return [
            Event(
                id=row["id"],
                monitor_id=row["monitor_id"],
                event_type=row["event_type"],
                old_value=row["old_value"],
                new_value=row["new_value"],
                change_summary=row["change_summary"],
                severity=row["severity"],
                notified=bool(row["notified"]),
                ai_analyzed=bool(row["ai_analyzed"]),
                created_at=__import__("datetime").datetime.fromisoformat(row["created_at"]),
            )
            for row in rows
        ]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd friday && python -m pytest tests/watcher/test_database.py::test_event_queue_add_and_get -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
cd friday && git add ares/watcher/queue.py tests/watcher/test_database.py
git commit -m "feat(watcher): add event queue management

Event queue for storing and retrieving detected changes."
```

---

## Task 6: Scheduler Service

**Files:**
- Create: `ares/watcher/scheduler.py`
- Create: `tests/watcher/test_scheduler.py`

- [ ] **Step 1: Write failing test for scheduler**

```python
# tests/watcher/test_scheduler.py
"""Tests for watcher scheduler."""

import pytest
import asyncio
from datetime import datetime, timedelta
from ares.watcher.scheduler import WatcherScheduler
from ares.watcher.database import WatcherDatabase
from ares.watcher.models import Monitor


@pytest.fixture
def temp_db():
    """Create a temporary database for testing."""
    import tempfile
    import os
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test_watchers.db")
        db = WatcherDatabase(db_path)
        yield db
        db.close()


def test_scheduler_creation(temp_db):
    """Test creating a WatcherScheduler."""
    scheduler = WatcherScheduler(temp_db)
    assert scheduler is not None
    assert scheduler.db == temp_db


def test_scheduler_should_check(temp_db):
    """Test scheduler check timing logic."""
    scheduler = WatcherScheduler(temp_db)

    # Monitor never checked - should check
    monitor = Monitor(
        id="test-1",
        name="Test",
        type="website",
        interval_seconds=60,
        created_at=datetime.now(),
        updated_at=datetime.now(),
    )
    assert scheduler.should_check(monitor) is True

    # Monitor checked recently - should not check
    monitor.last_checked_at = datetime.now()
    assert scheduler.should_check(monitor) is False

    # Monitor checked long ago - should check
    monitor.last_checked_at = datetime.now() - timedelta(seconds=120)
    assert scheduler.should_check(monitor) is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd friday && python -m pytest tests/watcher/test_scheduler.py::test_scheduler_creation -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'ares.watcher.scheduler'`

- [ ] **Step 3: Implement scheduler**

```python
# ares/watcher/scheduler.py
"""Main scheduler loop for the Watcher Service."""

import asyncio
import logging
from datetime import datetime
from typing import Optional
from uuid import uuid4

from ares.watcher.database import WatcherDatabase
from ares.watcher.models import Monitor, Snapshot, Event
from ares.watcher.detectors import HashDetector, ThresholdDetector
from ares.watcher.queue import EventQueue
from ares.watcher.fetchers.website import WebsiteFetcher

logger = logging.getLogger(__name__)


class WatcherScheduler:
    """Schedules and runs monitor checks."""

    def __init__(self, db: WatcherDatabase):
        """Initialize scheduler with database."""
        self.db = db
        self.queue = EventQueue(db)
        self.running = False
        self._fetchers = {
            "website": WebsiteFetcher(),
        }
        self._detectors = {
            "hash": HashDetector(),
            "threshold": ThresholdDetector(),
        }

    def should_check(self, monitor: Monitor) -> bool:
        """Determine if a monitor should be checked."""
        if not monitor.enabled:
            return False

        if monitor.last_checked_at is None:
            return True

        elapsed = (datetime.now() - monitor.last_checked_at).total_seconds()
        return elapsed >= monitor.interval_seconds

    async def check_monitor(self, monitor: Monitor) -> Optional[Event]:
        """Run a single monitor check."""
        fetcher = self._fetchers.get(monitor.type)
        if fetcher is None:
            logger.warning(f"No fetcher for monitor type: {monitor.type}")
            return None

        try:
            result = await fetcher.fetch(monitor.url or monitor.config)

            if not result.success:
                monitor.error_count += 1
                monitor.last_status = "error"
                if monitor.error_count >= 5:
                    monitor.enabled = False
                    logger.warning(f"Monitor {monitor.name} disabled after 5 failures")
                self.db.update_monitor(monitor)
                return None

            # Get previous snapshot
            prev_snapshot = self.db.get_latest_snapshot(monitor.id)

            # Create new snapshot
            new_snapshot = Snapshot(
                id=str(uuid4()),
                monitor_id=monitor.id,
                content=result.content,
                metadata=result.metadata,
                created_at=datetime.now(),
            )

            # Calculate hash
            detector = HashDetector()
            if prev_snapshot:
                det_result = detector.detect(
                    old_content=prev_snapshot.content,
                    new_content=result.content,
                )
                new_snapshot.content_hash = det_result.new_hash

                if det_result.changed:
                    event = Event(
                        id=str(uuid4()),
                        monitor_id=monitor.id,
                        event_type="content_change",
                        old_value=str(prev_snapshot.content)[:500],
                        new_value=str(result.content)[:500],
                        change_summary="Content changed",
                        created_at=datetime.now(),
                    )
                    self.queue.add_event(event)
            else:
                new_snapshot.content_hash = detector._hash(result.content or "")

            self.db.insert_snapshot(new_snapshot)

            # Update monitor status
            monitor.last_checked_at = datetime.now()
            monitor.last_status = "ok"
            monitor.error_count = 0
            self.db.update_monitor(monitor)

            return None

        except Exception as e:
            logger.error(f"Error checking monitor {monitor.name}: {e}")
            monitor.error_count += 1
            monitor.last_status = "error"
            self.db.update_monitor(monitor)
            return None

    async def run_once(self):
        """Run one iteration of the scheduler."""
        monitors = self.db.list_monitors(enabled_only=True)

        for monitor in monitors:
            if self.should_check(monitor):
                logger.info(f"Checking monitor: {monitor.name}")
                await self.check_monitor(monitor)

    async def run(self, interval: int = 10):
        """Run the scheduler loop."""
        self.running = True
        logger.info("Watcher scheduler started")

        while self.running:
            await self.run_once()
            await asyncio.sleep(interval)

    def stop(self):
        """Stop the scheduler."""
        self.running = False
        logger.info("Watcher scheduler stopped")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd friday && python -m pytest tests/watcher/test_scheduler.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
cd friday && git add ares/watcher/scheduler.py tests/watcher/test_scheduler.py
git commit -m "feat(watcher): add scheduler service

Background scheduler that runs monitor checks on configurable intervals."
```

---

## Task 7: Update pyproject.toml with Dependencies

**Files:**
- Modify: `pyproject.toml`

- [ ] **Step 1: Add watcher dependencies**

```toml
# Add to pyproject.toml [project.optional-dependencies]
watcher = [
    "httpx>=0.27.0",
    "beautifulsoup4>=4.12.0",
]
```

- [ ] **Step 2: Install dependencies**

Run: `cd friday && pip install -e ".[watcher]"`

- [ ] **Step 3: Run all watcher tests**

Run: `cd friday && python -m pytest tests/watcher/ -v`
Expected: All tests PASS

- [ ] **Step 4: Commit**

```bash
cd friday && git add pyproject.toml
git commit -m "feat(watcher): add watcher dependencies to pyproject.toml

Add httpx and beautifulsoup4 as optional dependencies."
```

---

## Summary

After completing this plan, you will have:

1. **Data Models** - Monitor, Snapshot, Event, Notification, InstagramState
2. **Database** - SQLite with full CRUD operations
3. **Change Detection** - Hash, diff, and threshold detectors
4. **Fetchers** - Website fetcher with price extraction
5. **Event Queue** - For tracking and notifying changes
6. **Scheduler** - Background loop for running checks

**Next Plans:**
- `2026-07-12-watcher-notifications.md` - Telegram, Desktop, Email notifications
- `2026-07-12-watcher-instagram.md` - Instagram monitor fetcher
- `2026-07-12-watcher-commands.md` - Terminal and Telegram commands
- `2026-07-12-watcher-dashboard.md` - Web dashboard with FastAPI
