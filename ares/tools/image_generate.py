"""Image generation via Pollinations.ai with verified, durable assets."""
from __future__ import annotations

import hashlib
import io
import json
import os
import tempfile
from contextlib import suppress
from pathlib import Path
from urllib.parse import quote

import httpx
from PIL import Image

from ares.tools.asset_manifest import record_asset


IMAGES_DIR = Path("~/.ares/images").expanduser()
POLLINATIONS_BASE = "https://image.pollinations.ai/prompt"
_EXTENSIONS = {"JPEG": ".jpg", "PNG": ".png", "WEBP": ".webp", "GIF": ".gif", "BMP": ".bmp"}


def _identity(prompt: str, width: int, height: int, model: str, seed: int | None) -> str:
    payload = json.dumps(
        {"prompt": prompt, "width": width, "height": height, "model": model, "seed": seed},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:20]


def _decoded_format(content: bytes) -> str:
    try:
        with Image.open(io.BytesIO(content)) as image:
            image.verify()
        with Image.open(io.BytesIO(content)) as image:
            return (image.format or "").upper()
    except Exception as exc:
        raise ValueError(f"Generated response is not a valid image: {exc}") from exc


def _atomic_write(destination: Path, content: bytes) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=f".{destination.stem}.", suffix=destination.suffix, dir=destination.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
    finally:
        with suppress(FileNotFoundError):
            temporary.unlink()


def generate_image(
    prompt: str,
    width: int = 1024,
    height: int = 1024,
    model: str = "flux",
    seed: int | None = None,
) -> str:
    """Generate an image from a text prompt via Pollinations.ai.

    A content-type header is only a hint.  The response is decoded before it
    receives a path, and a valid path remains successful if the optional asset
    manifest cannot be updated.
    """
    try:
        width, height = int(width), int(height)
    except (TypeError, ValueError):
        return "Error: width and height must be integers"
    if width <= 0 or height <= 0:
        return "Error: width and height must be positive"
    prompt = str(prompt or "")
    if not prompt.strip():
        return "Error: prompt is required"
    if seed is not None:
        try:
            seed = int(seed)
        except (TypeError, ValueError):
            return "Error: seed must be an integer"
    model = str(model or "flux")
    IMAGES_DIR.mkdir(parents=True, exist_ok=True)
    url = f"{POLLINATIONS_BASE}/{quote(prompt)}"
    params: dict[str, object] = {"width": width, "height": height, "model": model}
    if seed is not None:
        params["seed"] = seed

    try:
        with httpx.Client(timeout=120.0) as client:
            response = client.get(url, params=params)
            response.raise_for_status()
            content_type = response.headers.get("content-type", "")
            if "image" not in content_type.casefold():
                return f"Error: Expected image, got {content_type}"
            content = bytes(response.content)
        image_format = _decoded_format(content)
        extension = _EXTENSIONS.get(image_format, ".img")
        filepath = IMAGES_DIR / f"{_identity(prompt, width, height, model, seed)}{extension}"
        _atomic_write(filepath, content)
        try:
            manifest = record_asset(
                filepath,
                action="generate_image",
                history={"prompt": prompt, "width": width, "height": height, "model": model, "seed": seed},
            )
        except Exception as exc:
            return f"Image saved to {filepath}\nWarning: image saved but asset manifest could not be recorded: {exc}"
        return f"Image saved to {filepath}\nManifest: {manifest}"
    except httpx.TimeoutException:
        return "Error: Image generation timed out after 120s"
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 429:
            return "Error: Rate limited by Pollinations.ai. Wait 15 seconds and try again."
        return f"Error: HTTP {exc.response.status_code}: {exc}"
    except Exception as exc:
        return f"Error generating image: {exc}"
