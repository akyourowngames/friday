from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from .bus import EventBus
from .configuration import WatcherConfig
from .dashboard import dashboard_html
from .index import FolderIndex
from .llm import FolderWatcherLLM
from .status import load_status
from .understanding import file_understanding
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
    llm_queries_enabled: bool | None = None
    max_content_chars: int | None = None
    hot_file_event_threshold: int | None = None


class SummarizePendingRequest(BaseModel):
    limit: int = 10


class ChatRequest(BaseModel):
    message: str
    history: list[dict[str, Any]] = Field(default_factory=list)
    file_id: str | None = None
    limit: int = 8


class WebhookRequest(BaseModel):
    url: str
    events: list[str] = []
    filter: dict[str, Any] = {}


def create_app(
    config: WatcherConfig,
    index: FolderIndex,
    event_bus: EventBus | None = None,
    llm_service: FolderWatcherLLM | None = None,
) -> FastAPI:
    app = FastAPI(title="KING Folder Watcher Service", version="1.0.0")
    bus = event_bus or EventBus()
    webhook_registry = WebhookRegistry(rate_limit_per_sec=config.webhook_rate_limit_per_sec)
    bus.add_listener(webhook_registry.dispatch)
    llm = llm_service or FolderWatcherLLM(config, index)

    def require_auth(authorization: str | None = Header(default=None)):
        if not config.auth_token:
            return
        expected = "Bearer " + config.auth_token
        if authorization != expected:
            raise HTTPException(status_code=401, detail="auth token required")

    @app.get("/health")
    def health(_: Any = Depends(require_auth)):
        return {"status": "ok", "watch_path": str(config.watch_path), "database_path": str(config.database_path)}

    @app.get("/maintenance/daily/status")
    def maintenance_daily_status(_: Any = Depends(require_auth)):
        from maintenance.engine import build_engine
        from maintenance.steps import register_default_steps

        engine = build_engine(str(config.repo_root))
        register_default_steps(engine)
        return engine.status()

    @app.post("/maintenance/daily/run")
    def maintenance_daily_run(force: bool = False, dry_run: bool = False, _: Any = Depends(require_auth)):
        from maintenance.engine import build_engine
        from maintenance.steps import register_default_steps

        engine = build_engine(str(config.repo_root))
        register_default_steps(engine)
        result = engine.run(triggered_by="folder_watcher_api", dry_run=bool(dry_run), force=bool(force))
        return result.to_dict()

    @app.get("/dashboard", response_class=HTMLResponse)
    def dashboard(_: Any = Depends(require_auth)):
        return dashboard_html(config)

    @app.get("/status")
    def service_status(_: Any = Depends(require_auth)):
        status = load_status(config.repo_root)
        status["runtime"] = {
            "watch_path": str(config.watch_path),
            "database_path": str(config.database_path),
            "fts_enabled": index.fts_enabled,
            "auth_enabled": bool(config.auth_token),
            "llm": llm.status(),
        }
        return status

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

    @app.get("/files/snapshot")
    def files_snapshot(at: float | None = None, _: Any = Depends(require_auth)):
        return index.snapshot(at)

    @app.get("/files/hot")
    def files_hot(limit: int = Query(default=20, ge=1, le=200), _: Any = Depends(require_auth)):
        return {
            "files": index.hot_files(
                threshold=config.hot_file_event_threshold,
                window_seconds=config.hot_file_window_seconds,
                limit=limit,
            )
        }

    @app.get("/files/anomalies")
    def files_anomalies(limit: int = Query(default=100, ge=1, le=500), _: Any = Depends(require_auth)):
        return {"events": index.anomalies(limit)}

    @app.get("/files/search")
    def files_search(q: str, limit: int = Query(default=20, ge=1, le=100), _: Any = Depends(require_auth)):
        return {"query": q, "files": index.search(q, limit)}

    @app.post("/files/query")
    def files_query(request: QueryRequest, _: Any = Depends(require_auth)):
        if not request.query.strip():
            raise HTTPException(status_code=400, detail="query must not be empty")
        if llm.query_available():
            try:
                generated = llm.generate_sql(request.query, request.limit)
                result = index.readonly_query(
                    generated["sql"],
                    llm.policy.allowed_tables,
                    llm.policy.allowed_functions,
                    generated["row_limit"],
                )
            except Exception as exc:
                raise HTTPException(status_code=502, detail=f"LLM query failed: {exc}") from exc
            if result["status"] != "success":
                raise HTTPException(
                    status_code=400,
                    detail={
                        "message": "LLM SQL was blocked by the read-only database guard.",
                        "sql": generated.get("sql", ""),
                        "result": result,
                    },
                )
            return {
                "mode": "llm_sql",
                "provider_sql_generation": "active",
                "query": request.query,
                "sql": generated["sql"],
                "explanation": generated.get("explanation", ""),
                "columns": result["columns"],
                "rows": result["rows"],
                "files": _rows_as_files(result["rows"]),
            }
        files = index.search(request.query, request.limit)
        if not files:
            files = index.latest(request.limit)
        return {
            "mode": "local_fallback",
            "provider_sql_generation": "unavailable",
            "llm": llm.status(),
            "query": request.query,
            "files": files,
        }

    @app.get("/llm/status")
    def llm_status(_: Any = Depends(require_auth)):
        return llm.status()

    @app.post("/chat")
    def chat(request: ChatRequest, _: Any = Depends(require_auth)):
        if not request.message.strip():
            raise HTTPException(status_code=400, detail="message must not be empty")
        if request.file_id and index.get_file(request.file_id) is None:
            raise HTTPException(status_code=404, detail="file not found")
        if llm.chat_available():
            try:
                result = llm.chat(request.message, request.history, request.file_id, request.limit)
            except Exception as exc:
                raise HTTPException(status_code=502, detail=f"LLM chat failed: {exc}") from exc
            return {"mode": "llm_chat", "message": request.message, **result}
        files = index.search(request.message, request.limit)
        if not files:
            files = index.latest(request.limit)
        selected_file = index.get_file(request.file_id) if request.file_id else None
        return {
            "mode": "local_context",
            "message": request.message,
            "answer": _local_chat_answer(files, selected_file, llm.status()),
            "selected_file": selected_file,
            "files": files,
            "llm": llm.status(),
        }

    @app.get("/files/duplicates")
    def files_duplicates(_: Any = Depends(require_auth)):
        return {"groups": index.duplicates()}

    @app.get("/files/duplicates/symlink-suggestions")
    def duplicate_symlink_suggestions(_: Any = Depends(require_auth)):
        return {"suggestions": index.duplicate_symlink_suggestions()}

    @app.get("/files/stats")
    def files_stats(_: Any = Depends(require_auth)):
        return index.stats()

    @app.get("/files/details")
    def files_details(
        limit: int = Query(default=100, ge=1, le=500),
        ext: str | None = None,
        dir: str | None = None,
        include_content: bool = False,
        max_content_chars: int = Query(default=2000, ge=1, le=50000),
        _: Any = Depends(require_auth),
    ):
        files = index.file_details(limit, ext, dir, include_content, max_content_chars)
        return {
            "files": files,
            "count": len(files),
            "filters": {
                "extension": ext,
                "directory": dir,
                "include_content": include_content,
                "max_content_chars": max_content_chars,
            },
        }

    @app.get("/playlist/new-arrivals")
    def playlist_new_arrivals(format: str = "json", _: Any = Depends(require_auth)):
        if format.strip().lower() == "m3u":
            result = index.write_playlist(config.playlist_path)
            if result.get("path"):
                try:
                    return Response(Path(result["path"]).read_text(encoding="utf-8"), media_type="audio/x-mpegurl")
                except OSError:
                    return Response("#EXTM3U\n", media_type="audio/x-mpegurl")
            return Response("#EXTM3U\n", media_type="audio/x-mpegurl")
        return {"files": index.audio_files(), "playlist": index.write_playlist(config.playlist_path)}

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
    def get_file_content(
        file_id: str,
        offset: int = Query(default=0, ge=0),
        max_chars: int = Query(default=0, ge=0, le=200000),
        _: Any = Depends(require_auth),
    ):
        content = index.get_content(file_id)
        if content is None:
            raise HTTPException(status_code=404, detail="file content not found")
        start = min(offset, len(content))
        if max_chars:
            end = min(len(content), start + max_chars)
        else:
            end = len(content)
        next_offset = end if end < len(content) else None
        return {
            "file_id": file_id,
            "content": content[start:end],
            "offset": start,
            "max_chars": max_chars,
            "total_chars": len(content),
            "truncated": next_offset is not None,
            "next_offset": next_offset,
        }

    @app.get("/files/{file_id}/dependencies")
    def get_dependencies(file_id: str, _: Any = Depends(require_auth)):
        if index.get_file(file_id) is None:
            raise HTTPException(status_code=404, detail="file not found")
        return {"file_id": file_id, "dependencies": index.dependencies(file_id)}

    @app.get("/files/{file_id}/dependents")
    def get_dependents(file_id: str, _: Any = Depends(require_auth)):
        if index.get_file(file_id) is None:
            raise HTTPException(status_code=404, detail="file not found")
        return {"file_id": file_id, "dependents": index.dependents(file_id)}

    @app.get("/files/{file_id}/deep-dive")
    def file_deep_dive(file_id: str, _: Any = Depends(require_auth)):
        item = index.get_file(file_id)
        if item is None:
            raise HTTPException(status_code=404, detail="file not found")
        if llm.deep_dive_available():
            try:
                result = llm.deep_dive_file(file_id)
            except Exception as exc:
                raise HTTPException(status_code=502, detail=f"LLM deep dive failed: {exc}") from exc
            return {"mode": "llm_deep_dive", "file_id": file_id, **result}
        return {
            "mode": "local_context",
            "file_id": file_id,
            "answer": _local_deep_dive_answer(item),
            "file": item,
            "understanding": file_understanding(item, index.get_content(file_id) or ""),
            "content_excerpt": (index.get_content(file_id) or "")[:5000],
            "dependencies": index.dependencies(file_id),
            "dependents": index.dependents(file_id),
            "events": [event for event in index.diff(since=0, limit=1000) if event.get("file_id") == file_id][-20:],
            "llm": llm.status(),
        }

    @app.get("/files/{file_id}/summary")
    def get_file_summary(file_id: str, _: Any = Depends(require_auth)):
        item = index.get_file(file_id)
        if item is None:
            raise HTTPException(status_code=404, detail="file not found")
        if item.get("summary"):
            return {"file_id": file_id, "summary": item["summary"], "status": "ready"}
        if llm.summaries_available():
            content = index.get_content(file_id) or ""
            try:
                summary = llm.summarize_file(item, content)
            except Exception as exc:
                raise HTTPException(status_code=502, detail=f"LLM summary failed: {exc}") from exc
            updated = index.update_summary(file_id, summary["summary"], summary["tags"])
            return {
                "file_id": file_id,
                "summary": updated["summary"] if updated else summary["summary"],
                "tags": updated["tags"] if updated else summary["tags"],
                "status": "ready",
                "provider": summary["provider"],
            }
        return Response(
            json.dumps({"file_id": file_id, "summary": "", "status": "pending", "llm": llm.status()}),
            status_code=202,
            media_type="application/json",
        )

    @app.post("/files/summarize-pending")
    def summarize_pending(request: SummarizePendingRequest, _: Any = Depends(require_auth)):
        if not llm.summaries_available():
            raise HTTPException(status_code=503, detail={"message": "LLM summaries are not available.", "llm": llm.status()})
        files = index.pending_summaries(request.limit)
        summarized = []
        failed = []
        for item in files:
            try:
                content = index.get_content(item["id"]) or ""
                summary = llm.summarize_file(item, content)
                updated = index.update_summary(item["id"], summary["summary"], summary["tags"])
                summarized.append(updated or item)
            except Exception as exc:
                failed.append({"file_id": item["id"], "error": str(exc)})
        return {"summarized": summarized, "failed": failed, "count": len(summarized)}

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
        minimum_gap = 1.0 / max(0.1, float(config.subscriber_rate_limit_per_sec or 20.0))
        last_sent = 0.0
        with bus.subscribe() as queue:
            try:
                while True:
                    event = await queue.get()
                    if _event_matches(event, ext_filter, mime_filter):
                        import time

                        now = time.time()
                        if now - last_sent < minimum_gap:
                            continue
                        last_sent = now
                        await websocket.send_json(event)
            except WebSocketDisconnect:
                return

    app.state.folder_watcher_event_bus = bus
    app.state.folder_watcher_webhooks = webhook_registry
    app.state.folder_watcher_llm = llm
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


def _rows_as_files(rows: list[dict]) -> list[dict]:
    files = []
    for row in rows:
        if "id" in row and "path" in row and "filename" in row:
            files.append(row)
    return files


def _local_chat_answer(files: list[dict], selected_file: dict | None, llm_status: dict) -> str:
    if not llm_status.get("provider_ready"):
        state = "unavailable"
    elif not llm_status.get("chat_enabled", True):
        state = "disabled by policy"
    else:
        state = "not active for this request"
    if selected_file:
        return (
            "LLM chat is " + state + ". Selected file: "
            + str(selected_file.get("filename", "unknown"))
            + ". Matching indexed files: "
            + str(len(files))
            + "."
        )
    return "LLM chat is " + state + ". Matching indexed files: " + str(len(files)) + "."


def _local_deep_dive_answer(file_record: dict) -> str:
    return (
        str(file_record.get("filename", "unknown"))
        + " is indexed as "
        + str(file_record.get("mime_type", "unknown"))
        + " with "
        + str(len(file_record.get("tags", [])))
        + " tags."
    )
