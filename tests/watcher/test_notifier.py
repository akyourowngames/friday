import pytest

from ares.watcher.ai_analyzer import AIAnalyzer
from ares.watcher.models import Event, Monitor
from ares.watcher.notifier import NotificationDispatcher, format_alert


class FakeDispatcher(NotificationDispatcher):
    async def _send_webhook(self,*args): return None
    async def _send_email(self,*args): raise RuntimeError("smtp offline")


class FakeLLM:
    async def chat(self,messages,**kwargs):
        assert "untrusted data" in messages[0]["content"]
        return {"content":"This is material; review before acting."}


def test_alert_format_includes_context():
    monitor=Monitor(id="m",name="Price",type="website")
    event=Event(id="e",monitor_id="m",event_type="price_change",old_value="100",new_value="80",severity="warning")
    text=format_alert(event,monitor)
    assert "Price" in text and "Previous: 100" in text and "Current: 80" in text


@pytest.mark.asyncio
async def test_dispatcher_audits_success_and_failure(watcher_db):
    monitor=Monitor(id="m",name="M",type="website",config={"notifications":{"webhook":{"enabled":True},"email":{"enabled":True}}})
    watcher_db.insert_monitor(monitor); event=Event(id="e",monitor_id="m",event_type="content_change"); watcher_db.insert_event(event)
    records=await FakeDispatcher(watcher_db).dispatch(event,monitor)
    assert {item.status for item in records}=={"sent","failed"}
    assert watcher_db.get_event("e").notified is True
    failed=[item for item in watcher_db.list_notifications() if item.status=="failed"][0]
    assert failed.next_retry_at is not None and "smtp offline" in failed.error


@pytest.mark.asyncio
async def test_failed_delivery_is_retried_in_place(watcher_db):
    monitor=Monitor(id="m",name="M",type="website",config={"notifications":{"webhook":{"enabled":True}}})
    watcher_db.insert_monitor(monitor); event=Event(id="e",monitor_id="m",event_type="content_change"); watcher_db.insert_event(event)
    record=(await FakeDispatcher(watcher_db).dispatch(event,monitor))[0]
    record.status="failed"; record.sent_at=None; record.next_retry_at=None; watcher_db.update_notification(record)
    retried=await FakeDispatcher(watcher_db).retry_failed()
    assert retried[0].id==record.id and retried[0].status=="sent" and retried[0].attempts==2


@pytest.mark.asyncio
async def test_no_channels_marks_event_processed(watcher_db):
    monitor=Monitor(id="m",name="M",type="website"); watcher_db.insert_monitor(monitor)
    event=Event(id="e",monitor_id="m",event_type="content_change"); watcher_db.insert_event(event)
    assert await NotificationDispatcher(watcher_db).dispatch(event,monitor)==[]
    assert watcher_db.get_event("e").notified is True


@pytest.mark.asyncio
async def test_ai_analysis_only_for_smart_modes():
    event=Event(id="e",monitor_id="m",event_type="content_change",old_value="a",new_value="b")
    assert await AIAnalyzer(FakeLLM()).analyze(event,Monitor(id="m",name="M",type="website")) is None
    result=await AIAnalyzer(FakeLLM()).analyze(event,Monitor(id="m",name="M",type="website",ai_action="suggest"))
    assert "review" in result
