"""Image editing operations using Pillow (PIL)."""

from __future__ import annotations

import os

from PIL import Image, ImageOps

from ares.tools.asset_manifest import record_asset


def _human_size(nbytes: int) -> str:
    """Convert byte count to human-readable string."""
    for unit in ("B", "KB", "MB", "GB"):
        if nbytes < 1024:
            return f"{nbytes:.1f} {unit}"
        nbytes /= 1024
    return f"{nbytes:.1f} TB"


def image_info(path: str) -> str:
    """Get metadata about an image file.

    Args:
        path: Path to the image file.

    Returns:
        Formatted string with image metadata.
    """
    if not os.path.isfile(path):
        return f"Error: File not found: {path}"

    try:
        file_size = os.path.getsize(path)
        size_human = _human_size(file_size)

        with Image.open(path) as img:
            img.load()

            info = [
                f"Format: {img.format} ({img.format_description})",
                f"Dimensions: {img.size[0]}\u00d7{img.size[1]}",
                f"Mode: {img.mode}",
                f"Size: {size_human}",
            ]

            is_animated = getattr(img, "is_animated", False)
            n_frames = getattr(img, "n_frames", 1)
            if is_animated:
                info.append(f"Animated: Yes ({n_frames} frames)")

            exif = img.getexif()
            if exif:
                orientation = exif.get(274, 1)
                if orientation != 1:
                    info.append(f"EXIF Orientation: {orientation}")
                make = exif.get(271, "")
                model = exif.get(272, "")
                if make or model:
                    info.append(f"Camera: {make} {model}".strip())

        return "\n".join(info)

    except Exception as e:
        return f"Error reading image: {e}"


def resize_image(
    path: str,
    width: int | None = None,
    height: int | None = None,
    percent: int | None = None,
    output: str | None = None,
) -> str:
    """Resize an image preserving aspect ratio.

    Args:
        path: Source image path.
        width: Target width (scales proportionally if height not given).
        height: Target height (scales proportionally if width not given).
        percent: Scale by percentage (e.g. 50 = half size).
        output: Output path (default: overwrite source).

    Returns:
        Status message with dimensions.
    """
    if not os.path.isfile(path):
        return f"Error: File not found: {path}"

    try:
        img = Image.open(path)
        img.load()
        img = ImageOps.exif_transpose(img)

        original_w, original_h = img.size

        if percent is not None:
            factor = percent / 100.0
            new_size = (int(original_w * factor), int(original_h * factor))
        elif width is not None and height is not None:
            ratio = min(width / original_w, height / original_h)
            new_size = (int(original_w * ratio), int(original_h * ratio))
        elif width is not None:
            ratio = width / original_w
            new_size = (width, int(original_h * ratio))
        elif height is not None:
            ratio = height / original_h
            new_size = (int(original_w * ratio), height)
        else:
            return "Error: Specify width, height, or percent"

        img = img.resize(new_size, Image.Resampling.LANCZOS)
        save_path = output or path
        img.save(save_path)
        img.close()
        manifest = record_asset(
            save_path,
            action="resize_image",
            history={
                "source": path,
                "original_width": original_w,
                "original_height": original_h,
                "width": width,
                "height": height,
                "percent": percent,
            },
        )

        return f"Resized {original_w}\u00d7{original_h} \u2192 {new_size[0]}\u00d7{new_size[1]}, saved to {save_path}\nManifest: {manifest}"

    except Exception as e:
        return f"Error: {e}"


def _fmt_upper(fmt: str) -> str:
    """Convert format string to PIL uppercase format name."""
    mapping = {
        "png": "PNG",
        "jpeg": "JPEG",
        "webp": "WEBP",
        "bmp": "BMP",
        "gif": "GIF",
    }
    return mapping.get(fmt, fmt.upper())


def convert_image(
    path: str,
    format: str,
    output: str | None = None,
    quality: int = 85,
) -> str:
    """Convert an image to a different format.

    Args:
        path: Source image path.
        format: Target format (png, jpeg, webp, bmp, gif).
        output: Output path (default: same name, new extension).
        quality: JPEG/WebP quality (1-100, default 85).

    Returns:
        Status message.
    """
    if not os.path.isfile(path):
        return f"Error: File not found: {path}"

    supported = {"png", "jpeg", "jpg", "webp", "bmp", "gif"}
    fmt_lower = format.lower()
    if fmt_lower == "jpg":
        fmt_lower = "jpeg"
    if fmt_lower not in supported:
        return f"Error: Unsupported format: {format}. Use one of: {', '.join(sorted(supported))}"

    try:
        img = Image.open(path)
        img.load()
        img = ImageOps.exif_transpose(img)

        if output is None:
            base = os.path.splitext(path)[0]
            ext = ".jpeg" if fmt_lower == "jpeg" else f".{fmt_lower}"
            save_path = base + ext
        else:
            save_path = output

        if fmt_lower == "jpeg" and img.mode in ("RGBA", "LA", "P"):
            if img.mode == "P":
                img = img.convert("RGBA")
            background = Image.new("RGB", img.size, (255, 255, 255))
            if img.mode == "RGBA":
                background.paste(img, mask=img.split()[3])
            else:
                background.paste(img)
            img = background

        if img.mode == "P" and fmt_lower != "gif":
            img = img.convert("RGB")

        save_kwargs = {}
        if fmt_lower in ("jpeg", "webp"):
            save_kwargs["quality"] = quality
        if fmt_lower == "jpeg":
            save_kwargs["optimize"] = True

        img.save(save_path, format=_fmt_upper(fmt_lower), **save_kwargs)
        img.close()
        manifest = record_asset(
            save_path,
            action="convert_image",
            history={
                "source": path,
                "format": fmt_lower,
                "quality": quality,
            },
        )

        return f"Converted to {fmt_lower.upper()}, saved to {save_path}\nManifest: {manifest}"

    except Exception as e:
        return f"Error: {e}"


def crop_image(
    path: str,
    left: int = 0,
    top: int = 0,
    right: int | None = None,
    bottom: int | None = None,
    output: str | None = None,
) -> str:
    """Crop a rectangular region from an image.

    Args:
        path: Source image path.
        left: Left edge in pixels (default 0).
        top: Top edge in pixels (default 0).
        right: Right edge in pixels (exclusive, required).
        bottom: Bottom edge in pixels (exclusive, required).
        output: Output path (default: overwrite source).

    Returns:
        Status message with cropped dimensions.
    """
    if not os.path.isfile(path):
        return f"Error: File not found: {path}"

    if right is None or bottom is None:
        return "Error: right and bottom parameters are required"

    if right <= left or bottom <= top:
        return f"Error: Invalid crop region ({left},{top}) \u2192 ({right},{bottom})"

    try:
        img = Image.open(path)
        img.load()
        img = ImageOps.exif_transpose(img)

        w, h = img.size
        left = max(0, min(left, w))
        top = max(0, min(top, h))
        right = max(left, min(right, w))
        bottom = max(top, min(bottom, h))

        cropped = img.crop((left, top, right, bottom))
        save_path = output or path
        cropped.save(save_path)
        cropped.close()
        img.close()
        manifest = record_asset(
            save_path,
            action="crop_image",
            history={
                "source": path,
                "left": left,
                "top": top,
                "right": right,
                "bottom": bottom,
                "original_width": w,
                "original_height": h,
            },
        )

        return f"Cropped ({left},{top}) \u2192 ({right},{bottom}) = {right - left}\u00d7{bottom - top}, saved to {save_path}\nManifest: {manifest}"

    except Exception as e:
        return f"Error: {e}"
