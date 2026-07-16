"""Local image, camera, and screen capture adapters.

Only Pillow is imported at module load because it is already a core Ares
dependency.  Hardware and screen integrations import OpenCV/MSS only inside a
requested start/capture operation, so ordinary Ares imports never need the
vision extra or a device permission.
"""

from __future__ import annotations

import asyncio
import mimetypes
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from PIL import Image

from .models import VisionFrame, VisionSourceType
from .providers.base import VisionDependencyError, VisionProviderError, require_optional_dependency


class VisionCaptureError(RuntimeError):
    """A requested local visual source cannot produce a frame."""


@runtime_checkable
class VisionCapture(Protocol):
    """Minimal source lifecycle contract consumed by the future service layer."""

    async def start(self) -> None: ...

    async def capture(self, *args: Any, **kwargs: Any) -> VisionFrame: ...

    async def stop(self, source_id: str | None = None) -> None: ...

    async def close(self) -> None: ...


def _content_type_for_path(path: Path) -> str:
    return mimetypes.guess_type(path.name)[0] or "image/png"


class ImageCapture:
    """Capture a user-provided image file or in-memory Pillow image.

    The source path is intentionally not stored on the frame.  It is only read
    to create a transient RGB/RGBA image that can flow through detection/OCR.
    """

    async def start(self) -> None:
        """Image inputs have no persistent resource to start."""

    async def stop(self, source_id: str | None = None) -> None:
        """Image inputs have no persistent resource to stop."""

    async def close(self) -> None:
        await self.stop()

    async def capture(
        self,
        image_or_path: Image.Image | str | Path,
        *,
        source_id: str = "image",
        source_type: VisionSourceType | str | None = None,
        content_type: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> VisionFrame:
        """Return an in-memory frame from a file path or Pillow image."""

        if source_type is not None and VisionSourceType(source_type) is not VisionSourceType.IMAGE:
            raise VisionCaptureError("ImageCapture only supports an image source type")
        if isinstance(image_or_path, Image.Image):
            image = image_or_path.copy()
            image.load()
            return VisionFrame(
                source_id=source_id,
                source_type=VisionSourceType.IMAGE,
                image=image,
                content_type=content_type or "image/png",
                metadata=dict(metadata or {}),
            )
        path = Path(image_or_path).expanduser()
        return await asyncio.to_thread(
            self._capture_path,
            path,
            source_id,
            content_type,
            dict(metadata or {}),
        )

    @staticmethod
    def _capture_path(
        path: Path,
        source_id: str,
        content_type: str | None,
        metadata: dict[str, Any],
    ) -> VisionFrame:
        resolved = path.resolve()
        if not resolved.is_file():
            raise VisionCaptureError(f"Image source does not exist or is not a file: {resolved}")
        try:
            with Image.open(resolved) as opened:
                opened.load()
                # A standalone copy keeps no open file descriptor alive after
                # capture and avoids storing a raw path in metadata.
                image = opened.copy()
        except (OSError, ValueError) as exc:
            raise VisionCaptureError(f"Could not decode image '{resolved.name}': {exc}") from exc
        return VisionFrame(
            source_id=source_id,
            source_type=VisionSourceType.IMAGE,
            image=image,
            content_type=content_type or _content_type_for_path(resolved),
            metadata=metadata,
        )

    capture_image = capture


class CameraCapture:
    """An opt-in, lazily imported OpenCV webcam source.

    ``start`` opens the camera but does not retain any footage.  ``capture``
    creates one transient frame; callers should call ``stop``/``close`` when a
    source is no longer visibly active.
    """

    def __init__(
        self,
        camera_index: int = 0,
        *,
        source_id: str = "camera",
        **_source_config: Any,
    ) -> None:
        self.camera_index = int(camera_index)
        self.source_id = str(source_id)
        self._camera: Any = None
        self._lock = asyncio.Lock()

    @property
    def started(self) -> bool:
        return self._camera is not None

    async def start(self) -> None:
        async with self._lock:
            if self._camera is not None:
                return
            self._camera = await asyncio.to_thread(self._open_camera)

    def _open_camera(self) -> Any:
        cv2 = require_optional_dependency("cv2", package_name="opencv-python-headless")
        camera = cv2.VideoCapture(self.camera_index)
        if not camera or not camera.isOpened():
            try:
                if camera:
                    camera.release()
            finally:
                raise VisionCaptureError(
                    f"Could not open camera index {self.camera_index}. "
                    "Check device access and grant camera permission."
                )
        return camera

    async def capture(
        self,
        *,
        source_id: str | None = None,
        source_type: VisionSourceType | str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> VisionFrame:
        if source_type is not None and VisionSourceType(source_type) is not VisionSourceType.CAMERA:
            raise VisionCaptureError("CameraCapture only supports a camera source type")
        await self.start()
        async with self._lock:
            if self._camera is None:
                raise VisionCaptureError("Camera is not running")
            image = await asyncio.to_thread(self._read_camera, self._camera)
        return VisionFrame(
            source_id=source_id or self.source_id,
            source_type=VisionSourceType.CAMERA,
            image=image,
            content_type="image/bgr",
            metadata=dict(metadata or {}),
        )

    @staticmethod
    def _read_camera(camera: Any) -> Any:
        try:
            ok, image = camera.read()
        except Exception as exc:
            raise VisionCaptureError(f"Could not read camera frame: {exc}") from exc
        if not ok or image is None:
            raise VisionCaptureError("Could not read a frame from the camera")
        return image

    async def stop(self, source_id: str | None = None) -> None:
        async with self._lock:
            camera, self._camera = self._camera, None
            if camera is not None:
                await asyncio.to_thread(self._release_camera, camera)

    @staticmethod
    def _release_camera(camera: Any) -> None:
        try:
            camera.release()
        except Exception:
            # Shutdown should remain idempotent even after device disconnect.
            pass

    async def close(self) -> None:
        await self.stop()


class ScreenCapture:
    """An opt-in, lazily imported one-frame MSS screen source."""

    def __init__(
        self,
        monitor: int = 1,
        *,
        source_id: str = "screen",
        **_source_config: Any,
    ) -> None:
        self.monitor = int(monitor)
        self.source_id = str(source_id)
        self._started = False
        self._lock = asyncio.Lock()

    @property
    def started(self) -> bool:
        return self._started

    async def start(self) -> None:
        # Validate the optional package now so callers receive a useful error
        # before claiming a screen indicator is active.
        await asyncio.to_thread(require_optional_dependency, "mss", package_name="mss")
        async with self._lock:
            self._started = True

    async def capture(
        self,
        *,
        source_id: str | None = None,
        source_type: VisionSourceType | str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> VisionFrame:
        if source_type is not None and VisionSourceType(source_type) is not VisionSourceType.SCREEN:
            raise VisionCaptureError("ScreenCapture only supports a screen source type")
        await self.start()
        async with self._lock:
            if not self._started:
                raise VisionCaptureError("Screen capture is not running")
            image = await asyncio.to_thread(self._grab_screen, self.monitor)
        return VisionFrame(
            source_id=source_id or self.source_id,
            source_type=VisionSourceType.SCREEN,
            image=image,
            content_type="image/bgra",
            metadata=dict(metadata or {}),
        )

    @staticmethod
    def _grab_screen(monitor: int) -> Any:
        mss_module = require_optional_dependency("mss", package_name="mss")
        try:
            import numpy as np

            with mss_module.mss() as client:
                monitors = client.monitors
                if monitor < 0 or monitor >= len(monitors):
                    raise VisionCaptureError(
                        f"Screen monitor {monitor} is unavailable; choose 0 through {len(monitors) - 1}."
                    )
                # mss returns BGRA; keeping it in memory retains no screenshot
                # once the caller clears/releases the VisionFrame.
                return np.asarray(client.grab(monitors[monitor])).copy()
        except VisionCaptureError:
            raise
        except Exception as exc:
            raise VisionCaptureError(f"Could not capture screen monitor {monitor}: {exc}") from exc

    async def stop(self, source_id: str | None = None) -> None:
        async with self._lock:
            self._started = False

    async def close(self) -> None:
        await self.stop()


async def capture_image(
    image_or_path: Image.Image | str | Path,
    *,
    source_id: str = "image",
    content_type: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> VisionFrame:
    """Convenience one-shot image capture for image-file observation."""

    return await ImageCapture().capture(
        image_or_path,
        source_id=source_id,
        source_type=VisionSourceType.IMAGE,
        content_type=content_type,
        metadata=metadata,
    )


# Compatibility-friendly names for integrations that phrase capture in source
# rather than adapter terms.
ImageFileCapture = ImageCapture
OpenCVCameraCapture = CameraCapture
MSSScreenCapture = ScreenCapture


__all__ = [
    "CameraCapture",
    "ImageCapture",
    "ImageFileCapture",
    "MSSScreenCapture",
    "OpenCVCameraCapture",
    "ScreenCapture",
    "VisionCapture",
    "VisionCaptureError",
    "capture_image",
]
