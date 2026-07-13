from datetime import datetime, timezone

import pytest

from ares.watcher.models import Event, Monitor, Snapshot, redact_secrets, redact_url


def test_monitor_creation_and_defaults():
    monitor = Monitor(id="m1", name=" API Health ", type="WEBSITE", url="https://example.com")
    assert monitor.name == "API Health"
    assert monitor.type == "website"
    assert monitor.interval_seconds == 900
    assert monitor.ai_action == "notify"
    assert monitor.enabled is True
    assert monitor.error_count == 0


@pytest.mark.parametrize("monitor_type", ["website", "custom", "instagram", "browser", "tool"])
def test_monitor_supports_every_ares_signal_source(monitor_type):
    assert Monitor(id="m", name="Signal", type=monitor_type).type == monitor_type


def test_monitor_to_dict_serializes_datetimes():
    monitor = Monitor(id="m1", name="Test", type="custom", created_at=datetime(2026, 1, 1, tzinfo=timezone.utc))
    data = monitor.to_dict()
    assert data["created_at"].startswith("2026-01-01T")
    assert data["config"] == {}


@pytest.mark.parametrize("kwargs", [
    {"type":"unknown"}, {"interval_seconds":19}, {"ai_action":"execute-everything"}, {"name":"  "},
])
def test_monitor_rejects_invalid_configuration(kwargs):
    base = {"id":"m1","name":"Test","type":"website"}
    with pytest.raises(ValueError): Monitor(**{**base, **kwargs})


def test_public_monitor_redacts_nested_secrets_and_url_query():
    monitor = Monitor(id="m1",name="Secret",type="custom",url="https://example.com/x?token=abc&view=full",
        config={"headers":{"Authorization":"Bearer secret"},"access_token":"abc","safe":"yes"})
    public = monitor.public_dict()
    assert "abc" not in str(public)
    assert "Bearer secret" not in str(public)
    assert public["config"]["safe"] == "yes"
    assert "view=full" in public["url"]


def test_secret_helpers_cover_nested_values():
    assert redact_secrets({"api_keys":{"one":"secret"}})["api_keys"] == "***REDACTED***"
    assert "password=%2A%2A%2AREDACTED%2A%2A%2A" in redact_url("https://x.test/?password=p")


def test_other_models_serialize():
    snapshot = Snapshot(id="s", monitor_id="m", metadata={"x":1})
    event = Event(id="e", monitor_id="m", event_type="content_change")
    assert snapshot.to_dict()["metadata"] == {"x":1}
    assert event.to_dict()["acknowledged"] is False
