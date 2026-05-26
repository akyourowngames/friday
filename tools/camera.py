import base64
import binascii
import time

import httpx

from config import settings
from tools.registry import tool
from tools.runtime import (
    coerce_bool,
    emit_trace,
    error_payload,
    make_trace,
    normalize_response_format,
    normalize_timeout_ms,
    structured_error,
    structured_success,
    utc_now_iso,
)


_CAMERA_VERSION = "1.0.0"
_SUPPORTED_MIME_TYPES = {"image/jpeg", "image/png", "image/webp"}


def _camera_trace(
    started_at: str,
    started: float,
    inputs_received: int,
    path: str,
    status: str,
    fields: int,
    external: int = 0,
    error_code: str | None = None,
) -> dict:
    return make_trace(
        "camera_vision",
        _CAMERA_VERSION,
        started_at,
        started,
        inputs_received,
        True,
        path,
        status,
        fields,
        {"count": external, "systems": ["nvidia_nim"] if external else []},
        error_code,
    )


def _camera_error(
    error: dict,
    response_format: str,
    trace_enabled: bool,
    started: float,
    started_at: str,
    inputs_received: int,
    legacy: str,
    path: str = "validate",
    external: int = 0,
):
    trace = _camera_trace(started_at, started, inputs_received, path, "FAILED", 1, external, error["code"])
    emit_trace(trace, trace_enabled)
    if response_format == "structured":
        return structured_error("camera_vision", _CAMERA_VERSION, error, started, trace)
    return legacy


def _camera_success(
    result: dict,
    response_format: str,
    trace_enabled: bool,
    started: float,
    started_at: str,
    inputs_received: int,
    external: int,
):
    trace = _camera_trace(started_at, started, inputs_received, "vision_provider", "SUCCESS", len(result), external)
    emit_trace(trace, trace_enabled)
    if response_format == "structured":
        return structured_success("camera_vision", _CAMERA_VERSION, result, started, trace)
    return str(result.get("description") or "Camera frame analyzed.")


def _candidate_models() -> list[str]:
    configured = [settings.camera_vision_model]
    configured.extend(settings.camera_vision_fallback_models.split(","))
    seen = set()
    models = []
    for item in configured:
        model = str(item or "").strip()
        if not model or model in seen:
            continue
        seen.add(model)
        models.append(model)
    return models


def _decode_image(image_base64: str, mime_type: str) -> tuple[str, str, bytes, dict | None]:
    raw = str(image_base64 or "").strip()
    mime = str(mime_type or "image/jpeg").strip().lower()
    if raw.startswith("data:"):
        header, separator, body = raw.partition(",")
        if not separator:
            return "", mime, b"", error_payload(
                "INVALID_DATA_URL",
                "image_base64 data URL is missing the comma separator.",
                "image_base64",
                "data URL without payload separator",
                "base64 image data",
                False,
                "Send a complete data URL or raw base64 payload.",
            )
        declared = header.removeprefix("data:").split(";", 1)[0].strip().lower()
        if declared:
            mime = declared
        raw = body
    compact = "".join(raw.split())
    if not compact:
        return "", mime, b"", error_payload(
            "MISSING_IMAGE",
            "image_base64 is required.",
            "image_base64",
            "",
            "base64 image data",
            False,
            "Capture a frame or upload an image before calling camera vision.",
        )
    if mime not in _SUPPORTED_MIME_TYPES:
        return "", mime, b"", error_payload(
            "UNSUPPORTED_MIME_TYPE",
            "mime_type is not supported.",
            "mime_type",
            mime,
            ", ".join(sorted(_SUPPORTED_MIME_TYPES)),
            False,
            "Use JPEG, PNG, or WebP image data.",
        )
    try:
        decoded = base64.b64decode(compact, validate=True)
    except (binascii.Error, ValueError):
        return "", mime, b"", error_payload(
            "INVALID_BASE64",
            "image_base64 could not be decoded.",
            "image_base64",
            "invalid base64 payload",
            "valid base64 image data",
            False,
            "Send the raw base64 section from a canvas data URL.",
        )
    if len(decoded) < 32:
        return "", mime, decoded, error_payload(
            "IMAGE_TOO_SMALL",
            "The decoded image is too small to analyze.",
            "image_base64",
            f"{len(decoded)} bytes",
            "camera frame image bytes",
            False,
            "Wait until the camera preview is ready, then capture another frame.",
        )
    if len(decoded) > settings.camera_max_image_bytes:
        return "", mime, decoded, error_payload(
            "IMAGE_TOO_LARGE",
            "The decoded image exceeds the configured camera payload limit.",
            "image_base64",
            f"{len(decoded)} bytes",
            f"at most {settings.camera_max_image_bytes} bytes",
            False,
            "Lower camera capture quality or increase KING_CAMERA_MAX_IMAGE_BYTES.",
        )
    return compact, mime, decoded, None


def _call_nim_vision(image_base64: str, mime_type: str, prompt: str, timeout_ms: int) -> tuple[str, str, list[dict]]:
    if not settings.nim_api_key.strip():
        return "", "", [{"model": "", "status": "missing_api_key", "detail": "NVIDIA_API_KEY is not configured"}]
    url = settings.nim_base_url.rstrip("/") + "/chat/completions"
    headers = {
        "Authorization": f"Bearer {settings.nim_api_key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    attempts = []
    for model in _candidate_models():
        payload = {
            "model": model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url", "image_url": {"url": f"data:{mime_type};base64,{image_base64}"}},
                    ],
                }
            ],
            "temperature": 0.1,
            "max_tokens": settings.camera_vision_max_tokens,
        }
        started = time.perf_counter()
        try:
            response = httpx.post(url, headers=headers, json=payload, timeout=timeout_ms / 1000)
        except httpx.TimeoutException:
            attempts.append({"model": model, "status": "timeout", "duration_ms": int((time.perf_counter() - started) * 1000)})
            continue
        except httpx.HTTPError as exc:
            attempts.append({"model": model, "status": exc.__class__.__name__, "duration_ms": int((time.perf_counter() - started) * 1000)})
            continue
        duration_ms = int((time.perf_counter() - started) * 1000)
        if response.status_code != 200:
            attempts.append({"model": model, "status": f"http_{response.status_code}", "duration_ms": duration_ms, "detail": response.text[:240]})
            continue
        try:
            body = response.json()
        except ValueError:
            attempts.append({"model": model, "status": "invalid_json", "duration_ms": duration_ms})
            continue
        choices = body.get("choices") if isinstance(body, dict) else None
        message = choices[0].get("message") if choices and isinstance(choices[0], dict) else {}
        content = str(message.get("content") or "").strip() if isinstance(message, dict) else ""
        attempts.append({"model": model, "status": "success", "duration_ms": duration_ms})
        if content:
            return content, model, attempts
    return "", "", attempts


@tool(
    name="camera_vision",
    description=(
        "Analyze a live camera frame or uploaded image with a NIM vision-language model. "
        "Use it when a user asks what is visible, what an object is, what text is present, "
        "or asks Jarvis to inspect the current camera view."
    ),
    examples=[
        "What do you see on my camera?",
        "What is this object?",
        "Read the text in this image",
        "Describe the current camera frame",
    ],
    param_descriptions={
        "image_base64": "Raw base64 image payload or a data:image/* URL.",
        "prompt": "Question or instruction for the vision model.",
        "mime_type": "Image MIME type: image/jpeg, image/png, or image/webp.",
        "timeout_ms": "Provider timeout in milliseconds.",
        "response_format": "legacy or structured.",
        "trace_enabled": "Emit machine-readable trace when true.",
    },
)
def camera_vision(
    image_base64: str,
    prompt: str = "Describe what is visible in this camera frame. If there is readable text, include it.",
    mime_type: str = "image/jpeg",
    timeout_ms: int = 0,
    response_format: str = "legacy",
    trace_enabled: bool = False,
):
    started = time.perf_counter()
    started_at = utc_now_iso()
    inputs_received = 6
    response_format = normalize_response_format(response_format)
    trace_enabled = coerce_bool(trace_enabled)
    timeout_value, timeout_error = normalize_timeout_ms(timeout_ms, settings.camera_default_timeout_ms)
    if timeout_error is not None:
        return _camera_error(timeout_error, response_format, trace_enabled, started, started_at, inputs_received, "Invalid camera timeout.")
    clean_prompt = str(prompt or "").strip() or "Describe what is visible in this camera frame."
    image_payload, detected_mime, decoded, decode_error = _decode_image(image_base64, mime_type)
    if decode_error is not None:
        return _camera_error(decode_error, response_format, trace_enabled, started, started_at, inputs_received, decode_error["message"])
    description, model_used, attempts = _call_nim_vision(image_payload, detected_mime, clean_prompt, int(timeout_value or settings.camera_default_timeout_ms))
    if not description:
        missing_key = attempts and attempts[0].get("status") == "missing_api_key"
        code = "MISSING_NIM_API_KEY" if missing_key else "VISION_PROVIDER_FAILED"
        message = "NVIDIA_API_KEY is not configured for camera vision." if missing_key else "No configured NIM vision model returned a usable description."
        suggestion = "Set NVIDIA_API_KEY in .env." if missing_key else "Retry, or set KING_CAMERA_VISION_MODEL to another available NIM vision model."
        error = error_payload(code, message, "provider", attempts, "vision model response", not missing_key, suggestion)
        return _camera_error(error, response_format, trace_enabled, started, started_at, inputs_received, message, "vision_provider", len(attempts))
    result = {
        "description": description,
        "transcript": description,
        "prompt": clean_prompt,
        "mime_type": detected_mime,
        "image_bytes": len(decoded),
        "provider": "nvidia_nim",
        "model": model_used,
        "models_tried": attempts,
        "captured_at": started_at,
        "source": "camera_frame",
    }
    return _camera_success(result, response_format, trace_enabled, started, started_at, inputs_received, len(attempts))
