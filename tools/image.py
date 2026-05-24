import base64
import os
import subprocess
import sys
import time
import urllib.parse
import warnings
from datetime import datetime
from pathlib import Path

import httpx

warnings.filterwarnings("ignore", message="Unverified HTTPS request")

from config import settings
from tools.registry import tool
from tools.runtime import (
    coerce_bool,
    emit_trace,
    error_payload,
    make_trace,
    normalize_int,
    normalize_response_format,
    structured_error,
    structured_success,
    utc_now_iso,
)

_IMAGE_VERSION = "2.0.0"
_IMAGE_DIR = Path(settings.images_dir).expanduser()
if not _IMAGE_DIR.is_absolute():
    _IMAGE_DIR = Path(__file__).resolve().parent.parent / _IMAGE_DIR
_VALID_SIZES = {"1024x1024", "1216x832", "832x1216"}
_DEFAULT_MODEL = "pollinations"
_VALID_MODELS = {"pollinations", "sdxl", "playground-v2.5", "sdxl-turbo"}
_GALLERY_ACTIONS = ("list", "search", "view", "remove")
_POLLINATIONS_BASE = "https://image.pollinations.ai/prompt"


def _image_trace(tool_name: str, started_at: str, started: float, inputs_received: int, path: str, status: str, fields: int, external: int = 0, error_code: str | None = None) -> dict:
    return make_trace(
        tool_name,
        _IMAGE_VERSION,
        started_at,
        started,
        inputs_received,
        True,
        path,
        status,
        fields,
        {"count": external, "systems": ["image_provider"] if external else []},
        error_code,
    )


def _image_error(tool_name: str, error: dict, response_format: str, trace_enabled: bool, started: float, started_at: str, inputs_received: int, legacy: str, path: str = "validate", external: int = 0):
    trace = _image_trace(tool_name, started_at, started, inputs_received, path, "FAILED", 1, external, error["code"])
    emit_trace(trace, trace_enabled)
    if response_format == "structured":
        return structured_error(tool_name, _IMAGE_VERSION, error, started, trace)
    return legacy


def _image_success(tool_name: str, result: dict, response_format: str, trace_enabled: bool, started: float, started_at: str, inputs_received: int, legacy: str, path: str, status: str = "SUCCESS", external: int = 0):
    trace = _image_trace(tool_name, started_at, started, inputs_received, path, status, len(result), external)
    emit_trace(trace, trace_enabled)
    if response_format == "structured":
        return structured_success(tool_name, _IMAGE_VERSION, result, started, trace)
    return legacy


def _pollinations_genai(prompt: str, width: int, height: int) -> tuple[str | None, str]:
    encoded = urllib.parse.quote(prompt)
    url = f"{_POLLINATIONS_BASE}/{encoded}?width={width}&height={height}&seed={int(time.time()) % 100000}&nologo=true"
    try:
        r = httpx.get(url, timeout=120, verify=False)
        if r.status_code == 200 and len(r.content) > 1000:
            return base64.b64encode(r.content).decode(), ""
        return None, f"http {r.status_code}"
    except httpx.TimeoutException:
        return None, "timeout"
    except httpx.HTTPError as e:
        return None, e.__class__.__name__


def _nvidia_genai(prompt: str, width: int, height: int, model: str) -> tuple[str | None, str]:
    endpoints = {
        "sdxl": ["https://integrate.api.nvidia.com/v1/genai/stabilityai/stable-diffusion-xl-base-1.0"],
        "playground-v2.5": ["https://integrate.api.nvidia.com/v1/genai/playgroundai/playground-v2.5-1024px-aesthetic"],
        "sdxl-turbo": ["https://integrate.api.nvidia.com/v1/genai/stabilityai/sdxl-turbo"],
    }
    urls = endpoints.get(model, [])
    if not urls:
        return None, "unsupported model"
    headers = {
        "Authorization": f"Bearer {settings.nim_api_key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    payload = {
        "text_prompts": [{"text": prompt, "weight": 1}],
        "height": height,
        "width": width,
        "samples": 1,
        "steps": 25,
        "cfg_scale": 7,
    }
    last_error = "provider unavailable"
    for url in urls:
        try:
            r = httpx.post(url, headers=headers, json=payload, timeout=120, verify=False)
        except httpx.TimeoutException:
            last_error = "timeout"
            continue
        except httpx.HTTPError as e:
            last_error = e.__class__.__name__
            continue
        if r.status_code in (200, 201):
            b64 = _extract_b64_from_nvidia(r)
            if b64:
                return b64, ""
            last_error = "empty response"
            continue
        if r.status_code == 202:
            poll_url = r.headers.get("Location") or r.headers.get("Content-Location")
            if poll_url:
                for _ in range(30):
                    time.sleep(2)
                    try:
                        pr = httpx.get(
                            poll_url,
                            headers={"Authorization": f"Bearer {settings.nim_api_key}", "Accept": "application/json"},
                            timeout=30,
                            verify=False,
                        )
                    except httpx.HTTPError as e:
                        last_error = e.__class__.__name__
                        continue
                    if pr.status_code == 200:
                        b64 = _extract_b64_from_nvidia(pr)
                        if b64:
                            return b64, ""
        last_error = f"http {r.status_code}"
    return None, last_error


def _extract_b64_from_nvidia(r) -> str | None:
    try:
        body = r.json()
    except Exception:
        return None
    artifacts = body.get("artifacts")
    if artifacts and len(artifacts) > 0:
        b64 = artifacts[0].get("base64")
        if b64:
            return b64
    for key in ("base64", "image", "data"):
        val = body.get(key)
        if val:
            return val
    img_url = body.get("url") or body.get("image_url")
    if img_url:
        try:
            ir = httpx.get(img_url, timeout=30, verify=False)
            ir.raise_for_status()
            return base64.b64encode(ir.content).decode()
        except httpx.HTTPError:
            pass
    return None


def _generate(prompt: str, width: int, height: int, model: str) -> tuple[str | None, str, str]:
    providers = []
    if model == "pollinations":
        result, err = _pollinations_genai(prompt, width, height)
        providers.append(("pollinations", err))
        if result:
            return result, "pollinations", ""
        alt = "playground-v2.5" if settings.nim_api_key else "pollinations"
        if settings.nim_api_key:
            result, err = _nvidia_genai(prompt, width, height, alt)
            providers.append((alt, err))
            if result:
                return result, alt, ""
        result, err = _pollinations_genai(prompt, width, height)
        providers.append(("pollinations_retry", err))
        if result:
            return result, "pollinations", ""
        return None, "", " | ".join(f"{name}:{detail}" for name, detail in providers if detail)

    result, err = _nvidia_genai(prompt, width, height, model)
    providers.append((model, err))
    if result:
        return result, model, ""
    alt = "playground-v2.5" if model in ("sdxl", "sdxl-turbo") else "sdxl"
    result, err = _nvidia_genai(prompt, width, height, alt)
    providers.append((alt, err))
    if result:
        return result, alt, ""
    result, err = _pollinations_genai(prompt, width, height)
    providers.append(("pollinations", err))
    if result:
        return result, "pollinations", ""
    return None, "", " | ".join(f"{name}:{detail}" for name, detail in providers if detail)


def _save_image(b64_data: str, prompt_slug: str) -> Path:
    _IMAGE_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    slug = "".join(c if c.isalnum() or c in "_-" else "_" for c in prompt_slug[:20].strip())
    name = f"imagine_{ts}_{slug}.png" if slug else f"imagine_{ts}.png"
    path = _IMAGE_DIR / name
    path.write_bytes(base64.b64decode(b64_data))
    return path


def _open_image(path: Path) -> bool:
    resolved = str(path.resolve())
    try:
        if sys.platform == "win32":
            os.startfile(resolved)
        elif sys.platform == "darwin":
            subprocess.run(["open", resolved], capture_output=True, timeout=10)
        else:
            subprocess.run(["xdg-open", resolved], capture_output=True, timeout=10)
        return True
    except (OSError, subprocess.SubprocessError):
        return False


def _find_image(name: str) -> str | None:
    import difflib

    if not _IMAGE_DIR.exists():
        return None
    files = sorted(_IMAGE_DIR.iterdir())
    if not files:
        return None
    if name.isdigit():
        idx = int(name) - 1
        if 0 <= idx < len(files):
            return str(files[idx].resolve())
    name_lower = name.lower()
    for f in files:
        if name_lower in f.stem.lower():
            return str(f.resolve())
    best = None
    best_ratio = 0.0
    for f in files:
        ratio = difflib.SequenceMatcher(None, name_lower, f.stem.lower()).ratio()
        if ratio > best_ratio:
            best_ratio = ratio
            best = f
    if best_ratio >= 0.35:
        return str(best.resolve())
    return None


@tool(
    name="imagine",
    description="Generate an image from a text description. Uses Pollinations.ai (free, no API key needed) with NVIDIA NIM fallback. Keywords: image, picture, photo, img, art, drawing, illustration, render, create, make, generate, draw, imagine, paint, sketch, visual. This is the ONLY tool for creating new images",
    examples=[
        "generate an image of a cyberpunk cat",
        "imagine a mountain landscape at sunset digital art",
        "create a logo for my startup a blue geometric fox",
    ],
    param_descriptions={
        "prompt": "Text description of the image to generate (5+ characters)",
        "size": "Image size: 1024x1024 (square, default), 1216x832 (landscape), or 832x1216 (portrait)",
        "model": "Model: pollinations (default, free), sdxl, playground-v2.5, or sdxl-turbo",
        "open_viewer": "When true, open the saved image with the system viewer",
        "response_format": "legacy or structured",
        "trace_enabled": "Emit machine-readable trace when true",
    },
)
def imagine(
    prompt: str,
    size: str = "1024x1024",
    model: str = _DEFAULT_MODEL,
    open_viewer: bool = True,
    response_format: str = "legacy",
    trace_enabled: bool = False,
):
    started = time.perf_counter()
    started_at = utc_now_iso()
    inputs_received = 6
    response_format = normalize_response_format(response_format)
    trace_enabled = coerce_bool(trace_enabled)
    open_viewer = coerce_bool(open_viewer)
    prompt = str(prompt or "").strip()
    if len(prompt) < 5:
        error = error_payload("SHORT_PROMPT", "prompt must be at least 5 characters.", "prompt", prompt, "descriptive prompt", False, "Add more visual detail to the prompt.")
        return _image_error("imagine", error, response_format, trace_enabled, started, started_at, inputs_received, "Provide a descriptive prompt (at least 5 characters)")
    size = str(size or "1024x1024").strip()
    if size not in _VALID_SIZES:
        error = error_payload("INVALID_SIZE", "size must be a supported image dimension.", "size", size, ", ".join(sorted(_VALID_SIZES)), False, "Use 1024x1024, 1216x832, or 832x1216.")
        return _image_error("imagine", error, response_format, trace_enabled, started, started_at, inputs_received, f"Invalid size '{size}'. Choose from: {', '.join(sorted(_VALID_SIZES))}")
    model = str(model or _DEFAULT_MODEL).strip().lower()
    if model not in _VALID_MODELS:
        error = error_payload("INVALID_MODEL", "model is not supported.", "model", model, ", ".join(sorted(_VALID_MODELS)), False, "Use pollinations or an NVIDIA model when configured.")
        return _image_error("imagine", error, response_format, trace_enabled, started, started_at, inputs_received, f"Invalid model '{model}'")
    parts = size.split("x")
    width, height = int(parts[0]), int(parts[1])
    b64, provider_used, provider_detail = _generate(prompt, width, height, model)
    if not b64:
        error = error_payload("GENERATION_FAILED", "All image generation backends failed.", "provider", provider_detail, "image bytes", True, "Retry later or switch model to pollinations.")
        legacy = "All image generation backends failed. Check API key or try again later."
        if provider_detail:
            legacy = f"All image generation backends failed. Provider status: {provider_detail}"
        return _image_error("imagine", error, response_format, trace_enabled, started, started_at, inputs_received, legacy, "generate", 1)
    try:
        path = _save_image(b64, prompt)
        opened = _open_image(path) if open_viewer else False
        result = {
            "path": str(path.resolve()),
            "size": size,
            "model_requested": model,
            "provider_used": provider_used,
            "opened_viewer": opened,
            "bytes": path.stat().st_size,
        }
        legacy = f"Image saved: {path.resolve()}"
        return _image_success("imagine", result, response_format, trace_enabled, started, started_at, inputs_received, legacy, "generate", "SUCCESS", 1)
    except OSError:
        error = error_payload("SAVE_FAILED", "Failed to save generated image.", "path", str(_IMAGE_DIR), "writable image directory", True, "Check storage permissions.")
        return _image_error("imagine", error, response_format, trace_enabled, started, started_at, inputs_received, "Failed to save generated image", "save", 1)


@tool(
    name="gallery",
    description="Browse and manage your gallery of previously generated saved images: list all, search by name, view file path, or delete.",
    examples=[
        "show my saved images",
        "list my image gallery",
        "find images about cyberpunk",
        "delete image 3",
    ],
    param_descriptions={
        "action": "list, search, view, or remove",
        "query": "Image name or number for search/remove/view",
        "limit": "Maximum items to return for list or search, from 1 to 200",
        "response_format": "legacy or structured",
        "trace_enabled": "Emit machine-readable trace when true",
    },
)
def images_manage(
    action: str,
    query: str = "",
    limit: int = 100,
    response_format: str = "legacy",
    trace_enabled: bool = False,
):
    started = time.perf_counter()
    started_at = utc_now_iso()
    inputs_received = 5
    response_format = normalize_response_format(response_format)
    trace_enabled = coerce_bool(trace_enabled)
    action = str(action or "").strip().lower()
    if action not in _GALLERY_ACTIONS:
        error = error_payload("INVALID_ACTION", "action must be list, search, view, or remove.", "action", action, "list, search, view, remove", False, "Use a supported gallery action.")
        return _image_error("gallery", error, response_format, trace_enabled, started, started_at, inputs_received, f"Unknown action '{action}'. Available: list, search, view, remove")
    limit, limit_error = normalize_int(limit, "limit", 100, 1, 200, "Use limit between 1 and 200.", "INVALID_LIMIT")
    if limit_error is not None:
        return _image_error("gallery", limit_error, response_format, trace_enabled, started, started_at, inputs_received, "Error: invalid limit")
    _IMAGE_DIR.mkdir(parents=True, exist_ok=True)
    files = sorted(f for f in _IMAGE_DIR.iterdir() if f.is_file())

    def _item(path: Path, index: int) -> dict:
        size = path.stat().st_size
        return {"index": index, "name": path.name, "path": str(path.resolve()), "size_bytes": size}

    if action == "list":
        if not files:
            legacy = "No generated images yet"
            result = {"action": action, "items": [], "count": 0}
            return _image_success("gallery", result, response_format, trace_enabled, started, started_at, inputs_received, legacy, "list", "PARTIAL")
        items = [_item(f, i) for i, f in enumerate(files[:limit], 1)]
        lines = [f"{row['index']}. {row['name']}  ({row['size_bytes'] // 1024}KB)" for row in items]
        legacy = "\n".join(lines)
        result = {"action": action, "items": items, "count": len(items), "total": len(files), "truncated": len(files) > limit}
        return _image_success("gallery", result, response_format, trace_enabled, started, started_at, inputs_received, legacy, "list")

    if action == "search":
        query = str(query or "").strip()
        if not query:
            error = error_payload("EMPTY_QUERY", "query is required for search.", "query", query, "search term", False, "Pass a name fragment to search.")
            return _image_error("gallery", error, response_format, trace_enabled, started, started_at, inputs_received, "Provide a search term")
        q = query.lower()
        matches = [f for f in files if q in f.stem.lower()][:limit]
        if not matches:
            legacy = f"No images match '{query}'"
            result = {"action": action, "query": query, "items": [], "count": 0}
            return _image_success("gallery", result, response_format, trace_enabled, started, started_at, inputs_received, legacy, "search", "PARTIAL")
        items = [_item(f, i) for i, f in enumerate(matches, 1)]
        lines = [f"{row['index']}. {row['name']}" for row in items]
        legacy = "\n".join(lines)
        result = {"action": action, "query": query, "items": items, "count": len(items)}
        return _image_success("gallery", result, response_format, trace_enabled, started, started_at, inputs_received, legacy, "search")

    if action in ("view", "remove"):
        query = str(query or "").strip()
        if not query:
            error = error_payload("EMPTY_QUERY", "query is required for view/remove.", "query", query, "image name or number", False, "Pass an image name or index.")
            return _image_error("gallery", error, response_format, trace_enabled, started, started_at, inputs_received, f"Specify an image name or number to {action}")
        path_str = _find_image(query)
        if path_str is None:
            error = error_payload("IMAGE_NOT_FOUND", "No gallery image matched the query.", "query", query, "existing gallery image", False, "Use gallery list to see available images.")
            return _image_error("gallery", error, response_format, trace_enabled, started, started_at, inputs_received, f"Image '{query}' not found")
        if action == "remove":
            Path(path_str).unlink(missing_ok=True)
            legacy = f"Removed {Path(path_str).name}"
            result = {"action": action, "removed": Path(path_str).name, "path": path_str}
            return _image_success("gallery", result, response_format, trace_enabled, started, started_at, inputs_received, legacy, "remove")
        legacy = path_str
        result = {"action": action, "path": path_str}
        return _image_success("gallery", result, response_format, trace_enabled, started, started_at, inputs_received, legacy, "view")

    return _image_error(
        "gallery",
        error_payload("INVALID_ACTION", "Unsupported gallery action.", "action", action, "list, search, view, remove", False, "Use a supported action."),
        response_format,
        trace_enabled,
        started,
        started_at,
        inputs_received,
        f"Unknown action '{action}'",
    )
