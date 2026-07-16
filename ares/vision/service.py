"""Local-first orchestration for Ares Vision.

The service owns the perception loop, but never owns a continuous recording.
Only the current in-memory frame is retained while a source is active; SQLite
contains structured snapshots/events and an explicitly approved frame reference
at most.  Heavy providers are injected and can be absent on a normal install.
"""

from __future__ import annotations

import asyncio
import inspect
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable

from ares.memory import MemoryStore
from ares.vision.events import VisionEventBus, make_visual_event
from ares.vision.memory import VisionMemory
from ares.vision.models import (
    DetectedObject,
    SceneSnapshot,
    VerificationResult,
    VisionFrame,
    VisionSource,
    VisionSourceType,
    VisionWatch,
    VisualEvent,
    visual_event_public_dict,
)
from ares.vision.privacy import VisionPermissionController
from ares.vision.scene import SceneDiffer
from ares.vision.store import VisionStore
from ares.vision.tracker import ObjectTracker
from ares.vision.verifier import VisionVerifier
from ares.vision.watch_engine import WatchEngine, parse_watch_condition


NotifyCallback = Callable[[VisualEvent, VisionWatch | None], Any]
SummaryCallback = Callable[[VisionFrame, SceneSnapshot, str | None], Any]
SemanticWatchCallback = Callable[..., Any]
GoalSuggestionCallback = Callable[[VisualEvent], Any]
FollowUpCallback = Callable[[VisualEvent], Any]

_DISALLOWED_VISION_INFERENCE_RE = re.compile(
    r"\b(?:face\s+(?:recognition|identification)|identify|recognise|recognize|name|who\s+is|"
    r"match)\b.{0,80}\b(?:face|person|people|human)\b|"
    r"\b(?:emotion|emotional|mood|feeling|age|gender|race|ethnicity)\b",
    re.IGNORECASE,
)


async def _await_maybe(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


def _assert_supported_visual_request(text: str | None) -> None:
    if text and _DISALLOWED_VISION_INFERENCE_RE.search(text):
        raise ValueError(
            "Ares Vision does not perform face identification, emotion, or sensitive-attribute inference."
        )


class _EmptyDetector:
    async def detect(self, _frame: VisionFrame, prompts: list[str] | None = None) -> list[DetectedObject]:
        return []


class _EmptyOCR:
    async def read(self, _frame: VisionFrame) -> list[str]:
        return []


@dataclass(slots=True)
class VisionObservation:
    """One service observation plus side-effect-free scene-change evidence."""

    snapshot: SceneSnapshot
    events: list[VisualEvent] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    motion_score: float | None = None
    detector_ran: bool = True
    ocr_ran: bool = False

    def model_dump(self) -> dict[str, Any]:
        return {
            "summary": self.snapshot.summary,
            "objects": [item.model_dump(mode="json") for item in self.snapshot.objects],
            "visible_text": list(self.snapshot.visible_text),
            "snapshot_id": self.snapshot.snapshot_id,
            "source_id": self.snapshot.source_id,
            "events": [visual_event_public_dict(item) for item in self.events],
            "warnings": list(self.warnings),
            "motion_score": self.motion_score,
            "detector_ran": self.detector_ran,
            "ocr_ran": self.ocr_ran,
        }


@dataclass(slots=True)
class _SourceRuntime:
    tracker: ObjectTracker = field(default_factory=ObjectTracker)
    differ: SceneDiffer = field(default_factory=SceneDiffer)
    latest_snapshot: SceneSnapshot | None = None
    latest_image: Any = None
    frame_count: int = 0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class VisionService:
    """Coordinates capture, privacy, detection, tracking, watches and memory."""

    def __init__(
        self,
        *,
        database_path: str | Path | None = None,
        store: VisionStore | None = None,
        memory_store: MemoryStore | None = None,
        detector: Any | None = None,
        ocr: Any | None = None,
        verifier: VisionVerifier | None = None,
        watch_engine: WatchEngine | None = None,
        event_bus: VisionEventBus | None = None,
        config: Any | None = None,
        action_ledger: Any | None = None,
        image_capture: Any | None = None,
        privacy: VisionPermissionController | None = None,
        notifier: NotifyCallback | None = None,
        summary_callback: SummaryCallback | None = None,
        semantic_watch_callback: SemanticWatchCallback | None = None,
        goal_suggestion_callback: GoalSuggestionCallback | None = None,
        follow_up_callback: FollowUpCallback | None = None,
    ) -> None:
        if store is None:
            if database_path is None:
                memory_path = getattr(memory_store, "db_path", None)
                database_path = Path(memory_path).with_name("vision.db") if memory_path else Path("~/.ares/data/vision.db").expanduser()
            store = VisionStore(database_path)
        self.store = store
        self.memory_store = memory_store
        self.visual_memory = VisionMemory(memory_store, store) if memory_store is not None else None
        if detector is None:
            try:
                from ares.vision.detector import create_default_detector

                detector = create_default_detector(
                    model_name=str(getattr(config, "detector_model", "yolo26n.pt")),
                )
            except Exception:
                detector = _EmptyDetector()
        if ocr is None:
            try:
                from ares.vision.ocr import create_default_ocr

                ocr = create_default_ocr()
            except Exception:
                ocr = _EmptyOCR()
        self.detector = detector
        self.ocr = ocr
        threshold = float(getattr(config, "verification_confidence_threshold", 0.80))
        self.verifier = verifier or VisionVerifier(confidence_threshold=threshold)
        self.watch_engine = watch_engine or WatchEngine()
        self.event_bus = event_bus or VisionEventBus()
        self.config = config
        self.action_ledger = action_ledger
        self.image_capture = image_capture
        self.privacy = privacy or VisionPermissionController(config)
        self.notifier = notifier
        self.summary_callback = summary_callback
        self.semantic_watch_callback = semantic_watch_callback
        self.goal_suggestion_callback = goal_suggestion_callback
        self.follow_up_callback = follow_up_callback
        self._captures: dict[str, Any] = {}
        self._capture_tasks: dict[str, asyncio.Task[None]] = {}
        self._runtime: dict[str, _SourceRuntime] = {}
        self._closed = False
        self._shutdown_task: asyncio.Task[None] | None = None
        self._last_retention_cleanup: datetime | None = None

    # -- Source lifecycle and consent ---------------------------------------

    @staticmethod
    def _source_type(value: VisionSourceType | str) -> VisionSourceType:
        return value if isinstance(value, VisionSourceType) else VisionSourceType(str(value).casefold())

    def _runtime_for(self, source_id: str) -> _SourceRuntime:
        runtime = self._runtime.get(source_id)
        if runtime is None:
            runtime = _SourceRuntime(
                differ=SceneDiffer(
                    duplicate_window_seconds=float(getattr(self.config, "event_cooldown_seconds", 5.0)),
                )
            )
            self._runtime[source_id] = runtime
        return runtime

    def create_source(
        self,
        *,
        source_type: VisionSourceType | str,
        source_id: str,
        config: dict[str, Any] | None = None,
        name: str | None = None,
        grant_observe: bool | None = None,
        grant_remember: bool | None = None,
        capture: Any | None = None,
    ) -> VisionSource:
        """Register a source; camera/screen observation still needs consent."""
        kind = self._source_type(source_type)
        existing = self.store.get_source(source_id)
        source = VisionSource(
            source_id=source_id,
            source_type=kind,
            status=existing.status if existing is not None else "stopped",
            config=dict(config if config is not None else (existing.config if existing else {})),
            name=name if name is not None else (existing.name if existing else None),
            created_at=existing.created_at if existing else datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
            last_captured_at=existing.last_captured_at if existing else None,
        )
        self.store.save_source(source)
        # Supplying an uploaded image is an explicit per-request observation.
        # Persistent cameras/screens always start with a denied permission.
        if kind is VisionSourceType.IMAGE and grant_observe is None:
            grant_observe = True
        if grant_observe is not None or grant_remember is not None:
            self.store.set_permission(
                source_id,
                observe_allowed=grant_observe,
                remember_allowed=grant_remember,
            )
        if grant_observe:
            self.privacy.grant_observation(source_id, kind)
        if grant_remember:
            self.privacy.grant_memory(source_id)
        if capture is not None:
            self._captures[source_id] = capture
        return source

    def list_sources(self) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for source in self.store.list_sources():
            payload = source.model_dump(mode="json")
            payload["permission"] = self.store.get_permission(source.source_id)
            payload["active"] = bool(
                source.source_id in self._capture_tasks
                or self.privacy.is_source_active(source.source_id)
            )
            result.append(payload)
        return result

    def grant_permission(
        self, source_id: str, *, observe: bool | None = None, remember: bool | None = None,
    ) -> dict[str, bool]:
        if self.store.get_source(source_id) is None:
            raise ValueError(f"Vision source '{source_id}' was not found.")
        source = self.store.get_source(source_id)
        assert source is not None
        values = self.store.set_permission(
            source_id, observe_allowed=observe, remember_allowed=remember,
        )
        if observe:
            self.privacy.grant_observation(source_id, source.source_type)
        if remember:
            self.privacy.grant_memory(source_id)
        return values

    def _require_observe_permission(self, source_id: str, source_type: VisionSourceType) -> None:
        if source_type not in {VisionSourceType.CAMERA, VisionSourceType.SCREEN}:
            return
        permission = self.store.get_permission(source_id)
        if not permission["observe_allowed"]:
            raise PermissionError(
                f"Observation permission is required before using {source_type.value} source '{source_id}'."
            )
        self.privacy.assert_observation_allowed(source_id, source_type)

    async def start_source(
        self,
        source_id: str,
        *,
        check_interval_seconds: float | None = None,
        grant_observe: bool = False,
    ) -> VisionSource:
        """Start a non-blocking camera/screen worker after explicit consent."""
        source = self.store.get_source(source_id)
        if source is None:
            raise ValueError(f"Vision source '{source_id}' was not found.")
        if grant_observe:
            self.store.set_permission(source_id, observe_allowed=True)
            self.privacy.grant_observation(source_id, source.source_type)
        self._require_observe_permission(source_id, source.source_type)
        if source_id not in self._captures:
            self._captures[source_id] = self._default_capture(source)
        starter = getattr(self._captures[source_id], "start", None)
        if callable(starter):
            await _await_maybe(starter())
        source = source.model_copy(update={"status": "active", "updated_at": datetime.now(timezone.utc)})
        self.store.save_source(source)
        self.store.set_permission(source_id, active_indicator=True)
        self.privacy.mark_source_active(source_id, source.source_type)
        if source_id not in self._capture_tasks or self._capture_tasks[source_id].done():
            configured_interval = source.config.get("check_interval_seconds")
            interval = float(
                check_interval_seconds
                if check_interval_seconds is not None
                else configured_interval
                if configured_interval is not None
                else getattr(self.config, "default_watch_interval_seconds", 3.0)
            )
            self._capture_tasks[source_id] = asyncio.create_task(
                self._capture_loop(source_id, max(0.25, interval)),
                name=f"ares-vision-{source_id}",
            )
        self._record_action("vision_source_started", source_id, f"Started {source.source_type.value} observation.")
        return source

    async def stop_source(self, source_id: str) -> bool:
        task = self._capture_tasks.pop(source_id, None)
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        capture = self._captures.get(source_id)
        close = getattr(capture, "close", None)
        if callable(close):
            with _suppress_errors():
                await _await_maybe(close())
        runtime = self._runtime.get(source_id)
        if runtime is not None:
            runtime.latest_image = None
        source = self.store.get_source(source_id)
        if source is None:
            return False
        self.store.save_source(source.model_copy(update={"status": "stopped", "updated_at": datetime.now(timezone.utc)}))
        self.store.set_permission(source_id, active_indicator=False)
        self.privacy.mark_source_inactive(source_id)
        self._record_action("vision_source_stopped", source_id, "Stopped visual observation.")
        return True

    async def stop_all_sources(self) -> list[str]:
        active = list(dict.fromkeys([
            *self._capture_tasks,
            *(source.source_id for source in self.store.list_sources()
              if self.store.get_permission(source.source_id)["active_indicator"]),
        ]))
        for source_id in active:
            await self.stop_source(source_id)
        # A stale active indicator is never useful after an application stop.
        for source in self.store.list_sources():
            if self.store.get_permission(source.source_id)["active_indicator"]:
                self.store.set_permission(source.source_id, active_indicator=False)
        self.privacy.stop_all()
        return active

    def delete_source(self, source_id: str) -> bool:
        if source_id in self._capture_tasks or self.privacy.is_source_active(source_id):
            raise RuntimeError("Stop a visual source before deleting it.")
        # A source is also the provenance boundary for saved visual facts.
        # Delete those facts before the store removes their event links so a
        # source-erasure request cannot leave searchable visual memories.
        if self.memory_store is not None:
            for fact_id in self.store.memory_fact_ids_for_source(source_id):
                with _suppress_errors():
                    self.memory_store.delete(fact_id)
        self._captures.pop(source_id, None)
        self._runtime.pop(source_id, None)
        return self.store.delete_source(source_id)

    def _default_capture(self, source: VisionSource) -> Any:
        try:
            from ares.vision.capture import CameraCapture, ScreenCapture
        except ImportError as exc:  # pragma: no cover - only while a partial install is being upgraded
            raise RuntimeError("Vision capture providers are unavailable.") from exc
        if source.source_type is VisionSourceType.CAMERA:
            return CameraCapture(**source.config)
        if source.source_type is VisionSourceType.SCREEN:
            return ScreenCapture(**source.config)
        raise ValueError(f"Source type '{source.source_type.value}' does not support live capture.")

    async def _capture_loop(self, source_id: str, interval: float) -> None:
        try:
            while not self._closed:
                source = self.store.get_source(source_id)
                if source is None or source.status != "active":
                    return
                try:
                    frame = await self._capture_live(source)
                    await self.process_frame(frame, include_ocr=True, force=False)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    event = self._record_event(
                        make_visual_event(
                            event_type="source_error",
                            source_id=source_id,
                            subject=None,
                            description=f"Visual source interrupted: {exc}",
                            confidence=1.0,
                        )
                    )
                    self.store.save_source(source.model_copy(update={"status": "error", "updated_at": datetime.now(timezone.utc)}))
                    self.store.set_permission(source_id, active_indicator=False)
                    self.privacy.mark_source_inactive(source_id)
                    runtime = self._runtime.get(source_id)
                    if runtime is not None:
                        runtime.latest_image = None
                    capture = self._captures.get(source_id)
                    close = getattr(capture, "close", None)
                    if callable(close):
                        with _suppress_errors():
                            await _await_maybe(close())
                    if self.follow_up_callback is not None and self.store.list_watches(source_id=source_id, status="active"):
                        with _suppress_errors():
                            await _await_maybe(self.follow_up_callback(event))
                    return
                await asyncio.sleep(interval)
        finally:
            self._capture_tasks.pop(source_id, None)

    # -- Observation ---------------------------------------------------------

    async def observe(
        self,
        *,
        source: VisionSourceType | str = VisionSourceType.IMAGE,
        source_id: str = "default",
        image_path: str | Path | None = None,
        frame: VisionFrame | None = None,
        include_ocr: bool = True,
        reasoning_prompt: str | None = None,
        prompts: list[str] | None = None,
    ) -> VisionObservation:
        """Observe one supplied image or the current frame of an active source."""
        kind = self._source_type(source)
        transient_source_id: str | None = None
        if frame is None:
            if image_path is not None:
                kind = VisionSourceType.IMAGE
                self.create_source(source_type=kind, source_id=source_id, grant_observe=True)
                frame = await self._capture_image(image_path, source_id=source_id)
            else:
                registered = self.store.get_source(source_id)
                if registered is None:
                    registered = self.create_source(source_type=kind, source_id=source_id)
                kind = registered.source_type
                self._require_observe_permission(source_id, kind)
                if kind in {VisionSourceType.CAMERA, VisionSourceType.SCREEN} and not self.privacy.is_source_active(source_id):
                    await self._start_transient_source(registered)
                    transient_source_id = source_id
                try:
                    frame = await self._capture_live(registered)
                except Exception:
                    if transient_source_id is not None:
                        await self.stop_source(transient_source_id)
                    raise
        else:
            kind = frame.source_type
            source_id = frame.source_id
            if self.store.get_source(source_id) is None:
                self.create_source(source_type=kind, source_id=source_id, grant_observe=(kind is VisionSourceType.IMAGE))
        self._require_observe_permission(source_id, kind)
        try:
            return await self.process_frame(
                frame,
                include_ocr=include_ocr,
                reasoning_prompt=reasoning_prompt,
                prompts=prompts,
                force=True,
            )
        finally:
            if transient_source_id is not None:
                await self.stop_source(transient_source_id)

    async def _start_transient_source(self, source: VisionSource) -> None:
        """Show an active indicator around an explicitly approved one-shot read."""

        self._require_observe_permission(source.source_id, source.source_type)
        if source.source_id not in self._captures:
            self._captures[source.source_id] = self._default_capture(source)
        starter = getattr(self._captures[source.source_id], "start", None)
        if callable(starter):
            await _await_maybe(starter())
        active = source.model_copy(update={"status": "active", "updated_at": datetime.now(timezone.utc)})
        self.store.save_source(active)
        self.store.set_permission(source.source_id, active_indicator=True)
        self.privacy.mark_source_active(source.source_id, source.source_type)
        self._record_action("vision_source_started", source.source_id, f"Started one-shot {source.source_type.value} observation.")

    async def _capture_image(self, image_path: str | Path, *, source_id: str) -> VisionFrame:
        if self.image_capture is None:
            try:
                from ares.vision.capture import ImageCapture

                self.image_capture = ImageCapture()
            except ImportError as exc:  # pragma: no cover - protects partial upgrades
                raise RuntimeError("Image capture support is unavailable.") from exc
        capture = getattr(self.image_capture, "capture", self.image_capture)
        return await _await_maybe(capture(image_path, source_id=source_id, source_type=VisionSourceType.IMAGE))

    async def _capture_live(self, source: VisionSource) -> VisionFrame:
        capture = self._captures.get(source.source_id)
        if capture is None:
            capture = self._default_capture(source)
            self._captures[source.source_id] = capture
        method = getattr(capture, "capture", capture)
        try:
            value = method(source_id=source.source_id, source_type=source.source_type)
        except TypeError:
            value = method()
        frame = await _await_maybe(value)
        if not isinstance(frame, VisionFrame):
            frame = VisionFrame(source_id=source.source_id, source_type=source.source_type, image=frame)
        return frame

    async def process_frame(
        self,
        frame: VisionFrame,
        *,
        include_ocr: bool = True,
        reasoning_prompt: str | None = None,
        prompts: list[str] | None = None,
        force: bool = False,
    ) -> VisionObservation:
        """Process an in-memory frame without persisting its raw pixel data."""
        if self._closed:
            raise RuntimeError("VisionService is closed.")
        _assert_supported_visual_request(reasoning_prompt)
        self._purge_expired_frames()
        source = self.store.get_source(frame.source_id)
        if source is None:
            source = self.create_source(
                source_type=frame.source_type,
                source_id=frame.source_id,
                grant_observe=(frame.source_type is VisionSourceType.IMAGE),
            )
        self._require_observe_permission(frame.source_id, frame.source_type)
        runtime = self._runtime_for(frame.source_id)
        async with runtime.lock:
            runtime.frame_count += 1
            previous = runtime.latest_snapshot or self.store.latest_snapshot(frame.source_id)
            motion_score = self._motion_score(runtime.latest_image, frame.image)
            interval = max(1, int(getattr(self.config, "detection_interval_frames", 5)))
            detector_ran = bool(force or previous is None or runtime.frame_count % interval == 0 or motion_score is None or motion_score > float(getattr(self.config, "motion_threshold", 0.025)))
            warnings: list[str] = []
            if detector_ran:
                objects = await self._detect(frame, prompts, warnings)
                objects = runtime.tracker.update(
                    objects,
                    frame_size=(frame.width, frame.height) if frame.width and frame.height else None,
                    observed_at=frame.captured_at,
                )
            else:
                objects = list(previous.objects) if previous is not None else []
            run_ocr = bool(include_ocr and (force or previous is None or detector_ran or motion_score is None or motion_score > 0.0))
            if run_ocr:
                visible_text = self._redact_texts(await self._read_ocr(frame, warnings))
            else:
                visible_text = list(previous.visible_text) if previous is not None else []
            snapshot = SceneSnapshot(
                source_id=frame.source_id,
                captured_at=frame.captured_at,
                objects=objects,
                visible_text=visible_text,
                summary=self._basic_summary(objects, visible_text),
                # Keeping arbitrary input paths would violate no-retention. A
                # frame reference can be set later only for an approved event.
                frame_reference=None,
            )
            if reasoning_prompt and self.summary_callback is not None:
                try:
                    generated = await _await_maybe(self.summary_callback(frame, snapshot, reasoning_prompt))
                    if generated:
                        snapshot.summary = self.privacy.redact_text(str(generated).strip()[:10_000])
                except Exception as exc:
                    warnings.append(f"Optional visual reasoning was unavailable: {exc}")
            events = runtime.differ.diff(
                previous,
                snapshot,
                frame_size=(frame.width, frame.height) if frame.width and frame.height else None,
                now=frame.captured_at,
            )
            runtime.latest_snapshot = snapshot
            # A supplied still image is not a live source; do not retain its
            # pixels after we have extracted the structured snapshot.
            runtime.latest_image = frame.image if self.privacy.is_source_active(frame.source_id) else None
            self.store.save_snapshot(self.privacy.prepare_snapshot_for_storage(snapshot))
            self.store.prune_snapshots(
                frame.source_id,
                keep=max(2, int(getattr(self.config, "snapshot_history", 2))),
            )
            source = source.model_copy(update={"last_captured_at": frame.captured_at, "updated_at": datetime.now(timezone.utc)})
            self.store.save_source(source)
            events = [self._record_event(self._retain_event_frame(event, frame)) for event in events]
            watch_events = await self._evaluate_watches(snapshot, events, frame=frame)
            events.extend(watch_events)
            return VisionObservation(
                snapshot=snapshot,
                events=events,
                warnings=warnings,
                motion_score=motion_score,
                detector_ran=detector_ran,
                ocr_ran=run_ocr,
            )

    async def _detect(self, frame: VisionFrame, prompts: list[str] | None, warnings: list[str]) -> list[DetectedObject]:
        try:
            result = await _await_maybe(self.detector.detect(frame, prompts=prompts))
        except Exception as exc:
            warnings.append(f"Object detector unavailable: {exc}")
            return []
        objects: list[DetectedObject] = []
        for item in result or []:
            try:
                objects.append(item if isinstance(item, DetectedObject) else DetectedObject.model_validate(item))
            except Exception as exc:
                warnings.append(f"Ignored invalid detector result: {exc}")
        return objects

    async def _read_ocr(self, frame: VisionFrame, warnings: list[str]) -> list[str]:
        reader = getattr(self.ocr, "read", self.ocr)
        try:
            result = await _await_maybe(reader(frame))
        except Exception as exc:
            warnings.append(f"OCR unavailable: {exc}")
            return []
        lines: list[str] = []
        for item in result or []:
            if isinstance(item, str):
                text = item
            elif isinstance(item, dict):
                text = item.get("text") or item.get("value") or ""
            elif isinstance(item, (tuple, list)) and item:
                text = item[0] if isinstance(item[0], str) else ""
            else:
                text = str(item or "")
            cleaned = " ".join(str(text).split())
            if cleaned:
                lines.append(cleaned)
        return lines

    def _retain_event_frame(self, event: VisualEvent, frame: VisionFrame) -> VisualEvent:
        """Write a single owned event frame only under the explicit policy.

        Normal snapshots and frames remain metadata-only.  A configured frame
        retention window plus a separate memory grant is required before this
        best-effort export is attempted.
        """

        if not self.privacy.can_retain_frames(event.source_id) or frame.image is None:
            return event
        try:
            from PIL import Image
            import numpy as np

            image = frame.image.copy() if isinstance(frame.image, Image.Image) else Image.fromarray(np.asarray(frame.image))
            maximum_width = max(160, int(getattr(self.config, "max_frame_width", 1280)))
            if image.width > maximum_width:
                image.thumbnail((maximum_width, max(1, int(image.height * maximum_width / image.width))))
            safe_event_id = re.sub(r"[^A-Za-z0-9_.-]+", "_", event.event_id)
            target = self.store.artifact_root / f"{safe_event_id}.webp"
            temporary = target.with_suffix(".tmp.webp")
            image.convert("RGB").save(temporary, format="WEBP", quality=82, method=4)
            temporary.replace(target)
            return event.model_copy(update={"frame_reference": str(target)})
        except Exception:
            # A missing Pillow codec or a malformed provider frame should not
            # make a useful visual event disappear.
            return event

    def _purge_expired_frames(self) -> None:
        now = datetime.now(timezone.utc)
        if self._last_retention_cleanup is not None and now - self._last_retention_cleanup < timedelta(seconds=30):
            return
        self._last_retention_cleanup = now
        seconds = max(0, int(getattr(self.privacy.config, "frame_retention_seconds", 0)))
        with _suppress_errors():
            self.store.expire_frame_references(now - timedelta(seconds=seconds))

    @staticmethod
    def _basic_summary(objects: Iterable[DetectedObject], visible_text: Iterable[str]) -> str:
        labels = [item.label for item in objects]
        counts: dict[str, int] = {}
        for label in labels:
            counts[label] = counts.get(label, 0) + 1
        pieces = [
            f"{count} {label}{'' if count == 1 else 's'}"
            for label, count in sorted(counts.items())
        ]
        if pieces:
            text = "Detected " + ", ".join(pieces) + "."
        else:
            text = "No common objects were detected."
        first_text = next(iter(visible_text), "")
        return f"{text} Visible text: {first_text}." if first_text else text

    @staticmethod
    def _motion_score(previous: Any, current: Any) -> float | None:
        """Cheap luminance difference used to avoid unnecessary heavy models."""
        if previous is None or current is None:
            return None
        try:
            import numpy as np
            from PIL import Image

            def luminance(value: Any) -> Any:
                image = value if isinstance(value, Image.Image) else Image.fromarray(np.asarray(value))
                return np.asarray(image.convert("L").resize((96, 96)), dtype=np.float32) / 255.0

            first, second = luminance(previous), luminance(current)
            return float(np.mean(np.abs(first - second)))
        except Exception:
            return None

    @staticmethod
    def _redact_texts(lines: Iterable[str]) -> list[str]:
        """Mask common sensitive tokens before OCR text reaches durable state."""
        output: list[str] = []
        for line in lines:
            text = str(line)
            # Cards, Aadhaar-like long numbers, API keys, email and phone-like
            # values are intentionally masked before snapshots/events/memory.
            text = re.sub(r"\b(?:\d[ -]?){13,19}\b", "[REDACTED_NUMBER]", text)
            text = re.sub(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b", "[REDACTED_EMAIL]", text)
            text = re.sub(r"\b(?:\+?\d[\d ()-]{7,}\d)\b", "[REDACTED_PHONE]", text)
            text = re.sub(r"\b(?:sk|api|token|secret)[_-][A-Za-z0-9_-]{12,}\b", "[REDACTED_SECRET]", text, flags=re.I)
            if text.strip():
                output.append(text)
        return output

    # -- Watches -------------------------------------------------------------

    def create_watch(
        self,
        *,
        source_id: str,
        condition: str,
        user_id: str = "default",
        check_interval_seconds: float | None = None,
        expires_after_minutes: float | None = None,
        notify: bool = True,
        remember_event: bool = False,
        cooldown_seconds: int = 0,
        condition_type: str | None = None,
        target_labels: list[str] | None = None,
    ) -> VisionWatch:
        if self.store.get_source(source_id) is None:
            raise ValueError(f"Vision source '{source_id}' was not found.")
        _assert_supported_visual_request(condition)
        rule = parse_watch_condition(
            condition, target_labels=target_labels, condition_type=condition_type,
        )
        expires_at = (
            datetime.now(timezone.utc) + timedelta(minutes=max(0.0, float(expires_after_minutes)))
            if expires_after_minutes is not None else None
        )
        watch = VisionWatch(
            source_id=source_id,
            user_id=user_id,
            condition_text=condition,
            condition_type=rule.condition_type,
            target_labels=rule.target_labels,
            expires_at=expires_at,
            cooldown_seconds=max(0, int(cooldown_seconds)),
            notify=bool(notify),
            remember_event=bool(remember_event),
        )
        self.store.save_watch(watch)
        self._record_action("vision_watch_created", watch.watch_id, f"Created visual watch for {source_id}.")
        # The interval is a worker scheduling preference, not durable footage.
        if check_interval_seconds is not None:
            source = self.store.get_source(source_id)
            if source is not None:
                config = dict(source.config)
                config["check_interval_seconds"] = max(0.25, float(check_interval_seconds))
                self.store.save_source(source.model_copy(update={"config": config, "updated_at": datetime.now(timezone.utc)}))
        return watch

    def list_watches(self, *, source_id: str | None = None, status: str | None = None) -> list[VisionWatch]:
        watches = self.store.list_watches(source_id=source_id)
        now = datetime.now(timezone.utc)
        # Listing is deliberately side-effect-free so it remains a harmless
        # status query; the live evaluation loop persists expiration.
        rendered = [
            watch.model_copy(update={"status": "expired"})
            if watch.status == "active" and watch.is_expired(now)
            else watch
            for watch in watches
        ]
        return [watch for watch in rendered if status is None or watch.status == status]

    def cancel_watch(self, watch_id: str) -> VisionWatch | None:
        watch = self.store.get_watch(watch_id)
        if watch is None:
            return None
        watch.status = "cancelled"
        self.store.save_watch(watch)
        self._record_action("vision_watch_cancelled", watch_id, "Cancelled visual watch.")
        return watch

    def _expire_watches(self) -> list[VisionWatch]:
        watches = self.store.list_watches(limit=1000)
        expired = self.watch_engine.expire(watches)
        for watch in expired:
            self.store.save_watch(watch)
        return expired

    async def _evaluate_watches(
        self,
        snapshot: SceneSnapshot,
        events: list[VisualEvent],
        *,
        frame: VisionFrame | None = None,
    ) -> list[VisualEvent]:
        triggered: list[VisualEvent] = []
        self._expire_watches()
        watches = self.store.list_watches(source_id=snapshot.source_id, limit=1000)
        for watch in watches:
            old_status = watch.status
            result = self.watch_engine.evaluate_watch(watch, events=events, snapshot=snapshot, now=snapshot.captured_at)
            if result.status != old_status:
                self.store.save_watch(watch)
            trigger = result.event
            if trigger is None and result.reason == "condition requires semantic evaluation" and events and self.semantic_watch_callback is not None:
                trigger = await self._semantic_watch_event(watch, snapshot, events, frame=frame)
                if trigger is not None:
                    watch.status = "completed"
                    self.store.save_watch(watch)
            if trigger is None:
                continue
            # A watch event must own its artifact rather than sharing the
            # source scene event's file (deleting either event stays safe).
            trigger = trigger.model_copy(update={"frame_reference": None})
            if frame is not None:
                trigger = self._retain_event_frame(trigger, frame)
            trigger = self._record_event(trigger)
            triggered.append(trigger)
            self._record_action("vision_watch_completed", watch.watch_id, "A visual watch condition was met.")
            if watch.remember_event and self.store.get_permission(watch.source_id)["remember_allowed"]:
                with _suppress_errors():
                    self.remember_event(trigger.event_id, approved=True)
            if watch.notify and self.notifier is not None:
                with _suppress_errors():
                    await _await_maybe(self.notifier(trigger, watch))
        return triggered

    async def _semantic_watch_event(
        self,
        watch: VisionWatch,
        snapshot: SceneSnapshot,
        events: list[VisualEvent],
        *,
        frame: VisionFrame | None = None,
    ) -> VisualEvent | None:
        if self.semantic_watch_callback is None:
            return None
        try:
            try:
                raw = await _await_maybe(self.semantic_watch_callback(watch, snapshot, events, frame))
            except TypeError:
                raw = await _await_maybe(self.semantic_watch_callback(watch, snapshot, events))
        except Exception:
            return None
        if isinstance(raw, dict):
            matched = bool(raw.get("matched"))
            confidence = float(raw.get("confidence", 0.0))
            evidence = raw.get("evidence", [])
        else:
            matched = bool(raw)
            confidence = 0.8 if matched else 0.0
            evidence = []
        if not matched or confidence < 0.80:
            return None
        return make_visual_event(
            event_type="watch_condition_met",
            source_id=watch.source_id,
            subject=watch.target_labels[0] if watch.target_labels else None,
            description=f"Watch condition met: {watch.condition_text}",
            confidence=min(1.0, confidence),
            previous_state={"watch_id": watch.watch_id, "condition_type": "semantic"},
            current_state={"watch_id": watch.watch_id, "evidence": evidence},
            occurred_at=snapshot.captured_at,
        )

    # -- Comparison, verification, memory and deletion ----------------------

    def compare(
        self,
        *,
        source_id: str,
        compare_with: str = "latest",
        snapshot_id: str | None = None,
    ) -> dict[str, Any]:
        snapshots = self.store.list_snapshots(source_id, limit=3)
        current = self.store.get_snapshot(snapshot_id) if snapshot_id else (snapshots[0] if snapshots else None)
        if current is None:
            raise ValueError("No current visual snapshot is available.")
        if compare_with == "latest":
            reference = snapshots[1] if len(snapshots) > 1 else None
        else:
            reference = self.store.get_snapshot(compare_with)
        if reference is None:
            raise ValueError("No previous visual snapshot is available for comparison.")
        events = SceneDiffer(duplicate_window_seconds=0).diff(reference, current)
        return {
            "source_id": source_id,
            "current_snapshot_id": current.snapshot_id,
            "reference_snapshot_id": reference.snapshot_id,
            "changed": bool(events),
            "events": [visual_event_public_dict(event) for event in events],
            "summary": "No meaningful scene change detected." if not events else "; ".join(event.description for event in events),
        }

    async def verify(
        self,
        *,
        expected_result: str,
        source_id: str | None = None,
        source: VisionSourceType | str = VisionSourceType.IMAGE,
        reference_snapshot_id: str | None = None,
        image_path: str | Path | None = None,
    ) -> VerificationResult:
        _assert_supported_visual_request(expected_result)
        reasoning_frame: VisionFrame | None = None
        if image_path is not None:
            effective_source_id = source_id or "verification"
            self.create_source(
                source_type=VisionSourceType.IMAGE,
                source_id=effective_source_id,
                grant_observe=True,
            )
            reasoning_frame = await self._capture_image(image_path, source_id=effective_source_id)
            observed = await self.process_frame(reasoning_frame, include_ocr=True, force=True)
            snapshot = observed.snapshot
        else:
            if not source_id:
                raise ValueError("source_id or image_path is required for visual verification.")
            snapshot = self.store.latest_snapshot(source_id)
            if snapshot is None:
                observed = await self.observe(source=source, source_id=source_id)
                snapshot = observed.snapshot
            runtime = self._runtime.get(source_id)
            if runtime is not None and runtime.latest_image is not None:
                source_record = self.store.get_source(source_id)
                reasoning_frame = VisionFrame(
                    source_id=source_id,
                    source_type=source_record.source_type if source_record else self._source_type(source),
                    captured_at=snapshot.captured_at,
                    image=runtime.latest_image,
                )
        reference = self.store.get_snapshot(reference_snapshot_id) if reference_snapshot_id else None
        result = await self.verifier.verify(expected_result, snapshot, reference, frame=reasoning_frame)
        event = make_visual_event(
            event_type="verification_passed" if result.status.value == "passed" else "verification_failed" if result.status.value == "failed" else "verification_uncertain",
            source_id=snapshot.source_id,
            subject=None,
            description=(result.evidence[0] if result.evidence else f"Verification is {result.status.value}."),
            confidence=result.confidence,
            previous_state={"expected_result": expected_result, "reference_snapshot_id": reference_snapshot_id},
            current_state=result.model_dump(mode="json"),
            occurred_at=snapshot.captured_at,
        )
        self._record_event(self._retain_event_frame(event, reasoning_frame) if reasoning_frame is not None else event)
        if result.status.value == "passed" and result.confidence >= 0.80 and self.goal_suggestion_callback is not None:
            with _suppress_errors():
                await _await_maybe(self.goal_suggestion_callback(event))
        return result

    def remember_event(
        self,
        event_id: str,
        *,
        approved: bool,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        event = self.store.get_event(event_id)
        if event is None:
            raise ValueError(f"Visual event '{event_id}' was not found.")
        if not self.store.get_permission(event.source_id)["remember_allowed"]:
            if not approved:
                raise PermissionError("Saving visual evidence requires explicit memory permission.")
            self.store.set_permission(event.source_id, remember_allowed=True)
            self.privacy.grant_memory(event.source_id)
        self.privacy.assert_memory_allowed(event.source_id)
        if self.visual_memory is None:
            raise RuntimeError("Ares memory storage is unavailable.")
        result = self.visual_memory.remember(event, approved=approved, session_id=session_id)
        self.store.mark_event_remembered(event_id, True)
        self._record_action("vision_memory_saved", event_id, "Saved an approved visual memory.")
        return result

    def forget_memory_link(self, fact_id: int) -> dict[str, Any]:
        """Remove retained visual frames after the ordinary memory was deleted."""
        if self.visual_memory is None:
            return {"fact_id": int(fact_id), "deleted_frame_references": []}
        return self.visual_memory.forget_memory(int(fact_id), delete_memory=False)

    def delete_memory_frame(self, fact_id: int) -> dict[str, Any]:
        if self.visual_memory is None:
            return {
                "fact_id": int(fact_id),
                "deleted_frame_references": self.store.delete_frame_references_for_memory(int(fact_id)),
            }
        result = self.visual_memory.delete_memory_frame(int(fact_id))
        self._record_action("vision_memory_frame_deleted", str(fact_id), "Deleted a retained visual memory frame.")
        return result

    def delete_event(self, event_id: str) -> bool:
        # Event erasure is a privacy request: remove every ordinary visual
        # memory it created as well as the metadata and owned frame.
        if self.memory_store is not None:
            for fact_id in self.store.memory_fact_ids_for_event(event_id):
                with _suppress_errors():
                    self.memory_store.delete(fact_id)
        return self.store.delete_event(event_id, delete_frame=True)

    def erase_recent_events(self, *, minutes: float = 60.0, source_id: str | None = None) -> int:
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=max(0.0, float(minutes)))
        event_ids = self.store.event_ids_since(cutoff, source_id=source_id)
        return sum(1 for event_id in event_ids if self.delete_event(event_id))

    def list_events(self, *, source_id: str | None = None, limit: int = 100) -> list[VisualEvent]:
        return self.store.list_events(source_id=source_id, limit=limit)

    # -- Internals -----------------------------------------------------------

    def _record_event(self, event: VisualEvent) -> VisualEvent:
        prepared = self.privacy.prepare_event_for_storage(event)
        self.store.save_event(prepared)
        self.event_bus.publish(prepared)
        return prepared

    def _record_action(self, action_type: str, target: str, summary: str) -> None:
        if self.action_ledger is None:
            return
        try:
            self.action_ledger.record(
                action_type,
                target=target,
                summary=summary,
                tool_name="vision",
                tags=["vision"],
            )
        except Exception:
            pass

    async def shutdown(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self.stop_all_sources()
        self.event_bus.close()
        if self.visual_memory is not None:
            self.visual_memory.close()
        self.store.close()

    def close(self) -> None:
        """Close sources synchronously when possible, or schedule safe cleanup.

        The normal Agent shutdown awaits :meth:`shutdown`.  This compatibility
        method is still used by synchronous callers and must release a camera
        rather than merely cancelling its worker and closing SQLite beneath it.
        """
        if self._closed:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            asyncio.run(self.shutdown())
            return
        if self._shutdown_task is None or self._shutdown_task.done():
            self._shutdown_task = loop.create_task(self.shutdown(), name="ares-vision-shutdown")


class _suppress_errors:
    def __enter__(self) -> None:
        return None

    def __exit__(self, _type: Any, _value: Any, _traceback: Any) -> bool:
        return True


__all__ = ["VisionObservation", "VisionService"]
