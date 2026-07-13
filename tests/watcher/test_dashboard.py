import httpx
import pytest

from ares.watcher.dashboard.app import create_app
from ares.watcher.service import WatcherService


@pytest.fixture
def dashboard(tmp_path):
    service=WatcherService(tmp_path/"dashboard.db",notification_settings={})
    app=create_app(service=service,start_scheduler=False)
    yield app,service
    service.db.close()


@pytest.mark.asyncio
async def test_dashboard_crud_overview_and_controls(dashboard):
    app,service=dashboard
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url="http://test") as client:
        root=await client.get("/"); assert root.status_code==200 and "WATCHER <em>COMMAND" in root.text
        created=await client.post("/api/monitors",json={"name":"Health","type":"website","url":"https://example.com","interval_seconds":60,"config":{"access_token":"secret"}})
        assert created.status_code==201 and "secret" not in created.text
        identifier=created.json()["id"]
        listing=await client.get("/api/monitors"); assert len(listing.json())==1
        paused=await client.post(f"/api/monitors/{identifier}/pause"); assert paused.json()["enabled"] is False
        resumed=await client.post(f"/api/monitors/{identifier}/resume"); assert resumed.json()["enabled"] is True
        updated=await client.patch(f"/api/monitors/{identifier}",json={"interval_seconds":120}); assert updated.json()["interval_seconds"]==120
        detail=await client.get(f"/api/monitors/{identifier}"); assert detail.json()["monitor"]["name"]=="Health"
        overview=await client.get("/api/overview"); assert overview.json()["monitors"]==1
        capabilities=await client.get("/api/capabilities"); assert {"browser","tool"}.issubset(capabilities.json()["monitor_types"])
        removed=await client.delete(f"/api/monitors/{identifier}"); assert removed.status_code==204


@pytest.mark.asyncio
async def test_dashboard_creates_authenticated_browser_watcher(dashboard):
    app,_service=dashboard
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url="http://test") as client:
        response=await client.post("/api/monitors",json={"name":"DM inbox","type":"browser","url":"https://www.instagram.com/direct/inbox/","interval_seconds":60,"config":{"preset":"instagram_dm"}})
        assert response.status_code==201
        assert response.json()["type"]=="browser"


@pytest.mark.asyncio
async def test_dashboard_validation_and_auth(tmp_path):
    service=WatcherService(tmp_path/"auth.db")
    app=create_app(service=service,start_scheduler=False,api_token="top-secret")
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url="http://test") as client:
            assert (await client.get("/api/overview")).status_code==401
            assert (await client.get("/api/overview",headers={"X-Ares-Token":"top-secret"})).status_code==200
            invalid=await client.post("/api/monitors",headers={"X-Ares-Token":"top-secret"},json={"name":"X","type":"website","interval_seconds":1})
            assert invalid.status_code==422
    finally: service.db.close()


@pytest.mark.asyncio
async def test_dashboard_event_acknowledgement(dashboard):
    app,service=dashboard
    from ares.watcher.models import Event,Monitor
    service.db.insert_monitor(Monitor(id="m",name="M",type="website")); service.db.insert_event(Event(id="e",monitor_id="m",event_type="content_change"))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url="http://test") as client:
        assert len((await client.get("/api/events?unacknowledged=true")).json())==1
        assert (await client.post("/api/events/e/acknowledge")).json()["acknowledged"] is True
        assert (await client.get("/api/events?unacknowledged=true")).json()==[]


@pytest.mark.asyncio
async def test_dashboard_settings_preserve_redacted_secrets(tmp_path):
    saved=[]
    service=WatcherService(tmp_path/"settings.db",notification_settings={"telegram":{"enabled":True,"bot_token":"real-secret","chat_id":"1"}})
    app=create_app(service=service,start_scheduler=False,settings_saver=saved.append)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url="http://test") as client:
            settings=(await client.get("/api/settings")).json()
            assert "real-secret" not in str(settings)
            response=await client.patch("/api/settings",json={"notifications":{"telegram":{"enabled":False,"bot_token":"***REDACTED***","chat_id":"2"}}})
            assert response.status_code==200
            assert service.notifier.settings["telegram"]["bot_token"]=="real-secret"
            assert saved[-1]["telegram"]["chat_id"]=="2"
    finally: service.db.close()
