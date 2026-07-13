from datetime import timedelta

from ares.models import AppConfig
from ares.watcher.database import resolve_watcher_database_path
from ares.watcher.models import CheckRun, Event, Monitor, Notification, Snapshot, utc_now
from ares.watcher.queue import EventQueue


def test_database_initialization_creates_all_tables(watcher_db):
    tables = {row[0] for row in watcher_db.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"monitors","snapshots","events","notifications","instagram_state","check_runs"} <= tables
    assert watcher_db.conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_default_watcher_database_follows_custom_ares_data_directory(tmp_path):
    config = AppConfig(data_dir=str(tmp_path))
    assert resolve_watcher_database_path(config) == tmp_path / "watchers.db"


def test_monitor_crud_and_due_query(watcher_db):
    monitor = Monitor(id="m1",name="Health",type="website",url="https://example.com",interval_seconds=60)
    watcher_db.insert_monitor(monitor)
    assert watcher_db.get_monitor("m1").name == "Health"
    assert [item.id for item in watcher_db.list_due_monitors()] == ["m1"]
    monitor.enabled = False; watcher_db.update_monitor(monitor)
    assert watcher_db.list_monitors()[0].enabled is False
    assert watcher_db.list_due_monitors() == []
    assert watcher_db.delete_monitor("m1") is True
    assert watcher_db.get_monitor("m1") is None


def test_due_monitor_leases_prevent_duplicate_workers(watcher_db):
    watcher_db.insert_monitor(Monitor(id="m",name="M",type="website"))
    assert [item.id for item in watcher_db.claim_due_monitors("worker-a")]==["m"]
    assert watcher_db.claim_due_monitors("worker-b")==[]
    monitor=watcher_db.get_monitor("m"); monitor.next_check_at=utc_now()+timedelta(minutes=10); watcher_db.update_monitor(monitor)
    assert watcher_db.claim_due_monitors("worker-b")==[]


def test_snapshot_retention_and_latest(watcher_db):
    watcher_db.insert_monitor(Monitor(id="m",name="M",type="custom"))
    for index in range(4):
        watcher_db.insert_snapshot(Snapshot(id=f"s{index}",monitor_id="m",content=str(index),created_at=utc_now()+timedelta(seconds=index)),retain=2)
    assert watcher_db.get_latest_snapshot("m").content == "3"
    assert watcher_db.conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0] == 2


def test_event_queue_acknowledgement_and_cascade(watcher_db):
    watcher_db.insert_monitor(Monitor(id="m",name="M",type="website"))
    event = Event(id="e",monitor_id="m",event_type="content_change",change_summary="changed")
    queue = EventQueue(watcher_db); queue.add_event(event)
    assert queue.get_unnotified_events()[0].id == "e"
    queue.mark_notified("e"); assert queue.get_unnotified_events() == []
    assert watcher_db.acknowledge_event("e") is True
    assert watcher_db.get_event("e").acknowledged is True
    watcher_db.delete_monitor("m")
    assert watcher_db.get_event("e") is None


def test_notification_check_history_and_overview(watcher_db):
    watcher_db.insert_monitor(Monitor(id="m",name="M",type="website",total_checks=1,total_changes=1,last_duration_ms=120))
    watcher_db.insert_event(Event(id="e",monitor_id="m",event_type="content_change"))
    watcher_db.insert_notification(Notification(id="n",event_id="e",channel="email",status="failed"))
    now = utc_now(); watcher_db.insert_check_run(CheckRun("r","m","ok",now,now,120,True,200,42))
    overview = watcher_db.overview()
    assert overview["monitors"] == 1
    assert overview["unacknowledged_alerts"] == 1
    assert overview["delivery_failures"] == 1
    assert overview["success_rate_24h"] == 100
    assert watcher_db.list_check_runs("m")[0].http_status == 200
