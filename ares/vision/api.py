"""Local HTTP and WebSocket routes for structured Ares Vision state.

The router intentionally exposes scene metadata and events only.  It never
serialises image pixels, camera frames, screenshots, or frame artifact paths.
The enclosing workspace is normally loopback-bound; the routes additionally
reject non-loopback callers so enabling a local source cannot become a remote
camera API by accident.
"""

from __future__ import annotations

import ipaddress
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field

from ares.vision.models import VisionSourceType, VisualEvent, visual_event_public_dict
from ares.vision.service import VisionService


class SourcePayload(BaseModel):
    source_id: str = Field(min_length=1, max_length=200)
    source_type: VisionSourceType = VisionSourceType.IMAGE
    name: str | None = Field(default=None, max_length=300)
    config: dict[str, Any] = Field(default_factory=dict)
    grant_observe: bool | None = None
    grant_remember: bool | None = None


class ObservePayload(BaseModel):
    source_id: str = Field(default="default", min_length=1, max_length=200)
    source: VisionSourceType = VisionSourceType.IMAGE
    image_path: str | None = Field(default=None, max_length=4_000)
    include_ocr: bool = True
    reasoning_prompt: str | None = Field(default=None, max_length=4_000)
    prompts: list[str] | None = Field(default=None, max_length=32)
    grant_observe: bool = False


class WatchPayload(BaseModel):
    source_id: str = Field(min_length=1, max_length=200)
    condition: str = Field(min_length=1, max_length=2_000)
    user_id: str = Field(default="default", max_length=200)
    check_interval_seconds: float | None = Field(default=None, ge=0.25, le=3_600)
    expires_after_minutes: float | None = Field(default=None, ge=0, le=43_200)
    notify: bool = True
    remember_event: bool = False
    cooldown_seconds: int = Field(default=0, ge=0, le=86_400)
    condition_type: str | None = Field(default=None, max_length=100)
    target_labels: list[str] | None = Field(default=None, max_length=20)


class ComparePayload(BaseModel):
    source_id: str = Field(min_length=1, max_length=200)
    compare_with: str = Field(default="latest", max_length=200)
    snapshot_id: str | None = Field(default=None, max_length=200)


class VerifyPayload(BaseModel):
    expected_result: str = Field(min_length=1, max_length=2_000)
    source_id: str | None = Field(default=None, max_length=200)
    source: VisionSourceType = VisionSourceType.IMAGE
    image_path: str | None = Field(default=None, max_length=4_000)
    reference_snapshot_id: str | None = Field(default=None, max_length=200)
    grant_observe: bool = False


class StartPayload(BaseModel):
    check_interval_seconds: float | None = Field(default=None, ge=0.25, le=3_600)
    grant_observe: bool = False


class RememberPayload(BaseModel):
    approved: bool = False
    session_id: str | None = Field(default=None, max_length=200)


def _is_loopback(host: str | None) -> bool:
    if not host:
        return False
    if host.casefold() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _response(model: Any) -> Any:
    if isinstance(model, VisualEvent):
        return visual_event_public_dict(model)
    return model.model_dump(mode="json") if hasattr(model, "model_dump") else model


def _http_error(exc: Exception) -> HTTPException:
    if isinstance(exc, PermissionError):
        return HTTPException(status_code=403, detail=str(exc))
    if isinstance(exc, ValueError):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, RuntimeError):
        return HTTPException(status_code=409, detail=str(exc))
    return HTTPException(status_code=500, detail="Vision service failed.")


def create_vision_router(service: VisionService) -> APIRouter:
    """Create a router backed by one in-process :class:`VisionService`."""

    router = APIRouter(prefix="/vision", tags=["vision"])

    async def require_local(request: Request) -> None:
        if not _is_loopback(request.client.host if request.client else None):
            raise HTTPException(status_code=403, detail="Vision routes are available only from the local device.")

    @router.post("/sources")
    async def create_source(payload: SourcePayload, request: Request) -> dict[str, Any]:
        await require_local(request)
        try:
            source = service.create_source(
                source_id=payload.source_id,
                source_type=payload.source_type,
                name=payload.name,
                config=payload.config,
                grant_observe=payload.grant_observe,
                grant_remember=payload.grant_remember,
            )
            return {"ok": True, "source": _response(source), "permission": service.store.get_permission(source.source_id)}
        except Exception as exc:
            raise _http_error(exc) from exc

    @router.get("/sources")
    async def list_sources(request: Request) -> dict[str, Any]:
        await require_local(request)
        return {"ok": True, "sources": service.list_sources()}

    @router.delete("/sources/{source_id}")
    async def delete_source(source_id: str, request: Request) -> dict[str, Any]:
        await require_local(request)
        try:
            return {"ok": service.delete_source(source_id), "source_id": source_id}
        except Exception as exc:
            raise _http_error(exc) from exc

    @router.post("/sources/{source_id}/start")
    async def start_source(source_id: str, payload: StartPayload, request: Request) -> dict[str, Any]:
        await require_local(request)
        try:
            source = await service.start_source(
                source_id,
                check_interval_seconds=payload.check_interval_seconds,
                grant_observe=payload.grant_observe,
            )
            return {"ok": True, "source": _response(source)}
        except Exception as exc:
            raise _http_error(exc) from exc

    @router.post("/sources/{source_id}/stop")
    async def stop_source(source_id: str, request: Request) -> dict[str, Any]:
        await require_local(request)
        try:
            return {"ok": await service.stop_source(source_id), "source_id": source_id}
        except Exception as exc:
            raise _http_error(exc) from exc

    @router.post("/observe")
    async def observe(payload: ObservePayload, request: Request) -> dict[str, Any]:
        await require_local(request)
        try:
            if payload.grant_observe:
                if service.store.get_source(payload.source_id) is None:
                    service.create_source(
                        source_id=payload.source_id,
                        source_type=payload.source,
                        grant_observe=True,
                    )
                else:
                    service.grant_permission(payload.source_id, observe=True)
            result = await service.observe(
                source=payload.source,
                source_id=payload.source_id,
                image_path=payload.image_path,
                include_ocr=payload.include_ocr,
                reasoning_prompt=payload.reasoning_prompt,
                prompts=payload.prompts,
            )
            return {"ok": True, **result.model_dump()}
        except Exception as exc:
            raise _http_error(exc) from exc

    @router.post("/watches")
    async def create_watch(payload: WatchPayload, request: Request) -> dict[str, Any]:
        await require_local(request)
        try:
            watch = service.create_watch(**payload.model_dump())
            return {"ok": True, "watch": _response(watch)}
        except Exception as exc:
            raise _http_error(exc) from exc

    @router.get("/watches")
    async def list_watches(request: Request, source_id: str | None = None, status: str | None = None) -> dict[str, Any]:
        await require_local(request)
        return {"ok": True, "watches": [_response(item) for item in service.list_watches(source_id=source_id, status=status)]}

    @router.delete("/watches/{watch_id}")
    async def cancel_watch(watch_id: str, request: Request) -> dict[str, Any]:
        await require_local(request)
        try:
            watch = service.cancel_watch(watch_id)
            return {"ok": watch is not None, "watch": _response(watch) if watch else None}
        except Exception as exc:
            raise _http_error(exc) from exc

    @router.post("/compare")
    async def compare(payload: ComparePayload, request: Request) -> dict[str, Any]:
        await require_local(request)
        try:
            return {"ok": True, **service.compare(**payload.model_dump())}
        except Exception as exc:
            raise _http_error(exc) from exc

    @router.post("/verify")
    async def verify(payload: VerifyPayload, request: Request) -> dict[str, Any]:
        await require_local(request)
        try:
            if payload.grant_observe and payload.source_id:
                if service.store.get_source(payload.source_id) is None:
                    service.create_source(
                        source_id=payload.source_id,
                        source_type=payload.source,
                        grant_observe=True,
                    )
                else:
                    service.grant_permission(payload.source_id, observe=True)
            result = await service.verify(**payload.model_dump(exclude={"grant_observe"}))
            return {"ok": True, "verification": _response(result)}
        except Exception as exc:
            raise _http_error(exc) from exc

    @router.get("/events")
    async def list_events(
        request: Request,
        source_id: str | None = None,
        limit: int = Query(default=100, ge=1, le=1_000),
    ) -> dict[str, Any]:
        await require_local(request)
        return {"ok": True, "events": [_response(item) for item in service.list_events(source_id=source_id, limit=limit)]}

    @router.delete("/events/{event_id}")
    async def delete_event(event_id: str, request: Request) -> dict[str, Any]:
        await require_local(request)
        return {"ok": service.delete_event(event_id), "event_id": event_id}

    @router.post("/events/{event_id}/remember")
    async def remember_event(event_id: str, payload: RememberPayload, request: Request) -> dict[str, Any]:
        await require_local(request)
        try:
            result = service.remember_event(event_id, approved=payload.approved, session_id=payload.session_id)
            return {"ok": True, "memory": result}
        except Exception as exc:
            raise _http_error(exc) from exc

    @router.delete("/memories/{fact_id}/frame")
    async def delete_memory_frame(fact_id: int, request: Request) -> dict[str, Any]:
        await require_local(request)
        if fact_id < 1:
            raise HTTPException(status_code=422, detail="fact_id must be positive")
        return {"ok": True, **service.delete_memory_frame(fact_id)}

    @router.websocket("/stream/{source_id}")
    async def stream_events(websocket: WebSocket, source_id: str) -> None:
        if not _is_loopback(websocket.client.host if websocket.client else None):
            await websocket.close(code=4403)
            return
        if service.store.get_source(source_id) is None:
            await websocket.close(code=4404)
            return
        await websocket.accept()
        subscription = service.event_bus.subscribe(source_id=source_id, maxsize=50)
        try:
            async for event in subscription:
                # Event DTOs are privacy-prepared before publishing and do
                # not contain frame pixels or local artifact locations.
                await websocket.send_json(_response(event))
        except WebSocketDisconnect:
            pass
        finally:
            subscription.close()

    return router


__all__ = ["create_vision_router"]
