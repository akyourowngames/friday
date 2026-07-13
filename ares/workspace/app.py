"""HTTP host for the local Ares workspace SPA."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
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
) -> FastAPI:
    app = FastAPI(title="Ares Workspace", version="1.0.0", docs_url=None, redoc_url=None)
    static_dir = resolve_workspace_static_dir()

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "DENY"
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

    # Mounted last so the runtime APIs above remain authoritative while the
    # exported Next app owns all browser routes and hashed assets.
    app.mount("/", StaticFiles(directory=static_dir, html=True), name="workspace-next")
    return app


__all__ = ["create_workspace_app", "resolve_workspace_static_dir"]
