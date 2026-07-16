"""Thin tool adapters for :class:`ares.vision.service.VisionService`.

Tool handlers intentionally contain no detector, OCR, or model logic.  They
only validate user-facing arguments and send all work through VisionService.
"""

from __future__ import annotations

import json
from typing import Any, Callable

from ares.vision.models import VisionSourceType, visual_event_public_dict
from ares.vision.service import VisionService


class VisionToolHandlers:
    """Expose the local vision subsystem through Ares function tools."""

    def __init__(
        self,
        service: VisionService,
        *,
        session_id_provider: Callable[[], str | None] | None = None,
    ) -> None:
        self.service = service
        self.session_id_provider = session_id_provider or (lambda: None)

    @staticmethod
    def _json(payload: Any) -> str:
        if hasattr(payload, "model_dump"):
            payload = payload.model_dump(mode="json")
        return json.dumps(payload, ensure_ascii=False, indent=2, default=str)

    @staticmethod
    def _source_type(value: Any) -> VisionSourceType:
        return VisionSourceType(str(value or "image").casefold())

    def _ensure_source(self, args: dict[str, Any]) -> tuple[str, VisionSourceType]:
        source_id = str(args.get("source_id") or "default").strip()
        source_type = self._source_type(args.get("source") or args.get("source_type"))
        if not source_id:
            raise ValueError("source_id is required")
        existing = self.service.store.get_source(source_id)
        if existing is None:
            self.service.create_source(
                source_id=source_id,
                source_type=source_type,
                config=dict(args.get("source_config") or {}),
                grant_observe=bool(args.get("grant_observe", source_type is VisionSourceType.IMAGE)),
                grant_remember=bool(args.get("grant_remember", False)),
            )
        else:
            source_type = existing.source_type
            # Consent may be provided on a later tool call.  Do not silently
            # change it merely because an existing image source is reused.
            observe = args.get("grant_observe")
            remember = args.get("grant_remember")
            if observe is not None or remember is not None:
                self.service.grant_permission(
                    source_id,
                    observe=bool(observe) if observe is not None else None,
                    remember=bool(remember) if remember is not None else None,
                )
        return source_id, source_type

    async def observe(self, args: dict[str, Any]) -> str:
        source_id, source_type = self._ensure_source(args)
        result = await self.service.observe(
            source=source_type,
            source_id=source_id,
            image_path=args.get("image_path") or args.get("path"),
            include_ocr=bool(args.get("include_ocr", True)),
            reasoning_prompt=args.get("reasoning_prompt"),
            prompts=args.get("prompts") if isinstance(args.get("prompts"), list) else None,
        )
        return self._json({"ok": True, **result.model_dump()})

    async def watch(self, args: dict[str, Any]) -> str:
        source_id, source_type = self._ensure_source(args)
        condition = str(args.get("condition") or "").strip()
        if not condition:
            raise ValueError("condition is required")
        if source_type in {VisionSourceType.CAMERA, VisionSourceType.SCREEN} and not self.service.privacy.is_source_active(source_id):
            await self.service.start_source(
                source_id,
                check_interval_seconds=args.get("check_interval_seconds"),
                grant_observe=bool(args.get("grant_observe", False)),
            )
        watch = self.service.create_watch(
            source_id=source_id,
            condition=condition,
            user_id=str(args.get("user_id") or "default"),
            check_interval_seconds=args.get("check_interval_seconds"),
            expires_after_minutes=args.get("expires_after_minutes"),
            notify=bool(args.get("notify", True)),
            remember_event=bool(args.get("remember_event", False)),
            cooldown_seconds=int(args.get("cooldown_seconds", 0)),
            condition_type=args.get("condition_type"),
            target_labels=args.get("target_labels") if isinstance(args.get("target_labels"), list) else None,
        )
        return self._json({"ok": True, "watch": watch.model_dump(mode="json")})

    async def compare(self, args: dict[str, Any]) -> str:
        source_id = str(args.get("source_id") or "").strip()
        if not source_id:
            raise ValueError("source_id is required")
        return self._json({
            "ok": True,
            **self.service.compare(
                source_id=source_id,
                compare_with=str(args.get("compare_with") or "latest"),
                snapshot_id=args.get("snapshot_id"),
            ),
        })

    async def verify(self, args: dict[str, Any]) -> str:
        expected = str(args.get("expected_result") or "").strip()
        if not expected:
            raise ValueError("expected_result is required")
        source_id = args.get("source_id")
        source_type = self._source_type(args.get("source") or "image")
        if source_id:
            source_id, source_type = self._ensure_source(args)
        result = await self.service.verify(
            expected_result=expected,
            source_id=source_id,
            source=source_type,
            reference_snapshot_id=args.get("reference_snapshot_id"),
            image_path=args.get("image_path") or args.get("path"),
        )
        return self._json({"ok": True, **result.model_dump(mode="json")})

    async def remember(self, args: dict[str, Any]) -> str:
        event_id = str(args.get("event_id") or "").strip()
        if not event_id:
            raise ValueError("event_id is required")
        result = self.service.remember_event(
            event_id,
            approved=bool(args.get("approved", True)),
            session_id=self.session_id_provider(),
        )
        return self._json({"ok": True, "memory": result})

    async def list_watches(self, args: dict[str, Any]) -> str:
        watches = self.service.list_watches(source_id=args.get("source_id"), status=args.get("status"))
        return self._json({"ok": True, "watches": [item.model_dump(mode="json") for item in watches]})

    async def cancel_watch(self, args: dict[str, Any]) -> str:
        watch_id = str(args.get("watch_id") or "").strip()
        if not watch_id:
            raise ValueError("watch_id is required")
        watch = self.service.cancel_watch(watch_id)
        return self._json({"ok": watch is not None, "watch": watch.model_dump(mode="json") if watch else None})

    async def start_source(self, args: dict[str, Any]) -> str:
        source_id, _source_type = self._ensure_source(args)
        source = await self.service.start_source(
            source_id,
            check_interval_seconds=args.get("check_interval_seconds"),
            grant_observe=bool(args.get("grant_observe", False)),
        )
        return self._json({"ok": True, "source": source.model_dump(mode="json")})

    async def stop_source(self, args: dict[str, Any]) -> str:
        source_id = str(args.get("source_id") or "").strip()
        if not source_id:
            raise ValueError("source_id is required")
        return self._json({"ok": self.service is not None and await self.service.stop_source(source_id), "source_id": source_id})

    async def stop_all_sources(self, _args: dict[str, Any]) -> str:
        sources = await self.service.stop_all_sources()
        return self._json({"ok": True, "stopped_source_ids": sources})

    async def list_sources(self, _args: dict[str, Any]) -> str:
        return self._json({"ok": True, "sources": self.service.list_sources()})

    async def list_events(self, args: dict[str, Any]) -> str:
        events = self.service.list_events(source_id=args.get("source_id"), limit=int(args.get("limit", 100)))
        return self._json({"ok": True, "events": [visual_event_public_dict(item) for item in events]})

    async def delete_event(self, args: dict[str, Any]) -> str:
        event_id = str(args.get("event_id") or "").strip()
        if not event_id:
            raise ValueError("event_id is required")
        return self._json({"ok": self.service.delete_event(event_id), "event_id": event_id})

    async def erase_recent_events(self, args: dict[str, Any]) -> str:
        deleted = self.service.erase_recent_events(
            minutes=float(args.get("minutes", 60)), source_id=args.get("source_id"),
        )
        return self._json({"ok": True, "deleted_events": deleted})

    async def delete_memory_frame(self, args: dict[str, Any]) -> str:
        try:
            fact_id = int(args.get("fact_id"))
        except (TypeError, ValueError) as exc:
            raise ValueError("fact_id is required") from exc
        if fact_id < 1:
            raise ValueError("fact_id is required")
        result = self.service.delete_memory_frame(fact_id)
        return self._json({"ok": True, **result})

    async def dispatch(self, tool_name: str, args: dict[str, Any]) -> str:
        handlers = {
            "vision_observe": self.observe,
            "vision_watch": self.watch,
            "vision_compare": self.compare,
            "vision_verify": self.verify,
            "vision_remember": self.remember,
            "vision_list_watches": self.list_watches,
            "vision_cancel_watch": self.cancel_watch,
            "vision_start_source": self.start_source,
            "vision_stop_source": self.stop_source,
            "vision_stop_all_sources": self.stop_all_sources,
            "vision_list_sources": self.list_sources,
            "vision_list_events": self.list_events,
            "vision_delete_event": self.delete_event,
            "vision_erase_recent_events": self.erase_recent_events,
            "vision_delete_memory_frame": self.delete_memory_frame,
        }
        try:
            handler = handlers[tool_name]
        except KeyError as exc:
            raise ValueError(f"Unknown vision tool: {tool_name}") from exc
        try:
            return await handler(args)
        except (ValueError, PermissionError, RuntimeError) as exc:
            return self._json({"ok": False, "error": str(exc)})


__all__ = ["VisionToolHandlers"]
