"""Image generation via Pollinations.ai (free, no API key)."""

from __future__ import annotations

import hashlib
from pathlib import Path
from urllib.parse import quote

import httpx

from ares.tools.asset_manifest import record_asset

IMAGES_DIR = Path("~/.ares/images").expanduser()

POLLINATIONS_BASE = "https://image.pollinations.ai/prompt"


def generate_image(
    prompt: str,
    width: int = 1024,
    height: int = 1024,
    model: str = "flux",
    seed: int | None = None,
) -> str:
    """Generate an image from a text prompt via Pollinations.ai.

    Args:
        prompt: Text description of the image.
        width: Output width in pixels (default 1024).
        height: Output height in pixels (default 1024).
        model: Model name - flux, turbo, or stable-diffusion (default flux).
        seed: Deterministic seed for reproducibility (None = random).

    Returns:
        File path on success, error message on failure.
    """
    IMAGES_DIR.mkdir(parents=True, exist_ok=True)

    encoded_prompt = quote(prompt)
    url = f"{POLLINATIONS_BASE}/{encoded_prompt}"

    params = {
        "width": width,
        "height": height,
        "model": model,
    }
    if seed is not None:
        params["seed"] = seed

    prompt_hash = hashlib.md5(prompt.encode()).hexdigest()[:8]
    filename = f"{prompt_hash}.jpg"
    filepath = IMAGES_DIR / filename

    try:
        with httpx.Client(timeout=120.0) as client:
            response = client.get(url, params=params)
            response.raise_for_status()

            content_type = response.headers.get("content-type", "")
            if "image" not in content_type:
                return f"Error: Expected image, got {content_type}"

            filepath.write_bytes(response.content)

        manifest = record_asset(
            filepath,
            action="generate_image",
            history={
                "prompt": prompt,
                "width": width,
                "height": height,
                "model": model,
                "seed": seed,
            },
        )
        return f"Image saved to {filepath}\nManifest: {manifest}"

    except httpx.TimeoutException:
        return "Error: Image generation timed out after 120s"
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 429:
            return "Error: Rate limited by Pollinations.ai. Wait 15 seconds and try again."
        return f"Error: HTTP {e.response.status_code}: {e}"
    except Exception as e:
        return f"Error generating image: {e}"
