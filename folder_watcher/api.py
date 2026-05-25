from __future__ import annotations

import json
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Response, WebSocket, WebSocketDisconnect
from pydantic import BaseModel

from .bus import EventBus
from .configuration import WatcherConfig
from .index import FolderIndex
from .webhooks import WebhookRegistry


class QueryRequest(BaseModel):
    query: str
    limit: int = 20


class TagRequest(BaseModel):
    tag: str


class ConfigPatchRequest(BaseModel):
    ignore_globs: list[str] | None = None
    debounce_ms: int | None = None
    ai_summaries_enabled: bool | None = None
    max_content_chars: int | None = None


class WebhookRequest(BaseModel):
    url: str
    events: list[str] = []
    filter: dict[str, Any] = {}


def create_app(config: WatcherConfig, index: FolderIndex, event_bus: EventBus | None = None) -> FastAPI:
    app = FastAPI(title="KING Folder Watcher Service", version="1.0.0")
    bus = event_bus or EventBus()
    webhook_registry = WebhookRegistry()
    bus.add_listener(webhook_registry.dispatch)

    def require_auth(authorization: str | None = Header(default=None)):
        if not config.auth_token:
            return
        expected = "Bearer " + config.auth_token
        if authorization != expected:
            raise HTTPException(status_code=401, detail="auth token required")

    @app.get("/health")
    def health(_: Any = Depends(require_auth)):
        return {"status": "ok", "watch_path": str(config.watch_path), "database_path": str(config.database_path)}

    @app.get("/files/latest")
    def files_latest(
        n: int = Query(default=10, ge=1, le=500),
        ext: str | None = None,
        since: float | None = None,
        dir: str | None = None,
        _: Any = Depends(require_auth),
    ):
        return {"files": index.latest(n, ext, since, dir)}

    @app.get("/files/diff")
    def files_diff(
        since: float | None = None,
        from_ts: float | None = None,
        to_ts: float | None = None,
        limit: int = Query(default=200, ge=1, le=1000),
        _: Any = Depends(require_auth),
    ):
        return {"events": index.diff(since=since, from_ts=from_ts, to_ts=to_ts, limit=limit)}

    @app.get("/files/search")
    def files_search(q: str, limit: int = Query(default=20, ge=1, le=100), _: Any = Depends(require_auth)):
        return {"query": q, "files": index.search(q, limit)}

    @app.post("/files/query")
    def files_query(request: QueryRequest, _: Any = Depends(require_auth)):
        if not request.query.strip():
            raise HTTPException(status_code=400, detail="query must not be empty")
        files = index.search(request.query, request.limit)
        if not files:
            files = index.latest(request.limit)
        return {
            "mode": "local_index_resolution",
            "provider_sql_generation": "not_configured",
            "query": request.query,
            "files": files,
        }

    @app.get("/files/duplicates")
    def files_duplicates(_: Any = Depends(require_auth)):
        return {"groups": index.duplicates()}

    @app.get("/files/stats")
    def files_stats(_: Any = Depends(require_auth)):
        return index.stats()

    @app.post("/webhooks")
    def register_webhook(request: WebhookRequest, _: Any = Depends(require_auth)):
        try:
            hook = webhook_registry.register(request.url, request.events, request.filter)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return hook

    @app.get("/webhooks")
    def list_webhooks(_: Any = Depends(require_auth)):
        return {"webhooks": webhook_registry.list_hooks()}

    @app.get("/config")
    def get_config(_: Any = Depends(require_auth)):
        return config.public_dict()

    @app.patch("/config")
    def patch_config(request: ConfigPatchRequest, _: Any = Depends(require_auth)):
        if hasattr(request, "model_dump"):
            payload = request.model_dump(exclude_none=True)
        else:
            payload = request.dict(exclude_none=True)
        changed = config.apply_runtime_patch(payload)
        return {"changed": changed, "config": config.public_dict()}

    @app.get("/export")
    def export(format: str = "json", _: Any = Depends(require_auth)):
        clean = format.strip().lower()
        if clean == "json":
            return index.export_json()
        if clean == "csv":
            return Response(index.export_csv(), media_type="text/csv")
        raise HTTPException(status_code=400, detail="format must be json or csv")

    @app.get("/files/{file_id}")
    def get_file(file_id: str, _: Any = Depends(require_auth)):
        item = index.get_file(file_id)
        if item is None:
            raise HTTPException(status_code=404, detail="file not found")
        return item

    @app.get("/files/{file_id}/content")
    def get_file_content(file_id: str, _: Any = Depends(require_auth)):
        content = index.get_content(file_id)
        if content is None:
            raise HTTPException(status_code=404, detail="file content not found")
        return {"file_id": file_id, "content": content}

    @app.get("/files/{file_id}/summary")
    def get_file_summary(file_id: str, _: Any = Depends(require_auth)):
        item = index.get_file(file_id)
        if item is None:
            raise HTTPException(status_code=404, detail="file not found")
        if item.get("summary"):
            return {"file_id": file_id, "summary": item["summary"], "status": "ready"}
        return Response(
            json.dumps({"file_id": file_id, "summary": "", "status": "pending", "ai_summaries_enabled": config.ai_summaries_enabled}),
            status_code=202,
            media_type="application/json",
        )

    @app.delete("/files/{file_id}")
    def delete_file(file_id: str, _: Any = Depends(require_auth)):
        deleted = index.delete_file_record(file_id)
        if not deleted:
            raise HTTPException(status_code=404, detail="file not found")
        return {"deleted": True, "file_id": file_id}

    @app.post("/files/{file_id}/tags")
    def add_tag(file_id: str, request: TagRequest, _: Any = Depends(require_auth)):
        item = index.add_user_tag(file_id, request.tag)
        if item is None:
            raise HTTPException(status_code=404, detail="file not found or tag empty")
        return item

    @app.websocket("/watch")
    async def watch(websocket: WebSocket):
        if config.auth_token:
            token = websocket.headers.get("authorization")
            if token != "Bearer " + config.auth_token:
                await websocket.close(code=1008)
                return
        await websocket.accept()
        ext_filter = websocket.query_params.get("ext")
        mime_filter = websocket.query_params.get("mime")
        with bus.subscribe() as queue:
            try:
                while True:
                    event = await queue.get()
                    if _event_matches(event, ext_filter, mime_filter):
                        await websocket.send_json(event)
            except WebSocketDisconnect:
                return

    app.state.folder_watcher_event_bus = bus
    app.state.folder_watcher_webhooks = webhook_registry
    return app


def _event_matches(event: dict, ext_filter: str | None, mime_filter: str | None) -> bool:
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    file_info = payload.get("file") if isinstance(payload.get("file"), dict) else {}
    if ext_filter:
        wanted = ext_filter.strip().lower()
        if wanted and not wanted.startswith("."):
            wanted = "." + wanted
        if str(file_info.get("extension") or "").lower() != wanted:
            return False
    if mime_filter:
        wanted_mime = mime_filter.strip()
        if wanted_mime.endswith("*"):
            prefix = wanted_mime[:-1]
            if not str(file_info.get("mime_type") or "").startswith(prefix):
                return False
        elif str(file_info.get("mime_type") or "") != wanted_mime:
            return False
    return True
