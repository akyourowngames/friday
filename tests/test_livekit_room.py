"""Focused tests for the auto-joining local LiveKit room flow."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from ares.telephony.livekit_room import (
    build_room_launch_url,
    create_room_app,
    create_room_session,
    run_room_launcher,
)
from ares.telephony.livekit_token import main as token_main


def _config() -> SimpleNamespace:
    return SimpleNamespace(telephony=SimpleNamespace(
        livekit_url="wss://voice.example.livekit.cloud",
        livekit_api_key="test-api-key",
        livekit_api_secret="test-api-secret-must-be-at-least-32-characters-long!!",
    ))


def test_room_session_mints_short_lived_token_without_exposing_credentials():
    session = create_room_session("ares-demo", "krish", ttl_seconds=600, config=_config())

    assert session["livekit_url"] == "wss://voice.example.livekit.cloud"
    assert session["room"] == "ares-demo"
    assert session["identity"] == "krish"
    assert session["token"].count(".") == 2
    assert "api_secret" not in session
    assert "test-api-secret" not in json.dumps(session)


def test_room_session_rejects_unsafe_room_or_identity():
    with pytest.raises(ValueError, match="room must use"):
        create_room_session("room with spaces", "krish", config=_config())
    with pytest.raises(ValueError, match="identity must use"):
        create_room_session("ares-room", "krish@example.com", config=_config())


def test_room_app_issues_no_store_session_payload():
    app = create_room_app(config_provider=_config, default_room="ares-demo", default_identity="krish")
    route = next(route for route in app.routes if getattr(route, "path", "") == "/api/livekit/session")
    response = asyncio.run(route.endpoint(room="ares-demo", identity="krish"))
    payload = json.loads(response.body)

    assert response.headers["cache-control"] == "no-store, max-age=0"
    assert payload["token"].count(".") == 2
    assert payload["room"] == "ares-demo"


def test_launcher_refuses_network_bind_before_starting_server():
    with pytest.raises(ValueError, match="only binds to localhost"):
        run_room_launcher(host="0.0.0.0", open_browser=False)


def test_launcher_url_forces_a_fresh_credential_free_browser_load():
    url = build_room_launch_url(
        room="ares-demo",
        identity="krish",
        host="127.0.0.1",
        port=8790,
        launch_id="fresh-session",
    )

    assert url == "http://127.0.0.1:8790/?room=ares-demo&identity=krish&launch=fresh-session"
    assert "token" not in url


def test_client_uses_local_token_endpoint_and_not_a_hardcoded_project():
    client = Path("ares/telephony/livekit_client.html").read_text(encoding="utf-8")

    assert "/api/livekit/session" in client
    assert "session.token" in client
    assert "tokenInput" not in client
    assert "ankita-t14wmh66.livekit.cloud" not in client


def test_client_requires_user_gesture_and_reports_audio_playback_state():
    """Remote audio must be unlocked from the Connect button, not page load."""
    client = Path("ares/telephony/livekit_client.html").read_text(encoding="utf-8")

    assert 'onclick="joinRoom()"' in client
    assert "room.startAudio" in client
    assert "audioEl.play()" in client
    assert 'id="enableAudioBtn"' in client
    assert "DOMContentLoaded', () => joinRoom()" not in client


def test_token_cli_prints_only_jwt(monkeypatch, capsys):
    monkeypatch.setenv("LIVEKIT_URL", "wss://voice.example.livekit.cloud")
    monkeypatch.setenv("LIVEKIT_API_KEY", "test-api-key")
    monkeypatch.setenv("LIVEKIT_API_SECRET", "test-api-secret-must-be-at-least-32-characters-long!!")

    token_main(["--room", "ares-demo", "--identity", "krish"])
    output = capsys.readouterr().out.strip()

    assert output.count(".") == 2
    assert "secret" not in output
