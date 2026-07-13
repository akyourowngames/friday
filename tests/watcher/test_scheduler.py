from datetime import datetime, timedelta

import pytest

from ares.watcher.fetchers.base import BaseFetcher, FetchResult
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
