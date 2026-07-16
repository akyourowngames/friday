"""Local-first computer-vision foundations for Ares.

This package is intentionally safe to import without ``ares[vision]``.  The
optional CV runtimes are imported only when a camera/screen/model provider is
actually used.
"""

from .capture import (
    CameraCapture,
    ImageCapture,
    ImageFileCapture,
    MSSScreenCapture,
    OpenCVCameraCapture,
    ScreenCapture,
    VisionCapture,
    VisionCaptureError,
    capture_image,
)
from .detector import (
    DetectorUnavailableError,
    UnavailableVisionDetector,
    VisionDetector,
    create_default_detector,
    filter_detections_by_prompts,
)
from .models import (
    DetectedObject,
    SceneSnapshot,
    TrackedEntity,
    VerificationResult,
    VerificationStatus,
    VisionFrame,
    VisionSource,
    VisionSourceType,
    VisionWatch,
    VisualEvent,
)
from .ocr import (
    EasyOCRReader,
    NullOCR,
    OCRUnavailableError,
    PaddleOCRProvider,
    PaddleOCRReader,
    VisionOCR,
    create_default_ocr,
)
from .privacy import (
    VisionPermissionController,
    VisionPermissionError,
    VisionPrivacyConfig,
    VisionPrivacyError,
    VisionPrivacyManager,
    redact_sensitive_text,
)
from .providers.base import VisionDependencyError, VisionProviderError

__all__ = [
    "CameraCapture",
    "DetectedObject",
    "EasyOCRReader",
    "DetectorUnavailableError",
    "ImageCapture",
    "ImageFileCapture",
    "MSSScreenCapture",
    "NullOCR",
    "OCRUnavailableError",
    "OpenCVCameraCapture",
    "PaddleOCRProvider",
    "PaddleOCRReader",
    "SceneSnapshot",
    "ScreenCapture",
    "TrackedEntity",
    "UnavailableVisionDetector",
    "VerificationResult",
    "VerificationStatus",
    "VisionCapture",
    "VisionCaptureError",
    "VisionDependencyError",
    "VisionDetector",
    "VisionFrame",
    "VisionOCR",
    "VisionPermissionController",
    "VisionPermissionError",
    "VisionPrivacyConfig",
    "VisionPrivacyError",
    "VisionPrivacyManager",
    "VisionProviderError",
    "VisionSource",
    "VisionSourceType",
    "VisionWatch",
    "VisualEvent",
    "capture_image",
    "create_default_detector",
    "create_default_ocr",
    "filter_detections_by_prompts",
    "redact_sensitive_text",
]
