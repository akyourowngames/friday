"""Safe image editing operations using Pillow.

Every mutating operation writes a verified sibling temporary file before
replacing its destination.  In particular, an edit whose destination is the
source cannot leave the source half-written after an encoder error.
"""
from __future__ import annotations

import os
import tempfile
from contextlib import suppress
from pathlib import Path
from typing import Any

from PIL import Image, ImageOps

from ares.tools.asset_manifest import record_asset


_FORMAT_BY_EXTENSION = {
    ".png": "PNG",
    ".jpg": "JPEG",
    ".jpeg": "JPEG",
    ".webp": "WEBP",
    ".bmp": "BMP",
    ".gif": "GIF",
}
_EXTENSION_BY_FORMAT = {"PNG": ".png", "JPEG": ".jpeg", "WEBP": ".webp", "BMP": ".bmp", "GIF": ".gif"}
_ANIMATED_FORMATS = {"GIF", "WEBP", "PNG"}


def _human_size(nbytes: int) -> str:
    """Convert byte count to human-readable string."""
    for unit in ("B", "KB", "MB", "GB"):
        if nbytes < 1024:
            return f"{nbytes:.1f} {unit}"
        nbytes /= 1024
    return f"{nbytes:.1f} TB"


def _format_from_extension(path: str | Path) -> str | None:
    return _FORMAT_BY_EXTENSION.get(Path(path).suffix.casefold())


def _format_from_name(format_name: str) -> str:
    normalized = str(format_name or "").strip().casefold()
    if normalized == "jpg":
        normalized = "jpeg"
    mapping = {"png": "PNG", "jpeg": "JPEG", "webp": "WEBP", "bmp": "BMP", "gif": "GIF"}
    if normalized not in mapping:
        raise ValueError(f"Unsupported format: {format_name}. Use one of: bmp, gif, jpeg, png, webp")
    return mapping[normalized]


def _close_frames(frames: list[Image.Image]) -> None:
    for frame in frames:
        with suppress(Exception):
            frame.close()


def _load_frames(path: str | Path) -> tuple[str, list[Image.Image], list[int], int, int]:
    """Load all frames and timing while the source remains open."""
    with Image.open(path) as source:
        source_format = (source.format or _format_from_extension(path) or "PNG").upper()
        frame_count = int(getattr(source, "n_frames", 1) or 1)
        durations: list[int] = []
        frames: list[Image.Image] = []
        loop = int(source.info.get("loop", 0) or 0)
        try:
            for index in range(frame_count):
                source.seek(index)
                frame = ImageOps.exif_transpose(source.copy())
                frames.append(frame)
                duration = source.info.get("duration", 0)
                durations.append(max(0, int(duration or 0)))
        except Exception:
            _close_frames(frames)
            raise
    return source_format, frames, durations, loop, frame_count


def _rgb_if_needed(frame: Image.Image, target_format: str) -> Image.Image:
    if target_format != "JPEG":
        return frame
    if frame.mode not in {"RGBA", "LA", "P"}:
        return frame.convert("RGB") if frame.mode != "RGB" else frame
    rgba = frame.convert("RGBA")
    background = Image.new("RGB", rgba.size, (255, 255, 255))
    background.paste(rgba, mask=rgba.getchannel("A"))
    rgba.close()
    return background


def _atomic_save_frames(
    frames: list[Image.Image],
    destination: str | Path,
    target_format: str,
    *,
    durations: list[int] | None = None,
    loop: int = 0,
    quality: int | None = None,
) -> Path:
    """Encode, validate, fsync, and atomically replace an image asset."""
    if not frames:
        raise ValueError("No image frames to save")
    target_format = target_format.upper()
    destination_path = Path(destination).expanduser()
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    animated = len(frames) > 1
    if animated and target_format not in _ANIMATED_FORMATS:
        raise ValueError(f"{target_format} does not support preserving animation; choose GIF, PNG, or WEBP")
    suffix = destination_path.suffix or _EXTENSION_BY_FORMAT.get(target_format, ".img")
    descriptor, temp_name = tempfile.mkstemp(prefix=f".{destination_path.stem}.", suffix=suffix, dir=destination_path.parent)
    os.close(descriptor)
    temporary = Path(temp_name)
    prepared: list[Image.Image] = []
    try:
        for frame in frames:
            prepared.append(_rgb_if_needed(frame, target_format))
        save_kwargs: dict[str, Any] = {}
        if quality is not None:
            save_kwargs["quality"] = quality
        if target_format == "JPEG":
            save_kwargs["optimize"] = True
        if animated:
            save_kwargs.update(
                save_all=True,
                append_images=prepared[1:],
                duration=durations or [0] * len(prepared),
                loop=loop,
            )
        prepared[0].save(temporary, format=target_format, **save_kwargs)
        with Image.open(temporary) as verification:
            verification.verify()
        if animated:
            with Image.open(temporary) as verification:
                if int(getattr(verification, "n_frames", 1) or 1) != len(prepared):
                    raise ValueError("Saved image lost animation frames")
        # Windows requires a writable descriptor for fsync on this path.
        with temporary.open("r+b") as handle:
            os.fsync(handle.fileno())
        os.replace(temporary, destination_path)
        return destination_path
    finally:
        with suppress(FileNotFoundError):
            temporary.unlink()
        # _rgb_if_needed sometimes returns the original object, so only close
        # truly created conversions that are not source frames.
        for frame in prepared:
            if frame not in frames:
                with suppress(Exception):
                    frame.close()


def _manifest_notice(path: Path, action: str, history: dict[str, Any]) -> str:
    try:
        return f"\nManifest: {record_asset(path, action=action, history=history)}"
    except Exception as exc:
        # The asset is already safely committed.  A telemetry failure must not
        # make a caller retry and accidentally overwrite it.
        return f"\nWarning: image saved but asset manifest could not be recorded: {exc}"


def image_info(path: str) -> str:
    """Get metadata about an image file."""
    if not os.path.isfile(path):
        return f"Error: File not found: {path}"
    try:
        file_size = os.path.getsize(path)
        with Image.open(path) as image:
            image.load()
            info = [
                f"Format: {image.format} ({image.format_description})",
                f"Dimensions: {image.size[0]}×{image.size[1]}",
                f"Mode: {image.mode}",
                f"Size: {_human_size(file_size)}",
            ]
            if getattr(image, "is_animated", False):
                info.append(f"Animated: Yes ({getattr(image, 'n_frames', 1)} frames)")
            exif = image.getexif()
            if exif:
                orientation = exif.get(274, 1)
                if orientation != 1:
                    info.append(f"EXIF Orientation: {orientation}")
                make, model = exif.get(271, ""), exif.get(272, "")
                if make or model:
                    info.append(f"Camera: {make} {model}".strip())
        return "\n".join(info)
    except Exception as exc:
        return f"Error reading image: {exc}"


def resize_image(
    path: str,
    width: int | None = None,
    height: int | None = None,
    percent: int | None = None,
    output: str | None = None,
) -> str:
    """Resize an image, preserving supported animation frames and timing."""
    if not os.path.isfile(path):
        return f"Error: File not found: {path}"
    try:
        source_format, frames, durations, loop, _ = _load_frames(path)
        try:
            original_w, original_h = frames[0].size
            if percent is not None:
                factor = float(percent) / 100.0
                new_size = (round(original_w * factor), round(original_h * factor))
            elif width is not None and height is not None:
                width, height = int(width), int(height)
                if width <= 0 or height <= 0:
                    raise ValueError("Width and height must be positive")
                ratio = min(width / original_w, height / original_h)
                new_size = (round(original_w * ratio), round(original_h * ratio))
            elif width is not None:
                width = int(width)
                new_size = (width, round(original_h * (width / original_w)))
            elif height is not None:
                height = int(height)
                new_size = (round(original_w * (int(height) / original_h)), int(height))
            else:
                return "Error: Specify width, height, or percent"
            if new_size[0] <= 0 or new_size[1] <= 0:
                raise ValueError("Computed image dimensions must be positive")
            destination = Path(output or path).expanduser()
            target_format = _format_from_extension(destination) or source_format
            resized = [frame.resize(new_size, Image.Resampling.LANCZOS) for frame in frames]
            try:
                saved = _atomic_save_frames(resized, destination, target_format, durations=durations, loop=loop)
            finally:
                _close_frames(resized)
        finally:
            _close_frames(frames)
        history = {
            "source": path,
            "original_width": original_w,
            "original_height": original_h,
            "width": width,
            "height": height,
            "percent": percent,
        }
        return f"Resized {original_w}×{original_h} → {new_size[0]}×{new_size[1]}, saved to {saved}" + _manifest_notice(saved, "resize_image", history)
    except Exception as exc:
        return f"Error: {exc}"


def _reconciled_convert_destination(path: str, output: str | None, target_format: str) -> Path:
    extension = _EXTENSION_BY_FORMAT[target_format]
    if output is None:
        return Path(path).expanduser().with_suffix(extension)
    destination = Path(output).expanduser()
    # A format and extension that disagree are misleading to consumers.  Keep
    # the requested basename but make the physical suffix truthful.
    valid_extensions = {extension}
    if target_format == "JPEG":
        valid_extensions.add(".jpg")
    return destination.with_suffix(extension) if destination.suffix.casefold() not in valid_extensions else destination


def convert_image(path: str, format: str, output: str | None = None, quality: int = 85) -> str:
    """Convert an image through a verified atomic replacement."""
    if not os.path.isfile(path):
        return f"Error: File not found: {path}"
    try:
        target_format = _format_from_name(format)
    except ValueError as exc:
        return f"Error: {exc}"
    try:
        quality = int(quality)
        if quality < 1 or quality > 100:
            return "Error: quality must be between 1 and 100"
        _, frames, durations, loop, _ = _load_frames(path)
        try:
            destination = _reconciled_convert_destination(path, output, target_format)
            saved = _atomic_save_frames(
                frames,
                destination,
                target_format,
                durations=durations,
                loop=loop,
                quality=quality if target_format in {"JPEG", "WEBP"} else None,
            )
        finally:
            _close_frames(frames)
        return f"Converted to {target_format}, saved to {saved}" + _manifest_notice(
            saved,
            "convert_image",
            {"source": path, "format": target_format.casefold(), "quality": quality},
        )
    except Exception as exc:
        return f"Error: {exc}"


def crop_image(
    path: str,
    left: int = 0,
    top: int = 0,
    right: int | None = None,
    bottom: int | None = None,
    output: str | None = None,
) -> str:
    """Crop an image after clamping and validating the actual intersection."""
    if not os.path.isfile(path):
        return f"Error: File not found: {path}"
    if right is None or bottom is None:
        return "Error: right and bottom parameters are required"
    try:
        source_format, frames, durations, loop, _ = _load_frames(path)
        try:
            width, height = frames[0].size
            left = max(0, min(int(left), width))
            top = max(0, min(int(top), height))
            right = max(0, min(int(right), width))
            bottom = max(0, min(int(bottom), height))
            if left >= right or top >= bottom:
                return f"Error: Crop region has no overlap with image bounds ({width}×{height})"
            cropped = [frame.crop((left, top, right, bottom)) for frame in frames]
            try:
                destination = Path(output or path).expanduser()
                target_format = _format_from_extension(destination) or source_format
                saved = _atomic_save_frames(cropped, destination, target_format, durations=durations, loop=loop)
            finally:
                _close_frames(cropped)
        finally:
            _close_frames(frames)
        return f"Cropped ({left},{top}) → ({right},{bottom}) = {right - left}×{bottom - top}, saved to {saved}" + _manifest_notice(
            saved,
            "crop_image",
            {
                "source": path,
                "left": left,
                "top": top,
                "right": right,
                "bottom": bottom,
                "original_width": width,
                "original_height": height,
            },
        )
    except Exception as exc:
        return f"Error: {exc}"
