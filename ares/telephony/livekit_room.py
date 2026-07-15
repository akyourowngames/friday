"""Loopback-only Ares LiveKit room launcher with local token issuance.

LiveKit creates a room when its first participant joins. This launcher therefore
does not need a separate room-create API call: it starts a tiny local token
broker, and opens a fresh bundled client session.
"""

from __future__ import annotations

import argparse
import os
import re
import threading
import uuid
import webbrowser
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlencode

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse

from ares.telephony.livekit_token import generate_room_token, resolve_livekit_credentials


DEFAULT_ROOM = "ares-voice-room"
DEFAULT_IDENTITY = "ares-user"
DEFAULT_TTL_SECONDS = 600
_SAFE_PARTICIPANT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}
_CLIENT_PATH = Path(__file__).with_name("livekit_client.html")


def _safe_name(value: str, *, field: str, default: str) -> str:
    normalized = str(value or default).strip()
    if not _SAFE_PARTICIPANT.fullmatch(normalized):
        raise ValueError(f"{field} must use 1-128 letters, numbers, '.', '_', ':', or '-'")
    return normalized


def _safe_ttl(value: int) -> int:
    return max(60, min(int(value), 3_600))


def create_room_session(
    room: str = DEFAULT_ROOM,
    identity: str = DEFAULT_IDENTITY,
    *,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
    config: Any | None = None,
) -> dict[str, Any]:
    """Create a browser-safe session payload without exposing API credentials."""
    safe_room = _safe_name(room, field="room", default=DEFAULT_ROOM)
    safe_identity = _safe_name(identity, field="identity", default=DEFAULT_IDENTITY)
    ttl = _safe_ttl(ttl_seconds)
    credentials = resolve_livekit_credentials(config)
    token = generate_room_token(
        safe_identity,
        safe_room,
        api_key=credentials.api_key,
        api_secret=credentials.api_secret,
        ttl=ttl,
    )
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=ttl)
    return {
        "livekit_url": credentials.url,
        "room": safe_room,
        "identity": safe_identity,
        "token": token,
        "expires_at": expires_at.isoformat(timespec="seconds").replace("+00:00", "Z"),
    }


def create_room_app(
    *,
    config_provider: Callable[[], Any] | None = None,
    default_room: str = DEFAULT_ROOM,
    default_identity: str = DEFAULT_IDENTITY,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
) -> FastAPI:
    """Create the local client/token broker used by ``ares-livekit-room``."""
    if config_provider is None:
        from ares.config import load_config

        config_provider = load_config
    safe_default_room = _safe_name(default_room, field="room", default=DEFAULT_ROOM)
    safe_default_identity = _safe_name(default_identity, field="identity", default=DEFAULT_IDENTITY)
    default_ttl = _safe_ttl(ttl_seconds)
    app = FastAPI(title="Ares LiveKit Room", docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def no_store(request: Request, call_next: Callable[..., Any]) -> Any:
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store, max-age=0"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    @app.get("/")
    async def client() -> FileResponse:
        return FileResponse(_CLIENT_PATH, media_type="text/html")

    @app.get("/api/livekit/session")
    async def session(room: str = safe_default_room, identity: str = safe_default_identity) -> JSONResponse:
        try:
            payload = create_room_session(
                room,
                identity,
                ttl_seconds=default_ttl,
                config=config_provider(),
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return JSONResponse(payload, headers={"Cache-Control": "no-store, max-age=0"})

    return app


def build_room_launch_url(
    *,
    room: str,
    identity: str,
    host: str,
    port: int,
    launch_id: str | None = None,
) -> str:
    """Build a unique URL so the browser cannot reuse a stale connected tab."""
    safe_room = _safe_name(room, field="room", default=DEFAULT_ROOM)
    safe_identity = _safe_name(identity, field="identity", default=DEFAULT_IDENTITY)
    if host not in _LOOPBACK_HOSTS:
        raise ValueError("The Ares LiveKit room launcher only binds to localhost to protect issued tokens.")
    address_host = "127.0.0.1" if host == "localhost" else host
    query = urlencode({
        "room": safe_room,
        "identity": safe_identity,
        # This is a credential-free cache buster. It forces a fresh LiveKit
        # join after the worker has restarted.
        "launch": launch_id or uuid.uuid4().hex,
    })
    return f"http://{address_host}:{int(port)}/?{query}"


def run_room_launcher(
    *,
    room: str = DEFAULT_ROOM,
    identity: str = DEFAULT_IDENTITY,
    host: str = "127.0.0.1",
    port: int = 8790,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
    open_browser: bool = True,
) -> None:
    """Serve the room client locally and optionally open a fresh browser session."""
    if host not in _LOOPBACK_HOSTS:
        raise ValueError("The Ares LiveKit room launcher only binds to localhost to protect issued tokens.")
    safe_room = _safe_name(room, field="room", default=DEFAULT_ROOM)
    safe_identity = _safe_name(identity, field="identity", default=DEFAULT_IDENTITY)
    app = create_room_app(default_room=safe_room, default_identity=safe_identity, ttl_seconds=ttl_seconds)
    launch_url = build_room_launch_url(
        room=safe_room,
        identity=safe_identity,
        host=host,
        port=int(port),
    )
    print(f"Ares LiveKit room launcher: {launch_url}")
    print("The browser will mint a short-lived local token. Click Connect to make a fresh room join. Press Ctrl+C to stop it.")
    if open_browser:
        threading.Timer(0.5, lambda: webbrowser.open(launch_url, new=2)).start()
    uvicorn.run(app, host=host, port=int(port), log_level="warning")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Launch a local Ares LiveKit voice room")
    parser.add_argument("--room", default=os.environ.get("ARES_LIVEKIT_ROOM", DEFAULT_ROOM))
    parser.add_argument("--identity", default=os.environ.get("ARES_LIVEKIT_IDENTITY", DEFAULT_IDENTITY))
    parser.add_argument("--ttl", type=int, default=DEFAULT_TTL_SECONDS, help="Issued browser token lifetime in seconds (60-3600)")
    parser.add_argument("--host", default="127.0.0.1", choices=sorted(_LOOPBACK_HOSTS))
    parser.add_argument("--port", type=int, default=8790)
    parser.add_argument("--no-open", action="store_true", help="Serve the room page without opening a browser")
    args = parser.parse_args(argv)
    run_room_launcher(
        room=args.room,
        identity=args.identity,
        host=args.host,
        port=args.port,
        ttl_seconds=args.ttl,
        open_browser=not args.no_open,
    )


if __name__ == "__main__":
    main()
