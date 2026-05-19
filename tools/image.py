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

_IMAGE_DIR = Path("storage/images")
_VALID_SIZES = {"1024x1024", "1216x832", "832x1216"}
_DEFAULT_MODEL = "pollinations"

_POLLINATIONS_BASE = "https://image.pollinations.ai/prompt"


def _pollinations_genai(prompt: str, width: int, height: int) -> str | None:
    encoded = urllib.parse.quote(prompt)
    url = f"{_POLLINATIONS_BASE}/{encoded}?width={width}&height={height}&seed={int(time.time()) % 100000}&nologo=true"
    try:
        r = httpx.get(url, timeout=120, verify=False)
        if r.status_code == 200 and len(r.content) > 1000:
            return base64.b64encode(r.content).decode()
        return None
    except Exception:
        return None


def _nvidia_genai(prompt: str, width: int, height: int, model: str) -> str | None:
    endpoints = {
        "sdxl": [
            "https://integrate.api.nvidia.com/v1/genai/stabilityai/stable-diffusion-xl-base-1.0",
        ],
        "playground-v2.5": [
            "https://integrate.api.nvidia.com/v1/genai/playgroundai/playground-v2.5-1024px-aesthetic",
        ],
        "sdxl-turbo": [
            "https://integrate.api.nvidia.com/v1/genai/stabilityai/sdxl-turbo",
        ],
    }
    urls = endpoints.get(model, [])
    if not urls:
        return None

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

    for url in urls:
        try:
            r = httpx.post(url, headers=headers, json=payload, timeout=120, verify=False)
        except Exception:
            continue
        if r.status_code in (200, 201):
            return _extract_b64_from_nvidia(r)
        if r.status_code == 202:
            poll_url = r.headers.get("Location") or r.headers.get("Content-Location")
            if poll_url:
                for _ in range(30):
                    time.sleep(2)
                    try:
                        pr = httpx.get(
                            poll_url,
                            headers={"Authorization": f"Bearer {settings.nim_api_key}", "Accept": "application/json"},
                            timeout=30, verify=False,
                        )
                    except Exception:
                        continue
                    if pr.status_code == 200:
                        return _extract_b64_from_nvidia(pr)
        break
    return None


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
        except Exception:
            pass
    return None


def _generate(prompt: str, width: int, height: int, model: str) -> str:
    if model == "pollinations":
        result = _pollinations_genai(prompt, width, height)
        if result:
            return result
        return "Pollinations generation failed. Try again."

    result = _nvidia_genai(prompt, width, height, model)
    if result:
        return result

    alt = "playground-v2.5" if model in ("sdxl", "sdxl-turbo") else "sdxl"
    result = _nvidia_genai(prompt, width, height, alt)
    if result:
        return result

    result = _pollinations_genai(prompt, width, height)
    if result:
        return result
    return "All image generation backends failed. Check API key or try again later."


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
    description="Generate an image from a text description. Uses Pollinations.ai (free, no API key needed) with NVIDIA NIM fallback. Keywords: image, picture, photo, img, art, drawing, illustration, render, create, make, generate, draw, imagine, paint, sketch, visual. This is the ONLY tool for creating new images",
    examples=[
        "generate an image of a cyberpunk cat",
        "imagine a mountain landscape at sunset digital art",
        "create a logo for my startup a blue geometric fox",
        "draw a pixel art spaceship",
        "make me a picture of a sports car",
        "gen me an image of a dragon",
        "generate a photo of a sunset beach",
        "create an illustration of a futuristic city",
        "img of a cat sitting on a couch",
        "render a 3D model of a robot",
        "paint a portrait of a wizard",
        "sketch a fantasy landscape",
        "generate a wallpaper of mountains",
        "make a logo for my brand",
        "create digital art of a cyberpunk street",
    ],
    param_descriptions={
        "prompt": "Text description of the image to generate (5+ characters)",
        "size": "Image size: 1024x1024 (square, default), 1216x832 (landscape), or 832x1216 (portrait)",
        "model": "Model: pollinations (default, free), sdxl, playground-v2.5, or sdxl-turbo",
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

    result = _generate(prompt.strip(), width, height, model)

    if result.startswith("All ") or result.startswith("Pollinations ") or result.startswith("NVIDIA "):
        return result

    try:
        path = _save_image(result, prompt)
        resolved = str(path.resolve())
        try:
            if sys.platform == "win32":
                os.startfile(resolved)
            elif sys.platform == "darwin":
                subprocess.run(["open", resolved], capture_output=True, timeout=10)
            else:
                subprocess.run(["xdg-open", resolved], capture_output=True, timeout=10)
        except Exception:
            pass
        return f"Image saved: {resolved}"
    except Exception:
        return "Failed to save generated image"


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
    name="gallery",
    description="Browse and manage your gallery of previously generated saved images: list all, search by name, view file path, or delete. Keywords: saved images, generated pictures, image collection, view image, delete image, browse gallery, list images. NOT for creating new images — use 'imagine' for that",
    examples=[
        "show my saved images",
        "list my image gallery",
        "find images about cyberpunk",
        "delete image 3",
        "remove the mountain landscape image",
    ],
    param_descriptions={
        "action": "list (all images), search (find by name), remove (delete by name/number), view (get path by name/number)",
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
