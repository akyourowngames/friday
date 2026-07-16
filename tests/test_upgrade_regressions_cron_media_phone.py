"""Cron, media, and phone fault-injection regressions."""
from __future__ import annotations

import asyncio
import io
import json
import subprocess
from pathlib import Path

import pytest
from PIL import Image

from ares.cron.schedule_utils import simulate_next_runs
from ares.cron.store import CronConflictError, CronStore
from ares.cron.tools import CronToolHandlers
from ares.tools import adb_bridge, kdeconnect_bridge
from ares.tools import image_generate
from ares.tools.image_edit import convert_image, crop_image, resize_image


def test_cron_revisions_leases_and_tracked_manual_lifecycle(tmp_path):
    store = CronStore(tmp_path / "ares")
    job = store.create_job("Audit", "do work", "* * * * *")
    store.update_job(job["id"], prompt="updated", expected_revision=job["revision"])
    with pytest.raises(CronConflictError):
        store.update_job(job["id"], prompt="stale", expected_revision=job["revision"])

    store.claim_job(job["id"], lease_seconds=1)
    store.update_job(job["id"], lease_expires_at="2000-01-01T00:00:00Z")
    recovered = store.get_job(job["id"])
    assert recovered["state"] == "failed"
    assert recovered["last_log_path"] and Path(recovered["last_log_path"]).exists()

    class Runner:
        def __init__(self, store):
            self.store = store

        async def run_job(self, job_id, *, lease_id=None):
            await asyncio.sleep(0)
            log = self.store.log_dir(job_id) / "manual.md"
            log.write_text("manual", encoding="utf-8")
            self.store.complete_job(job_id, lease_id, status="completed", log_path=log)
            return log

    handlers = CronToolHandlers(store, runner_factory=lambda *, store: Runner(store))

    async def trigger_twice():
        first = handlers.run_cron_job_now({"job_id": job["id"]})
        second = handlers.run_cron_job_now({"job_id": job["id"]})
        await asyncio.gather(*handlers._run_tasks.values())
        return first, second

    first, second = asyncio.run(trigger_twice())
    assert "tracked task" in first
    assert "already running" in second
    terminal = store.get_job(job["id"])
    assert terminal["state"] == "completed"
    # One earlier recovered lease plus exactly one accepted manual run.
    assert terminal["run_count"] == 2
    with pytest.raises(ValueError):
        handlers.run_cron_job_now({"job_id": "missing"})


def test_cron_missed_run_cap_is_labeled():
    report = simulate_next_runs(
        "* * * * *",
        "UTC",
        base=None,
        last_run_at="2000-01-01T00:00:00Z",
    )
    assert report["missed_runs"] == 100
    assert report["missed_runs_truncated"] is True
    assert report["missed_runs_lower_bound"] == 100
    assert "capped" in report["missed_run_explanation"]


def _image_bytes() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (4, 4), "green").save(buffer, format="PNG")
    return buffer.getvalue()


def test_media_atomic_animation_crop_and_generation_identity(tmp_path, monkeypatch):
    source = tmp_path / "animated.gif"
    red, blue = Image.new("RGB", (20, 10), "red"), Image.new("RGB", (20, 10), "blue")
    red.save(source, save_all=True, append_images=[blue], duration=[90, 180], loop=0)
    red.close()
    blue.close()
    resized = tmp_path / "resized.gif"
    assert "Resized" in resize_image(str(source), width=10, output=str(resized))
    with Image.open(resized) as image:
        assert image.n_frames == 2
        assert image.size == (10, 5)
    assert "quality must be" in convert_image(str(source), "webp", output=str(tmp_path / "bad.webp"), quality=101)
    converted = tmp_path / "converted.webp"
    assert "Converted" in convert_image(str(source), "webp", output=str(converted))
    with Image.open(converted) as image:
        assert image.n_frames == 2
    before = source.read_bytes()
    outside = crop_image(str(source), left=100, top=100, right=200, bottom=200)
    assert "no overlap" in outside.lower()
    assert source.read_bytes() == before

    monkeypatch.setattr(image_generate, "IMAGES_DIR", tmp_path / "generated")

    class Client:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get(self, *args, **kwargs):
            return type("Response", (), {"headers": {"content-type": "image/png"}, "content": _image_bytes(), "raise_for_status": lambda self: None})()

    monkeypatch.setattr(image_generate.httpx, "Client", Client)
    one = image_generate.generate_image("same", seed=1)
    two = image_generate.generate_image("same", seed=2)
    assert one != two

    class CorruptClient(Client):
        def get(self, *args, **kwargs):
            return type("Response", (), {"headers": {"content-type": "image/png"}, "content": b"not an image", "raise_for_status": lambda self: None})()

    monkeypatch.setattr(image_generate.httpx, "Client", CorruptClient)
    assert "not a valid image" in image_generate.generate_image("bad")
    monkeypatch.setattr(image_generate.httpx, "Client", Client)
    monkeypatch.setattr(image_generate, "record_asset", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("manifest unavailable")))
    warning = image_generate.generate_image("manifest warning", seed=3)
    assert "Image saved to" in warning and "Warning:" in warning


def test_phone_live_capabilities_and_bounded_unicode_notification_errors(monkeypatch):
    output = "- Pixel: abcdef123 (paired and reachable)\n"
    monkeypatch.setattr(kdeconnect_bridge, "_missing_cli", lambda: None)
    monkeypatch.setattr(kdeconnect_bridge, "_device_config_id", lambda: "")
    monkeypatch.setattr(kdeconnect_bridge, "_run_result", lambda *args, **kwargs: (subprocess.CompletedProcess(args[0], 0, output, ""), ""))
    live = kdeconnect_bridge.status()
    assert live["ok"] is True and live["reachable"] is True
    parsed = kdeconnect_bridge._notification_records("Mail: Hello - First line\n  second ✓ line\n\nChat: Ping - hi\n", 1)
    assert len(parsed) == 1 and "second ✓ line" in parsed[0]["text"]
    monkeypatch.setattr(kdeconnect_bridge, "_device_args", lambda: ["--device", "abcdef123"])
    monkeypatch.setattr(kdeconnect_bridge, "_run_result", lambda *args, **kwargs: (None, "timed out"))
    failed = json.loads(kdeconnect_bridge.get_recent_notifications(limit=999))
    assert failed["ok"] is False and failed["limit"] == 100 and "timed out" in failed["error"]

    monkeypatch.setattr(adb_bridge.kdeconnect_bridge, "status", lambda: {"ok": False, "reachable": False, "paired": False, "error": "offline"})
    monkeypatch.setattr(adb_bridge, "_adb", lambda: "adb")
    monkeypatch.setattr(adb_bridge, "connected_devices", lambda: ["device"])
    monkeypatch.setattr(adb_bridge, "_run", lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0, "level: 90\n", ""))
    status = json.loads(adb_bridge.phone_status())
    assert status["ok"] is True
    assert status["any_ready"] is True and status["fully_ready"] is False
    assert status["capability_matrix"]["battery"] is True
    assert status["capability_matrix"]["contacts"] is False
