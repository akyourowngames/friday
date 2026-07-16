"""HTTP host for the local Ares workspace SPA."""

from __future__ import annotations

import ipaddress
import mimetypes
import uuid
from pathlib import Path
from typing import Any, Callable

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field


BUNDLED_NEXT_DIR = Path(__file__).parent / "static"
NEXT_WORKSPACE_OUT = Path(__file__).resolve().parents[2] / "ares-workspace" / "out"


class SpeechPayload(BaseModel):
    """Bounded text accepted by the local Edge speech endpoint."""

    text: str = Field(min_length=1, max_length=8_000)


def resolve_workspace_static_dir() -> Path:
    """Prefer the production Next.js export, with a source-tree fallback."""
    if (NEXT_WORKSPACE_OUT / "index.html").is_file():
        return NEXT_WORKSPACE_OUT
    return BUNDLED_NEXT_DIR


def _is_loopback_client(request: Request) -> bool:
    """Only issue a browser voice token to a local workspace client."""
    host = request.client.host if request.client else ""
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def create_workspace_app(
    *,
    websocket_host: str = "127.0.0.1",
    websocket_port: int = 8765,
    watcher_dashboard_url: str = "http://127.0.0.1:8080",
    artifact_roots: list[str | Path] | None = None,
    voice_config_provider: Callable[[], Any] | None = None,
    artifact_resolver: Callable[[str], str | Path | None] | None = None,
    vision_service: Any | None = None,
) -> FastAPI:
    app = FastAPI(title="Ares Workspace", version="1.0.0", docs_url=None, redoc_url=None)
    if vision_service is not None:
        # Keep image pixels inside VisionService.  The router only presents
        # structured observations/events to the local workspace.
        from ares.vision.api import create_vision_router

        app.include_router(create_vision_router(vision_service))
    static_dir = resolve_workspace_static_dir()
    safe_artifact_roots = [
        Path(root).expanduser().resolve()
        for root in (artifact_roots or [Path.cwd(), Path.home() / ".ares"])
    ]
    if voice_config_provider is None:
        from ares.config import load_config

        voice_config_provider = load_config

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        # PDFs are displayed only by the same-origin workspace artifact panel;
        # denying all frames here makes Chrome render a blank PDF canvas.
        response.headers["X-Frame-Options"] = "SAMEORIGIN" if request.url.path == "/api/artifact" else "DENY"
        response.headers["Cache-Control"] = "no-store" if request.url.path.startswith("/api/") else "no-cache"
        return response

    @app.get("/", include_in_schema=False)
    async def index():
        return FileResponse(static_dir / "index.html")

    @app.get("/api/runtime")
    async def runtime(request: Request) -> dict[str, Any]:
        host = websocket_host
        if host in {"0.0.0.0", "::", "localhost"}:
            host = request.url.hostname or "127.0.0.1"
        return {
            "websocket_url": f"ws://{host}:{websocket_port}",
            "watcher_dashboard_url": watcher_dashboard_url,
            "workspace": "ares",
        }

    @app.get("/api/health")
    async def health():
        return {
            "status": "operational",
            "surface": "workspace",
            "frontend": "nextjs",
        }

    @app.get("/api/voice/session", include_in_schema=False)
    async def voice_session(request: Request) -> JSONResponse:
        """Mint one short-lived LiveKit token for the local workspace voice panel.

        The API secret remains entirely in Ares' local configuration. The JWT
        is never written to a URL and cannot be requested over a network bind.
        """
        if not _is_loopback_client(request):
            raise HTTPException(status_code=403, detail="Voice sessions are available only from the local Ares workspace.")
        try:
            from ares.telephony.livekit_room import DEFAULT_ROOM, create_room_session

            payload = create_room_session(
                DEFAULT_ROOM,
                f"workspace-{uuid.uuid4().hex[:20]}",
                ttl_seconds=600,
                config=voice_config_provider(),
            )
        except Exception as exc:
            raise HTTPException(status_code=503, detail="Ares voice is not configured or unavailable.") from exc
        return JSONResponse(payload, headers={"Cache-Control": "no-store, max-age=0"})

    @app.post("/api/speech", include_in_schema=False)
    async def speech(payload: SpeechPayload) -> Response:
        """Synthesize a voice-turn reply with the configured Edge voice.

        This endpoint is deliberately local to the workspace. Browser
        transcription never sends microphone audio here; only the completed
        Ares response is provided as text after a user initiated voice turn.
        """
        spoken_text = " ".join(payload.text.split())
        if not spoken_text:
            raise HTTPException(status_code=422, detail="Speech text is empty")
        try:
            from ares.config import load_config
            from ares.voice.tts import EdgeTTS

            config = load_config()
            audio = await EdgeTTS(config.voice.tts_voice).synthesize(spoken_text)
        except Exception as exc:
            raise HTTPException(status_code=502, detail="Edge TTS is unavailable") from exc
        if not audio:
            raise HTTPException(status_code=502, detail="Edge TTS returned no audio")
        return Response(content=audio, media_type="audio/mpeg", headers={"Cache-Control": "no-store"})

    @app.get("/api/artifact", include_in_schema=False)
    async def artifact(token: str) -> FileResponse:
        """Serve an artifact only through an opaque, short-lived capability."""
        authorized = artifact_resolver(token) if artifact_resolver is not None else None
        if not authorized:
            raise HTTPException(status_code=404, detail="Artifact preview token is invalid or expired")
        try:
            resolved = Path(authorized).expanduser().resolve(strict=True)
        except (OSError, RuntimeError):
            raise HTTPException(status_code=404, detail="Artifact does not exist") from None
        if (
            not resolved.is_file()
            or not any(resolved.is_relative_to(root) for root in safe_artifact_roots)
        ):
            raise HTTPException(status_code=404, detail="Artifact is outside the Ares workspace")
        if resolved.stat().st_size > 25 * 1024 * 1024:
            raise HTTPException(status_code=413, detail="Artifact is larger than the 25 MB preview limit")
        mime = mimetypes.guess_type(resolved.name)[0] or "application/octet-stream"
        return FileResponse(
            resolved,
            media_type=mime,
            filename=resolved.name,
            content_disposition_type="inline",
        )

    # Mounted last so the runtime APIs above remain authoritative while the
    # exported Next app owns all browser routes and hashed assets.
    app.mount("/", StaticFiles(directory=static_dir, html=True), name="workspace-next")
    return app


__all__ = ["create_workspace_app", "resolve_workspace_static_dir"]
