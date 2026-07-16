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

# Intentionally small, named output targets.  They are only used when a
# caller opts in via ``preset``; legacy width/height/percent calls retain
# their exact old geometry.
_RESIZE_PRESETS: dict[str, tuple[int, int]] = {
    "thumbnail": (160, 160),
    "avatar": (256, 256),
    "small": (640, 480),
    "medium": (1280, 720),
    "large": (1920, 1080),
    "square": (1080, 1080),
}
_CROP_ANCHORS: dict[str, tuple[float, float]] = {
    "center": (0.5, 0.5),
    "top": (0.5, 0.0),
    "bottom": (0.5, 1.0),
    "left": (0.0, 0.5),
    "right": (1.0, 0.5),
    "top_left": (0.0, 0.0),
    "top_right": (1.0, 0.0),
    "bottom_left": (0.0, 1.0),
    "bottom_right": (1.0, 1.0),
}


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


def resize_presets() -> dict[str, tuple[int, int]]:
    """Return supported opt-in resize targets without exposing mutable state."""
    return dict(_RESIZE_PRESETS)


def _positive_int(value: Any, field: str) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be an integer") from exc
    if result <= 0:
        raise ValueError(f"{field} must be positive")
    return result


def _normalize_metadata_policy(policy: str | None) -> str:
    normalized = str(policy or "strip").strip().casefold()
    if normalized not in {"strip", "preserve"}:
        raise ValueError("metadata_policy must be strip or preserve")
    return normalized


def _normalize_transparency_policy(policy: str | None) -> str:
    normalized = str(policy or "flatten").strip().casefold()
    aliases = {"white": "flatten", "background": "flatten"}
    normalized = aliases.get(normalized, normalized)
    if normalized not in {"flatten", "error", "preserve"}:
        raise ValueError("transparency_policy must be flatten, error, or preserve")
    return normalized


def _parse_aspect_ratio(value: Any) -> float:
    """Parse a positive ``W:H``/numeric aspect ratio into one float."""
    if isinstance(value, (tuple, list)) and len(value) == 2:
        numerator, denominator = value
    elif isinstance(value, str) and ":" in value:
        numerator, denominator = value.split(":", 1)
    else:
        try:
            ratio = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("aspect_ratio must be a positive number or W:H string") from exc
        if ratio <= 0:
            raise ValueError("aspect_ratio must be positive")
        return ratio
    try:
        ratio = float(numerator) / float(denominator)
    except (TypeError, ValueError, ZeroDivisionError) as exc:
        raise ValueError("aspect_ratio must be a positive number or W:H string") from exc
    if ratio <= 0:
        raise ValueError("aspect_ratio must be positive")
    return ratio


def resize_geometry(
    source_width: int,
    source_height: int,
    *,
    width: int | None = None,
    height: int | None = None,
    percent: int | float | None = None,
    fit: str = "contain",
    preset: str | None = None,
    pad: bool = False,
) -> dict[str, Any]:
    """Resolve an opt-in resize request before decoding or writing an image.

    ``resize_image`` historically returned the contained dimensions for a
    width/height box.  This helper preserves that result unless callers choose
    ``fit='pad'`` or ``pad=True``.  It makes previews and batch plans use the
    same pixel geometry as the encoder.
    """
    original_w = _positive_int(source_width, "source_width")
    original_h = _positive_int(source_height, "source_height")
    requested_preset = str(preset or "").strip().casefold()
    if requested_preset:
        if requested_preset not in _RESIZE_PRESETS:
            choices = ", ".join(sorted(_RESIZE_PRESETS))
            raise ValueError(f"Unknown resize preset: {preset}. Use one of: {choices}")
        if any(value is not None for value in (width, height, percent)):
            raise ValueError("preset cannot be combined with width, height, or percent")
        width, height = _RESIZE_PRESETS[requested_preset]

    fit_mode = str(fit or "contain").strip().casefold()
    if fit_mode not in {"contain", "cover", "stretch", "pad"}:
        raise ValueError("fit must be contain, cover, stretch, or pad")
    pad_requested = bool(pad) or fit_mode == "pad"
    if fit_mode == "pad":
        fit_mode = "contain"

    if percent is not None:
        if width is not None or height is not None:
            raise ValueError("percent cannot be combined with width or height")
        try:
            multiplier = float(percent) / 100.0
        except (TypeError, ValueError) as exc:
            raise ValueError("percent must be numeric") from exc
        if multiplier <= 0 or multiplier > 100:
            raise ValueError("percent must be greater than 0 and at most 10000")
        resize_size = (max(1, round(original_w * multiplier)), max(1, round(original_h * multiplier)))
        return {
            "source": {"width": original_w, "height": original_h},
            "resize": {"width": resize_size[0], "height": resize_size[1]},
            "target": {"width": resize_size[0], "height": resize_size[1]},
            "fit": "percent",
            "percent": float(percent),
            "preset": requested_preset or None,
            "padded": False,
        }

    if width is None and height is None:
        raise ValueError("Specify width, height, percent, or preset")
    target_w = _positive_int(width, "width") if width is not None else None
    target_h = _positive_int(height, "height") if height is not None else None
    if pad_requested and (target_w is None or target_h is None):
        raise ValueError("padding requires both width and height (or a two-dimensional preset)")

    if target_w is None:
        resize_size = (max(1, round(original_w * (target_h / original_h))), target_h)
    elif target_h is None:
        resize_size = (target_w, max(1, round(original_h * (target_w / original_w))))
    elif fit_mode == "stretch":
        resize_size = (target_w, target_h)
    else:
        ratio = min(target_w / original_w, target_h / original_h)
        if fit_mode == "cover":
            ratio = max(target_w / original_w, target_h / original_h)
        resize_size = (max(1, round(original_w * ratio)), max(1, round(original_h * ratio)))

    canvas_size = (target_w, target_h) if pad_requested and target_w and target_h else resize_size
    return {
        "source": {"width": original_w, "height": original_h},
        "resize": {"width": resize_size[0], "height": resize_size[1]},
        "target": {"width": canvas_size[0], "height": canvas_size[1]},
        "fit": fit_mode,
        "preset": requested_preset or None,
        "padded": canvas_size != resize_size,
    }


def _anchor_offset(container: int, content: int, alignment: float) -> int:
    return max(0, round((container - content) * alignment))


def crop_geometry(
    source_width: int,
    source_height: int,
    *,
    left: int | None = 0,
    top: int | None = 0,
    right: int | None = None,
    bottom: int | None = None,
    mode: str = "box",
    percent: Any = None,
    aspect_ratio: Any = None,
    crop_width: int | None = None,
    crop_height: int | None = None,
    anchor: str = "center",
    clamp: bool = True,
) -> dict[str, Any]:
    """Resolve pixel, percentage, centered, or aspect-ratio crop geometry."""
    width = _positive_int(source_width, "source_width")
    height = _positive_int(source_height, "source_height")
    crop_mode = str(mode or "box").strip().casefold()
    if crop_mode not in {"box", "center", "aspect"}:
        raise ValueError("mode must be box, center, or aspect")
    anchor_name = str(anchor or "center").strip().casefold().replace("-", "_")
    if anchor_name not in _CROP_ANCHORS:
        raise ValueError(f"anchor must be one of: {', '.join(sorted(_CROP_ANCHORS))}")

    details: dict[str, Any] = {"mode": crop_mode, "anchor": anchor_name}
    requested: tuple[int, int, int, int]
    if isinstance(percent, dict):
        required = {"left", "top", "right", "bottom"}
        if not required.issubset(percent):
            raise ValueError("percentage crop requires left, top, right, and bottom values")
        try:
            values = {key: float(percent[key]) for key in required}
        except (TypeError, ValueError) as exc:
            raise ValueError("percentage crop values must be numeric") from exc
        if any(value < 0 or value > 100 for value in values.values()):
            raise ValueError("percentage crop values must be between 0 and 100")
        requested = (
            round(width * values["left"] / 100),
            round(height * values["top"] / 100),
            round(width * values["right"] / 100),
            round(height * values["bottom"] / 100),
        )
        details["percentage"] = values
    else:
        center_scale: float | None = None
        if percent is not None:
            try:
                center_scale = float(percent) / 100.0
            except (TypeError, ValueError) as exc:
                raise ValueError("percent must be a number or a crop percentage object") from exc
            if center_scale <= 0 or center_scale > 1:
                raise ValueError("percent must be greater than 0 and at most 100")
        ratio = _parse_aspect_ratio(aspect_ratio) if aspect_ratio is not None else None
        if crop_mode == "box" and center_scale is None and ratio is None:
            if right is None or bottom is None:
                raise ValueError("right and bottom parameters are required for box crops")
            requested = (int(left or 0), int(top or 0), int(right), int(bottom))
        else:
            requested_width = _positive_int(crop_width, "crop_width") if crop_width is not None else None
            requested_height = _positive_int(crop_height, "crop_height") if crop_height is not None else None
            if requested_width is not None or requested_height is not None:
                if requested_width is None:
                    if ratio is None:
                        raise ValueError("crop_width requires crop_height or aspect_ratio")
                    requested_width = max(1, round(requested_height * ratio))
                if requested_height is None:
                    if ratio is None:
                        raise ValueError("crop_height requires crop_width or aspect_ratio")
                    requested_height = max(1, round(requested_width / ratio))
            else:
                max_width = max(1, round(width * center_scale)) if center_scale is not None else width
                max_height = max(1, round(height * center_scale)) if center_scale is not None else height
                if ratio is None:
                    if crop_mode == "center" or center_scale is not None:
                        requested_width, requested_height = max_width, max_height
                    else:
                        raise ValueError("aspect mode requires aspect_ratio or explicit crop dimensions")
                elif max_width / max_height > ratio:
                    requested_height = max_height
                    requested_width = max(1, round(max_height * ratio))
                else:
                    requested_width = max_width
                    requested_height = max(1, round(max_width / ratio))
            horizontal, vertical = _CROP_ANCHORS[anchor_name]
            requested_left = _anchor_offset(width, requested_width, horizontal)
            requested_top = _anchor_offset(height, requested_height, vertical)
            requested = (requested_left, requested_top, requested_left + requested_width, requested_top + requested_height)
            if ratio is not None:
                details["aspect_ratio"] = ratio
            if center_scale is not None:
                details["percent"] = center_scale * 100

    actual = requested
    if clamp:
        actual = (
            max(0, min(requested[0], width)), max(0, min(requested[1], height)),
            max(0, min(requested[2], width)), max(0, min(requested[3], height)),
        )
    elif actual[0] < 0 or actual[1] < 0 or actual[2] > width or actual[3] > height:
        raise ValueError(f"Crop region exceeds image bounds ({width}×{height})")
    if actual[0] >= actual[2] or actual[1] >= actual[3]:
        raise ValueError(f"Crop region has no overlap with image bounds ({width}×{height})")
    details.update({
        "requested": {"left": requested[0], "top": requested[1], "right": requested[2], "bottom": requested[3]},
        "applied": {"left": actual[0], "top": actual[1], "right": actual[2], "bottom": actual[3]},
        "clamped": requested != actual,
    })
    return {
        "box": actual,
        "target": {"width": actual[2] - actual[0], "height": actual[3] - actual[1]},
        "details": details,
    }


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


def _rgb_if_needed(
    frame: Image.Image,
    target_format: str,
    *,
    background: str | tuple[int, int, int] = "white",
    transparency_policy: str = "flatten",
) -> Image.Image:
    if target_format != "JPEG":
        return frame
    if frame.mode not in {"RGBA", "LA", "P"}:
        return frame.convert("RGB") if frame.mode != "RGB" else frame
    policy = _normalize_transparency_policy(transparency_policy)
    if policy in {"error", "preserve"}:
        raise ValueError("JPEG cannot preserve transparency; use transparency_policy=flatten or choose PNG/WebP")
    rgba = frame.convert("RGBA")
    try:
        background_image = Image.new("RGB", rgba.size, background)
    except (TypeError, ValueError) as exc:
        raise ValueError("background must be a Pillow-supported color") from exc
    background_image.paste(rgba, mask=rgba.getchannel("A"))
    rgba.close()
    return background_image


def _preservable_metadata(path: str | Path, policy: str) -> dict[str, bytes]:
    """Read only encoder-safe metadata when an advanced caller opts in."""
    if _normalize_metadata_policy(policy) != "preserve":
        return {}
    with Image.open(path) as source:
        metadata: dict[str, bytes] = {}
        exif = source.info.get("exif")
        icc_profile = source.info.get("icc_profile")
        if isinstance(exif, bytes) and exif:
            metadata["exif"] = exif
        if isinstance(icc_profile, bytes) and icc_profile:
            metadata["icc_profile"] = icc_profile
        return metadata


def _atomic_save_frames(
    frames: list[Image.Image],
    destination: str | Path,
    target_format: str,
    *,
    durations: list[int] | None = None,
    loop: int = 0,
    quality: int | None = None,
    metadata: dict[str, bytes] | None = None,
    background: str | tuple[int, int, int] = "white",
    transparency_policy: str = "flatten",
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
            prepared.append(
                _rgb_if_needed(
                    frame,
                    target_format,
                    background=background,
                    transparency_policy=transparency_policy,
                )
            )
        save_kwargs: dict[str, Any] = {}
        if quality is not None:
            save_kwargs["quality"] = quality
        if target_format == "JPEG":
            save_kwargs["optimize"] = True
        if metadata:
            # Pillow forwards these only to codecs that support them.  Restrict
            # EXIF to broadly compatible formats so an opt-in preserve request
            # never turns into a malformed GIF/BMP save.
            if metadata.get("icc_profile") and target_format in {"JPEG", "PNG", "WEBP"}:
                save_kwargs["icc_profile"] = metadata["icc_profile"]
            if metadata.get("exif") and target_format in {"JPEG", "PNG", "WEBP"}:
                save_kwargs["exif"] = metadata["exif"]
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


def _resize_frame_list(
    frames: list[Image.Image],
    geometry: dict[str, Any],
    *,
    pad_color: str | tuple[int, int, int] = "transparent",
) -> list[Image.Image]:
    """Resize frames and, when requested, letterbox them on a target canvas."""
    resize_size = (int(geometry["resize"]["width"]), int(geometry["resize"]["height"]))
    target_size = (int(geometry["target"]["width"]), int(geometry["target"]["height"]))
    resized = [frame.resize(resize_size, Image.Resampling.LANCZOS) for frame in frames]
    if target_size == resize_size:
        return resized
    padded: list[Image.Image] = []
    try:
        for frame in resized:
            mode = "RGBA" if "A" in frame.getbands() else "RGB"
            canvas_color: str | tuple[int, int, int] | tuple[int, int, int, int] = pad_color
            if str(pad_color).strip().casefold() == "transparent":
                canvas_color = (0, 0, 0, 0) if mode == "RGBA" else (0, 0, 0)
            try:
                canvas = Image.new(mode, target_size, canvas_color)
            except (TypeError, ValueError) as exc:
                raise ValueError("pad_color must be a Pillow-supported color") from exc
            rendered = frame.convert("RGBA") if mode == "RGBA" and frame.mode != "RGBA" else frame
            offset = (
                _anchor_offset(target_size[0], rendered.width, 0.5),
                _anchor_offset(target_size[1], rendered.height, 0.5),
            )
            if "A" in rendered.getbands():
                canvas.paste(rendered, offset, rendered.getchannel("A"))
            else:
                canvas.paste(rendered, offset)
            if rendered is not frame:
                rendered.close()
            padded.append(canvas)
    except Exception:
        _close_frames(padded)
        raise
    finally:
        _close_frames(resized)
    return padded


def resize_image(
    path: str,
    width: int | None = None,
    height: int | None = None,
    percent: int | None = None,
    output: str | None = None,
    fit: str = "contain",
    *,
    preset: str | None = None,
    pad: bool = False,
    pad_color: str | tuple[int, int, int] = "transparent",
    metadata_policy: str = "strip",
) -> str:
    """Resize an image, preserving supported animation frames and timing.

    ``preset`` and padding are opt-in additions.  The old width/height/percent
    contract still writes the same contained geometry when they are omitted.
    """
    if not os.path.isfile(path):
        return f"Error: File not found: {path}"
    try:
        source_format, frames, durations, loop, _ = _load_frames(path)
        try:
            original_w, original_h = frames[0].size
            geometry = resize_geometry(
                original_w,
                original_h,
                width=width,
                height=height,
                percent=percent,
                fit=fit,
                preset=preset,
                pad=pad,
            )
            new_size = (geometry["resize"]["width"], geometry["resize"]["height"])
            target_size = (geometry["target"]["width"], geometry["target"]["height"])
            destination = Path(output or path).expanduser()
            target_format = _format_from_extension(destination) or source_format
            resized = _resize_frame_list(frames, geometry, pad_color=pad_color)
            try:
                saved = _atomic_save_frames(
                    resized,
                    destination,
                    target_format,
                    durations=durations,
                    loop=loop,
                    metadata=_preservable_metadata(path, metadata_policy),
                )
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
            "fit": fit,
            "preset": preset,
            "pad": bool(target_size != new_size),
            "pad_color": str(pad_color),
            "metadata_policy": _normalize_metadata_policy(metadata_policy),
        }
        return f"Resized {original_w}×{original_h} → {target_size[0]}×{target_size[1]}, saved to {saved}" + _manifest_notice(saved, "resize_image", history)
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


def convert_image(
    path: str,
    format: str,
    output: str | None = None,
    quality: int = 85,
    flatten_animation: bool = False,
    *,
    metadata_policy: str = "strip",
    transparency_policy: str = "flatten",
    background: str | tuple[int, int, int] = "white",
) -> str:
    """Convert an image through a verified atomic replacement.

    Metadata and transparency choices are deliberately explicit advanced
    controls.  The defaults preserve the historical behavior: strip metadata
    and flatten alpha onto white for JPEG output.
    """
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
            save_frames = frames
            save_durations = durations
            if len(frames) > 1 and target_format not in _ANIMATED_FORMATS:
                if not flatten_animation:
                    return f"Error: {target_format} cannot preserve animation; set flatten_animation=true to explicitly flatten it"
                save_frames = frames[:1]
                save_durations = durations[:1]
            saved = _atomic_save_frames(
                save_frames,
                destination,
                target_format,
                durations=save_durations,
                loop=loop,
                quality=quality if target_format in {"JPEG", "WEBP"} else None,
                metadata=_preservable_metadata(path, metadata_policy),
                background=background,
                transparency_policy=transparency_policy,
            )
        finally:
            _close_frames(frames)
        return f"Converted to {target_format}, saved to {saved}" + _manifest_notice(
            saved,
            "convert_image",
            {
                "source": path,
                "format": target_format.casefold(),
                "quality": quality,
                "flatten_animation": bool(flatten_animation),
                "metadata_policy": _normalize_metadata_policy(metadata_policy),
                "transparency_policy": _normalize_transparency_policy(transparency_policy),
                "background": str(background),
            },
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
    clamp: bool = True,
    *,
    mode: str = "box",
    percent: Any = None,
    aspect_ratio: Any = None,
    crop_width: int | None = None,
    crop_height: int | None = None,
    anchor: str = "center",
    metadata_policy: str = "strip",
) -> str:
    """Crop an image after resolving a pixel, percent, center, or aspect mode."""
    if not os.path.isfile(path):
        return f"Error: File not found: {path}"
    try:
        source_format, frames, durations, loop, _ = _load_frames(path)
        try:
            width, height = frames[0].size
            geometry = crop_geometry(
                width,
                height,
                left=left,
                top=top,
                right=right,
                bottom=bottom,
                mode=mode,
                percent=percent,
                aspect_ratio=aspect_ratio,
                crop_width=crop_width,
                crop_height=crop_height,
                anchor=anchor,
                clamp=clamp,
            )
            left, top, right, bottom = geometry["box"]
            cropped = [frame.crop((left, top, right, bottom)) for frame in frames]
            try:
                destination = Path(output or path).expanduser()
                target_format = _format_from_extension(destination) or source_format
                saved = _atomic_save_frames(
                    cropped,
                    destination,
                    target_format,
                    durations=durations,
                    loop=loop,
                    metadata=_preservable_metadata(path, metadata_policy),
                )
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
                "clamp": bool(clamp),
                "mode": mode,
                "crop_details": geometry["details"],
                "metadata_policy": _normalize_metadata_policy(metadata_policy),
            },
        )
    except Exception as exc:
        return f"Error: {exc}"


def transform_image(
    path: str,
    *,
    resize: dict[str, Any] | None = None,
    crop: dict[str, Any] | None = None,
    convert: dict[str, Any] | None = None,
    output: str | None = None,
    action: str = "transform_image",
) -> str:
    """Apply a planned resize/crop/convert sequence with one atomic write.

    This is intentionally separate from the legacy one-operation helpers. It
    gives batch processing a single final manifest row rather than temporary
    intermediate assets, and it never writes the source unless a caller
    explicitly supplies the source as ``output``.
    """
    if not os.path.isfile(path):
        return f"Error: File not found: {path}"
    if not any(item is not None for item in (resize, crop, convert)):
        return "Error: Specify at least one of resize, crop, or convert"
    try:
        source_format, frames, durations, loop, _ = _load_frames(path)
        working = frames
        try:
            original_size = working[0].size
            resize_details: dict[str, Any] | None = None
            crop_details: dict[str, Any] | None = None
            target_format = source_format
            quality: int | None = None
            flatten_animation = False
            metadata_policy = "strip"
            background: str | tuple[int, int, int] = "white"
            transparency_policy = "flatten"

            if resize is not None:
                if not isinstance(resize, dict):
                    raise ValueError("resize must be an object")
                geometry = resize_geometry(
                    working[0].width,
                    working[0].height,
                    width=resize.get("width"),
                    height=resize.get("height"),
                    percent=resize.get("percent"),
                    fit=str(resize.get("fit") or "contain"),
                    preset=resize.get("preset"),
                    pad=bool(resize.get("pad", False)),
                )
                resized = _resize_frame_list(working, geometry, pad_color=resize.get("pad_color", "transparent"))
                _close_frames(working)
                working = resized
                resize_details = geometry
                metadata_policy = str(resize.get("metadata_policy") or metadata_policy)

            if crop is not None:
                if not isinstance(crop, dict):
                    raise ValueError("crop must be an object")
                geometry = crop_geometry(
                    working[0].width,
                    working[0].height,
                    left=crop.get("left", 0),
                    top=crop.get("top", 0),
                    right=crop.get("right"),
                    bottom=crop.get("bottom"),
                    mode=str(crop.get("mode") or "box"),
                    percent=crop.get("percent"),
                    aspect_ratio=crop.get("aspect_ratio"),
                    crop_width=crop.get("crop_width"),
                    crop_height=crop.get("crop_height"),
                    anchor=str(crop.get("anchor") or "center"),
                    clamp=bool(crop.get("clamp", True)),
                )
                cropped = [frame.crop(geometry["box"]) for frame in working]
                _close_frames(working)
                working = cropped
                crop_details = geometry
                metadata_policy = str(crop.get("metadata_policy") or metadata_policy)

            if convert is not None:
                if not isinstance(convert, dict):
                    raise ValueError("convert must be an object")
                target_format = _format_from_name(str(convert.get("format") or ""))
                quality = int(convert.get("quality", 85))
                if quality < 1 or quality > 100:
                    raise ValueError("quality must be between 1 and 100")
                flatten_animation = bool(convert.get("flatten_animation", False))
                metadata_policy = str(convert.get("metadata_policy") or metadata_policy)
                background = convert.get("background", background)
                transparency_policy = str(convert.get("transparency_policy") or transparency_policy)

            destination = (
                _reconciled_convert_destination(path, output, target_format)
                if convert is not None
                else Path(output or path).expanduser()
            )
            save_frames = working
            save_durations = durations
            if len(working) > 1 and target_format not in _ANIMATED_FORMATS:
                if not flatten_animation:
                    raise ValueError(
                        f"{target_format} cannot preserve animation; set flatten_animation=true to explicitly flatten it"
                    )
                save_frames = working[:1]
                save_durations = durations[:1]
            saved = _atomic_save_frames(
                save_frames,
                destination,
                target_format,
                durations=save_durations,
                loop=loop,
                quality=quality if target_format in {"JPEG", "WEBP"} else None,
                metadata=_preservable_metadata(path, metadata_policy),
                background=background,
                transparency_policy=transparency_policy,
            )
            history = {
                "source": path,
                "original_width": original_size[0],
                "original_height": original_size[1],
                "resize": resize_details,
                "crop": crop_details,
                "convert": {
                    "format": target_format.casefold(),
                    "quality": quality,
                    "flatten_animation": flatten_animation,
                    "metadata_policy": _normalize_metadata_policy(metadata_policy),
                    "transparency_policy": _normalize_transparency_policy(transparency_policy),
                } if convert is not None else None,
            }
        finally:
            _close_frames(working)
        return (
            f"Transformed {original_size[0]}×{original_size[1]} → {saved}, saved to {saved}"
            + _manifest_notice(saved, action, history)
        )
    except Exception as exc:
        return f"Error: {exc}"
