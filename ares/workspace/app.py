"""HTTP host for the local Ares workspace SPA."""

from __future__ import annotations

import mimetypes
from pathlib import Path
from typing import Any, Callable

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles


BUNDLED_NEXT_DIR = Path(__file__).parent / "static"
NEXT_WORKSPACE_OUT = Path(__file__).resolve().parents[2] / "ares-workspace" / "out"


def resolve_workspace_static_dir() -> Path:
    """Prefer the production Next.js export, with a source-tree fallback."""
    if (NEXT_WORKSPACE_OUT / "index.html").is_file():
        return NEXT_WORKSPACE_OUT
    return BUNDLED_NEXT_DIR


def create_workspace_app(
    *,
    websocket_host: str = "127.0.0.1",
    websocket_port: int = 8765,
    watcher_dashboard_url: str = "http://127.0.0.1:8080",
    artifact_roots: list[str | Path] | None = None,
    artifact_resolver: Callable[[str], str | Path | None] | None = None,
) -> FastAPI:
    app = FastAPI(title="Ares Workspace", version="1.0.0", docs_url=None, redoc_url=None)
    static_dir = resolve_workspace_static_dir()
    safe_artifact_roots = [
        Path(root).expanduser().resolve()
        for root in (artifact_roots or [Path.cwd(), Path.home() / ".ares"])
    ]

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
