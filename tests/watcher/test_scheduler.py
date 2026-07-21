from datetime import datetime, timedelta

import pytest

from ares.skills.actions import ActionLedger
from ares.skills.goals import GoalStore
from ares.watcher.fetchers.base import BaseFetcher, FetchResult
from ares.watcher.integration import GoalWatcherBridge
from ares.watcher.models import Monitor, utc_now
from ares.watcher.scheduler import WatcherScheduler


class SequenceFetcher(BaseFetcher):
    def __init__(self, results): self.results=list(results); self.closed=False
    async def fetch(self, target, config=None): return self.results.pop(0)
    async def close(self): self.closed=True


def test_scheduler_should_check_respects_enabled_and_interval(watcher_db):
    scheduler=WatcherScheduler(watcher_db,fetchers={})
    monitor=Monitor(id="m",name="M",type="website",interval_seconds=60)
    assert scheduler.should_check(monitor) is True
    monitor.last_checked_at=datetime.now(); assert scheduler.should_check(monitor) is False
    monitor.last_checked_at=datetime.now()-timedelta(seconds=61); assert scheduler.should_check(monitor) is True
    monitor.enabled=False; assert scheduler.should_check(monitor) is False


@pytest.mark.asyncio
async def test_scheduler_baseline_then_detects_diff(watcher_db):
    fetcher=SequenceFetcher([FetchResult(True,"alpha",{"bytes":5},status_code=200),FetchResult(True,"beta",{"bytes":4},status_code=200)])
    monitor=Monitor(id="m",name="Page",type="website",url="https://example.com",interval_seconds=20,config={"change_detection":"diff"})
    watcher_db.insert_monitor(monitor)
    messages=[]
    async def receive(name,payload): messages.append(name)
    scheduler=WatcherScheduler(watcher_db,fetchers={"website":fetcher},on_event=receive)
    assert await scheduler.check_monitor(monitor,force=True) is None
    event=await scheduler.check_monitor(watcher_db.get_monitor("m"),force=True)
    assert event is not None and event.event_type=="content_change"
    saved=watcher_db.get_monitor("m")
    assert saved.total_checks==2 and saved.total_changes==1 and saved.last_status=="ok"
    assert len(watcher_db.list_check_runs("m"))==2
    assert "alert.created" in messages
    await scheduler.close(); assert fetcher.closed is True


@pytest.mark.asyncio
async def test_scheduler_threshold_event(watcher_db):
    fetcher=SequenceFetcher([FetchResult(True,{"price":100}),FetchResult(True,{"price":80})])
    monitor=Monitor(id="m",name="Price",type="custom",interval_seconds=20,config={"threshold_field":"price","change_detection":"threshold","thresholds":{"price":{"alert_below":90}}})
    watcher_db.insert_monitor(monitor); scheduler=WatcherScheduler(watcher_db,fetchers={"custom":fetcher})
    await scheduler.check_monitor(monitor,force=True)
    event=await scheduler.check_monitor(watcher_db.get_monitor("m"),force=True)
    assert event.event_type=="price_change" and event.severity=="critical"
    await scheduler.close()


@pytest.mark.asyncio
async def test_repeated_failures_backoff_and_auto_pause(watcher_db):
    fetcher=SequenceFetcher([FetchResult(False,error="down"),FetchResult(False,error="still down")])
    monitor=Monitor(id="m",name="Broken",type="website",interval_seconds=20)
    watcher_db.insert_monitor(monitor); scheduler=WatcherScheduler(watcher_db,fetchers={"website":fetcher},failure_limit=2)
    await scheduler.check_monitor(monitor,force=True)
    after_one=watcher_db.get_monitor("m")
    assert after_one.error_count==1 and after_one.enabled is True and after_one.next_check_at>utc_now()
    event=await scheduler.check_monitor(after_one,force=True)
    after_two=watcher_db.get_monitor("m")
    assert after_two.enabled is False and event.event_type=="monitor_paused"
    assert watcher_db.overview()["failing"]==1
    await scheduler.close()


@pytest.mark.asyncio
async def test_run_once_checks_all_due_monitors(watcher_db):
    for index in range(3): watcher_db.insert_monitor(Monitor(id=f"m{index}",name=f"M{index}",type="website",interval_seconds=20))
    fetcher=SequenceFetcher([FetchResult(True,str(i)) for i in range(3)])
    scheduler=WatcherScheduler(watcher_db,fetchers={"website":fetcher},max_concurrency=2)
    results=await scheduler.run_once()
    assert len(results)==3 and watcher_db.overview()["total_checks"]==3
    await scheduler.close()


@pytest.mark.asyncio
async def test_watcher_event_fans_out_to_all_linked_goals_without_mutation(tmp_path, watcher_db):
    goals = GoalStore(tmp_path / "ares.db")
    ledger = ActionLedger(tmp_path / "ares.db")
    try:
        first = goals.create("Buy a laptop under $1,000", priority="high")
        second = goals.create("Refresh my work setup")
        monitor = Monitor(
            id="price-watch", name="Laptop price", type="custom", interval_seconds=20,
            config={"threshold_field": "price", "change_detection": "threshold", "thresholds": {"price": {"alert_below": 900}}},
        )
        watcher_db.insert_monitor(monitor)
        for goal in (first, second):
            goals.link(goal["goal_id"], link_type="watcher", ref_id=monitor.id)
        fetcher = SequenceFetcher([FetchResult(True, {"price": 1099}), FetchResult(True, {"price": 899})])
        bridge = GoalWatcherBridge(goals, ledger)
        scheduler = WatcherScheduler(
            watcher_db, fetchers={"custom": fetcher}, goal_signal_handler=bridge.handle_event,
        )
        await scheduler.check_monitor(monitor, force=True)
        event = await scheduler.check_monitor(watcher_db.get_monitor(monitor.id), force=True)

        assert event is not None
        assert "Linked goal signal" in (event.ai_summary or "")
        assert len(goals.list_watcher_signals(watcher_id=monitor.id)) == 2
        assert {item["source_event_id"] for item in goals.list_watcher_signals(watcher_id=monitor.id)} == {event.id}
        assert goals.get(first["goal_id"])["status"] == "active"
        assert goals.get(first["goal_id"])["progress_percent"] == 0
        assert len([item for item in ledger.list_all() if item["action_type"] == "watcher_goal_signal"]) == 2

        # Replay of the same watcher event is idempotent for signals and provenance.
        replayed = bridge.handle_event(event, monitor)
        assert all(item["created"] is False for item in replayed)
        assert len(goals.list_watcher_signals(watcher_id=monitor.id)) == 2
        assert len([item for item in ledger.list_all() if item["action_type"] == "watcher_goal_signal"]) == 2
        await scheduler.close()
    finally:
        ledger.close()
        goals.close()


@pytest.mark.asyncio
async def test_auto_pause_incident_also_reaches_linked_goal(tmp_path, watcher_db):
    goals = GoalStore(tmp_path / "ares.db")
    try:
        goal = goals.create("Keep production monitoring healthy")
        monitor = Monitor(id="health-watch", name="Health", type="website", interval_seconds=20)
        watcher_db.insert_monitor(monitor)
        goals.link(goal["goal_id"], link_type="watcher", ref_id=monitor.id)
        bridge = GoalWatcherBridge(goals)
        scheduler = WatcherScheduler(
            watcher_db,
            fetchers={"website": SequenceFetcher([FetchResult(False, error="upstream down")])},
            failure_limit=1,
            goal_signal_handler=bridge.handle_event,
        )
        event = await scheduler.check_monitor(monitor, force=True)
        signal = goals.list_watcher_signals(goal["goal_id"])[0]
        assert event.event_type == "monitor_paused"
        assert signal["event_type"] == "monitor_paused"
        assert signal["source_event_id"] == event.id
        assert goals.get(goal["goal_id"])["status"] == "active"
        await scheduler.close()
    finally:
        goals.close()
