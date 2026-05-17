import base64
import time
from datetime import datetime
from pathlib import Path

import httpx

from config import settings
from tools.registry import tool

_IMAGE_DIR = Path("storage/images")
_VALID_SIZES = {"1024x1024", "1216x832", "832x1216"}
_DEFAULT_MODEL = "sdxl"

_MODEL_ENDPOINTS = {
    "sdxl": "https://api.nvidia.com/v1/genai/stabilityai/sdxl",
    "playground-v2.5": "https://api.nvidia.com/v1/genai/playgroundai/playground-v2.5-1024px-aesthetic",
    "sdxl-turbo": "https://api.nvidia.com/v1/genai/stabilityai/sdxl-turbo",
}


def _nvidia_genai(prompt: str, width: int, height: int, model: str) -> str:
    endpoint = _MODEL_ENDPOINTS.get(model)
    if not endpoint:
        return None

    try:
        r = httpx.post(
            endpoint,
            headers={
                "Authorization": f"Bearer {settings.nim_api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            json={
                "text_prompts": [{"text": prompt, "weight": 1}],
                "height": height,
                "width": width,
                "samples": 1,
                "steps": 25,
            },
            timeout=120,
        )
    except Exception as e:
        return f"Request failed: {e}"

    if r.status_code == 202:
        poll_url = r.headers.get("Location") or r.headers.get("Content-Location")
        if poll_url:
            for _ in range(60):
                time.sleep(2)
                try:
                    pr = httpx.get(
                        poll_url,
                        headers={"Authorization": f"Bearer {settings.nim_api_key}", "Accept": "application/json"},
                        timeout=30,
                    )
                except Exception:
                    continue
                if pr.status_code == 200:
                    r = pr
                    break
            else:
                return "Image generation timed out"

    if r.status_code == 400:
        body = r.json()
        msg = body.get("detail") or body.get("message") or str(body)
        return f"Model rejected the request: {msg}"

    if r.status_code == 401:
        return "NVIDIA API authentication failed. Check your API key."

    if r.status_code == 404:
        return f"Model '{model}' not available at NVIDIA NIM"

    if r.status_code != 200 and r.status_code != 201:
        try:
            detail = r.json().get("detail", str(r.text[:200]))
        except Exception:
            detail = r.text[:200]
        return f"Image generation failed (HTTP {r.status_code}): {detail}"

    try:
        body = r.json()
    except Exception:
        return "Failed to parse image generation response"

    artifacts = body.get("artifacts")
    if artifacts and len(artifacts) > 0:
        b64 = artifacts[0].get("base64")
        if b64:
            return b64
        finish = artifacts[0].get("finishReason", "unknown")
        if finish != "SUCCESS":
            return f"Image generation finished with reason: {finish}"

    b64 = body.get("base64") or body.get("image") or body.get("data")
    if b64:
        return b64

    url = body.get("url") or body.get("image_url")
    if url:
        try:
            ir = httpx.get(url, timeout=30)
            ir.raise_for_status()
            return base64.b64encode(ir.content).decode()
        except Exception as e:
            return f"Failed to download generated image from URL: {e}"

    return f"Unexpected response format from image model"


def _save_image(b64_data: str, prompt_slug: str) -> Path:
    _IMAGE_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    slug = "".join(c if c.isalnum() or c in "_-" else "_" for c in prompt_slug[:20].strip())
    name = f"imagine_{ts}_{slug}.png" if slug else f"imagine_{ts}.png"
    path = _IMAGE_DIR / name
    path.write_bytes(base64.b64decode(b64_data))
    return path


@tool(
    name="imagine",
    description="Generate an image from a text prompt using NVIDIA NIM. Supports SDXL and Playground v2.5. Saves to storage/images/ and returns the file path",
    examples=[
        "generate an image of a cyberpunk cat",
        "imagine a mountain landscape at sunset digital art",
        "create a logo for my startup a blue geometric fox",
        "draw a pixel art spaceship",
    ],
    param_descriptions={
        "prompt": "Text description of the image to generate (5+ characters)",
        "size": "Image size: 1024x1024 (square), 1216x832 (landscape), or 832x1216 (portrait)",
        "model": "Model name: sdxl (default), playground-v2.5, or sdxl-turbo",
    },
)
def imagine(prompt: str, size: str = "1024x1024", model: str = _DEFAULT_MODEL) -> str:
    if len(prompt.strip()) < 5:
        return "Provide a descriptive prompt (at least 5 characters)"

    size = size.strip()
    if size not in _VALID_SIZES:
        return f"Invalid size '{size}'. Choose from: {', '.join(sorted(_VALID_SIZES))}"

    parts = size.split("x")
    width, height = int(parts[0]), int(parts[1])

    for attempt in (model,):
        result = _nvidia_genai(prompt.strip(), width, height, attempt)
        if result is None:
            alt = "playground-v2.5" if attempt == _DEFAULT_MODEL else _DEFAULT_MODEL
            result = _nvidia_genai(prompt.strip(), width, height, alt)
            if result is None:
                return f"Neither {attempt} nor {alt} is available"

        if result.startswith("Image generation ") or result.startswith("Model ") or result.startswith("NVIDIA "):
            if attempt == model:
                alt = "playground-v2.5" if model == _DEFAULT_MODEL else _DEFAULT_MODEL
                fallback = _nvidia_genai(prompt.strip(), width, height, alt)
                if fallback and not any(f.startswith(p) for p in ("Image generation ", "Model ", "NVIDIA ", "Request ", "Failed ")):
                    result = fallback
                else:
                    return result
            else:
                return result

        try:
            path = _save_image(result, prompt)
            return str(path.resolve())
        except Exception:
            return f"Failed to save generated image"

    return "Image generation failed after all attempts"


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
    name="images",
    description="Browse, search, and manage previously generated images in storage/images/. Actions: list, search <name>, remove <name/number>, view <name/number>",
    examples=[
        "show my generated images",
        "list saved images",
        "find images about cyberpunk",
        "delete image 3",
        "remove the mountain landscape image",
        "show me the pixel art spaceship image",
    ],
    param_descriptions={
        "action": "What to do: list (all images), search (find by name), remove (delete by name/number), view (get path by name/number)",
        "query": "Image name or number for search/remove/view actions",
    },
)
def images_manage(action: str, query: str = "") -> str:
    action = action.strip().lower()
    if not _IMAGE_DIR.exists():
        _IMAGE_DIR.mkdir(parents=True, exist_ok=True)

    if action == "list":
        files = sorted(_IMAGE_DIR.iterdir())
        if not files:
            return "No generated images yet"
        lines = []
        for i, f in enumerate(files, 1):
            size = f.stat().st_size
            kb = f"{size / 1024:.0f}KB" if size > 1024 else f"{size}B"
            lines.append(f"{i}. {f.name}  ({kb})")
        return "\n".join(lines)

    if action == "search":
        if not query:
            return "Provide a search term"
        q = query.strip().lower()
        files = sorted(_IMAGE_DIR.iterdir())
        matches = [f for f in files if q in f.stem.lower()]
        if not matches:
            return f"No images match '{query}'"
        lines = []
        for i, f in enumerate(matches, 1):
            kb = f"{f.stat().st_size / 1024:.0f}KB"
            lines.append(f"{i}. {f.name}  ({kb})")
        return "\n".join(lines)

    if action in ("view", "remove"):
        if not query:
            return f"Specify an image name or number to {action}"
        path_str = _find_image(query)
        if path_str is None:
            return f"Image '{query}' not found"
        if action == "remove":
            Path(path_str).unlink()
            return f"Removed {Path(path_str).name}"
        return path_str

    valid_actions = "list, search <term>, view <name/number>, remove <name/number>"
    return f"Unknown action '{action}'. Available: {valid_actions}"
