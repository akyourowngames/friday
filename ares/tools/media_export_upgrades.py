"""Opt-in planning and projection helpers for media, provenance, and exports.

The established image and export tools deliberately keep their small, text
based public interfaces.  This module is a side-effect-free layer intended for
new callers that need richer previews before invoking those tools.  Nothing in
this module writes an image, changes an action ledger, or writes an export.

Keeping the logic here makes it possible to add expressive tool modes without
changing the meaning of a legacy ``generate_image``/``resize_image``/``export``
call.  It also makes the validation rules straightforward to unit test.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone
from fractions import Fraction
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


class UpgradeValidationError(ValueError):
    """Raised when a proposed rich media/export operation is invalid."""


_SUPPORTED_IMAGE_FORMATS = {"PNG", "JPEG", "WEBP", "BMP", "GIF"}
_FORMAT_ALIASES = {"JPG": "JPEG", "TIF": "TIFF"}
_ANIMATION_FORMATS = {"PNG", "WEBP", "GIF"}
_SECRET_KEY = re.compile(
    r"(?:api[_-]?key|access[_-]?key|token|secret|password|passwd|credential|authorization|"
    r"private[_-]?key|client[_-]?secret)",
    re.IGNORECASE,
)
_SECRET_VALUE = re.compile(
    r"^(?:sk[-_][A-Za-z0-9_-]{8,}|(?:ghp|github_pat)_[A-Za-z0-9_]{8,}|"
    r"AIza[A-Za-z0-9_-]{12,}|Bearer\s+\S{8,})$",
    re.IGNORECASE,
)
_SAFE_STYLE = re.compile(r"\s+")


def _json_bytes(value: Any) -> bytes:
    """Return a deterministic JSON representation or a useful validation error."""
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise UpgradeValidationError(f"value is not JSON-serializable: {exc}") from exc


def checksum_payload(value: Any) -> str:
    """Calculate a stable SHA-256 checksum for a JSON-compatible value."""
    return hashlib.sha256(_json_bytes(value)).hexdigest()


def _positive_int(value: Any, field: str, *, maximum: int = 100_000) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise UpgradeValidationError(f"{field} must be an integer") from exc
    if result <= 0 or result > maximum:
        raise UpgradeValidationError(f"{field} must be between 1 and {maximum}")
    return result


def _nonnegative_int(value: Any, field: str) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise UpgradeValidationError(f"{field} must be an integer") from exc
    if result < 0:
        raise UpgradeValidationError(f"{field} must not be negative")
    return result


def _normalise_format(value: Any, *, allow_tiff: bool = False) -> str:
    label = str(value or "").strip().upper()
    label = _FORMAT_ALIASES.get(label, label)
    allowed = set(_SUPPORTED_IMAGE_FORMATS)
    if allow_tiff:
        allowed.add("TIFF")
    if label not in allowed:
        supported = ", ".join(sorted(allowed)).lower()
        raise UpgradeValidationError(f"unsupported image format {value!r}; use one of: {supported}")
    return label


def _normalise_text(value: Any, field: str, *, maximum: int = 10_000, required: bool = False) -> str:
    text = str(value or "").strip()
    if required and not text:
        raise UpgradeValidationError(f"{field} is required")
    if len(text) > maximum:
        raise UpgradeValidationError(f"{field} must be at most {maximum} characters")
    return text


def normalize_aspect_ratio(value: Any) -> dict[str, Any] | None:
    """Normalize ``16:9``/``1.777``/``(16, 9)`` aspect inputs.

    ``None`` is meaningful: it asks a caller to retain the supplied target
    dimensions rather than silently selecting an arbitrary ratio.
    """
    if value is None or value == "":
        return None
    numerator: int
    denominator: int
    if isinstance(value, str):
        raw = value.strip().replace("/", ":")
        if ":" in raw:
            left, right, *rest = raw.split(":")
            if rest:
                raise UpgradeValidationError("aspect_ratio must contain exactly one ':'")
            numerator = _positive_int(left, "aspect_ratio numerator", maximum=10_000)
            denominator = _positive_int(right, "aspect_ratio denominator", maximum=10_000)
        else:
            try:
                ratio = Fraction(float(raw)).limit_denominator(10_000)
            except (TypeError, ValueError, ZeroDivisionError) as exc:
                raise UpgradeValidationError("aspect_ratio must be like '16:9' or 1.777") from exc
            numerator, denominator = ratio.numerator, ratio.denominator
    elif isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        if len(value) != 2:
            raise UpgradeValidationError("aspect_ratio sequence must contain [width, height]")
        numerator = _positive_int(value[0], "aspect_ratio numerator", maximum=10_000)
        denominator = _positive_int(value[1], "aspect_ratio denominator", maximum=10_000)
    else:
        try:
            ratio = Fraction(float(value)).limit_denominator(10_000)
        except (TypeError, ValueError, ZeroDivisionError) as exc:
            raise UpgradeValidationError("aspect_ratio must be like '16:9' or 1.777") from exc
        numerator, denominator = ratio.numerator, ratio.denominator
    fraction = Fraction(numerator, denominator)
    if fraction <= 0:
        raise UpgradeValidationError("aspect_ratio must be greater than zero")
    return {
        "label": f"{fraction.numerator}:{fraction.denominator}",
        "numerator": fraction.numerator,
        "denominator": fraction.denominator,
        "value": float(fraction),
    }


def _fit_aspect(width: int, height: int, aspect: dict[str, Any] | None) -> tuple[int, int]:
    if aspect is None:
        return width, height
    ratio = float(aspect["value"])
    available = width / height
    if available > ratio:
        candidate = max(1, round(height * ratio))
        return candidate, height
    return width, max(1, round(width / ratio))


def _style_list(value: Any) -> list[str]:
    if value is None or value == "":
        return []
    raw_items = [value] if isinstance(value, str) else value
    if not isinstance(raw_items, Sequence) or isinstance(raw_items, (bytes, bytearray)):
        raise UpgradeValidationError("style must be a string or a list of style labels")
    styles: list[str] = []
    seen: set[str] = set()
    for raw in raw_items:
        item = _SAFE_STYLE.sub(" ", str(raw or "").strip())
        if not item:
            continue
        if len(item) > 120:
            raise UpgradeValidationError("each style label must be at most 120 characters")
        key = item.casefold()
        if key not in seen:
            seen.add(key)
            styles.append(item)
    return styles


def _fallback_policy(value: Any) -> list[dict[str, Any]]:
    """Normalize fallback instructions without picking or invoking a provider."""
    if value is None or value is False:
        return []
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, Sequence) or isinstance(value, (bytes, bytearray)):
        raise UpgradeValidationError("fallbacks must be a model label, a list, or a policy mapping")
    normalized: list[dict[str, Any]] = []
    for index, raw in enumerate(value):
        if isinstance(raw, Mapping):
            strategy = _normalise_text(raw.get("strategy") or "retry", "fallback strategy", maximum=40)
            model = _normalise_text(raw.get("model"), "fallback model", maximum=160)
            item = {"order": index + 1, "strategy": strategy}
            if model:
                item["model"] = model
            if raw.get("reason"):
                item["reason"] = _normalise_text(raw["reason"], "fallback reason", maximum=240)
        else:
            model = _normalise_text(raw, "fallback model", maximum=160, required=True)
            item = {"order": index + 1, "strategy": "retry", "model": model}
        normalized.append(item)
    return normalized


def _variant_seed(seed: int | None, index: int) -> int | None:
    if seed is None:
        return None
    # Stay in the signed 32-bit range accepted by common image providers while
    # avoiding a simple ``seed + index`` pattern for large batches.
    digest = hashlib.sha256(f"{seed}:variation:{index}".encode("utf-8")).digest()
    return int.from_bytes(digest[:4], "big") & 0x7FFFFFFF


def build_image_variation_manifest(
    prompt: str,
    *,
    width: int = 1024,
    height: int = 1024,
    model: str = "flux",
    seed: int | None = None,
    variations: int = 1,
    style: str | Sequence[str] | None = None,
    aspect_ratio: str | float | Sequence[int] | None = None,
    negative_prompt: str | None = None,
    fallbacks: str | Sequence[str | Mapping[str, Any]] | None = None,
    request_id: str | None = None,
) -> dict[str, Any]:
    """Create a deterministic, provider-neutral image variation manifest.

    The result carries every generation decision needed by a future executor:
    target geometry, explicit or unseeded seed policy, styles, a negative
    prompt, and ordered fallback metadata.  It intentionally does *not* make a
    network request or change how the legacy image generation tool works.
    """
    clean_prompt = _normalise_text(prompt, "prompt", required=True)
    base_width = _positive_int(width, "width")
    base_height = _positive_int(height, "height")
    variation_count = _positive_int(variations, "variations", maximum=32)
    clean_model = _normalise_text(model or "flux", "model", maximum=160, required=True)
    normalized_seed: int | None
    if seed is None or seed == "":
        normalized_seed = None
    else:
        try:
            normalized_seed = int(seed)
        except (TypeError, ValueError) as exc:
            raise UpgradeValidationError("seed must be an integer") from exc
        if normalized_seed < 0 or normalized_seed > 0x7FFFFFFF:
            raise UpgradeValidationError("seed must be between 0 and 2147483647")
    ratio = normalize_aspect_ratio(aspect_ratio)
    target_width, target_height = _fit_aspect(base_width, base_height, ratio)
    styles = _style_list(style)
    negative = _normalise_text(negative_prompt, "negative_prompt", maximum=10_000)
    fallback_policy = _fallback_policy(fallbacks)
    stable_identity = checksum_payload(
        {
            "prompt": clean_prompt,
            "model": clean_model,
            "size": [target_width, target_height],
            "seed": normalized_seed,
            "style": styles,
            "negative": negative,
        }
    )[:20]
    identifier = _normalise_text(request_id, "request_id", maximum=160) or f"image-{stable_identity}"
    effective_prompt = clean_prompt
    if styles:
        effective_prompt = f"{clean_prompt}. Style: {', '.join(styles)}."
    variants = [
        {
            "variant_id": f"{identifier}-{index + 1:02d}",
            "index": index,
            "seed": _variant_seed(normalized_seed, index),
            "prompt": effective_prompt,
            "negative_prompt": negative,
            "target_size": {"width": target_width, "height": target_height},
            "model": clean_model,
        }
        for index in range(variation_count)
    ]
    return {
        "kind": "image_variation_manifest",
        "schema_version": 1,
        "request_id": identifier,
        "prompt": clean_prompt,
        "effective_prompt": effective_prompt,
        "negative_prompt": negative,
        "style": styles,
        "model": clean_model,
        "requested_size": {"width": base_width, "height": base_height},
        "target_size": {"width": target_width, "height": target_height},
        "aspect_ratio": ratio or {
            "label": f"{target_width}:{target_height}",
            "numerator": target_width,
            "denominator": target_height,
            "value": target_width / target_height,
        },
        "seed": {
            "value": normalized_seed,
            "strategy": "derived-per-variation" if normalized_seed is not None else "provider-random",
        },
        "fallback_policy": fallback_policy,
        "variants": variants,
        "reproducibility_id": stable_identity,
    }


def normalize_image_metadata(metadata: Mapping[str, Any]) -> dict[str, Any]:
    """Validate and normalize image metadata used by planning/verification."""
    if not isinstance(metadata, Mapping):
        raise UpgradeValidationError("image metadata must be a mapping")
    width = _positive_int(metadata.get("width"), "width")
    height = _positive_int(metadata.get("height"), "height")
    result: dict[str, Any] = {"width": width, "height": height}
    if metadata.get("format") not in (None, ""):
        result["format"] = _normalise_format(metadata["format"], allow_tiff=True)
    if metadata.get("path") not in (None, ""):
        result["path"] = str(metadata["path"])
    if metadata.get("bytes") not in (None, ""):
        result["bytes"] = _nonnegative_int(metadata["bytes"], "bytes")
    if metadata.get("frame_count") not in (None, ""):
        result["frame_count"] = _positive_int(metadata["frame_count"], "frame_count", maximum=1_000_000)
    if metadata.get("animated") is not None:
        result["animated"] = bool(metadata["animated"])
    elif int(result.get("frame_count", 1)) > 1:
        result["animated"] = True
    for key in ("mode", "checksum_sha256", "source"):
        if metadata.get(key) not in (None, ""):
            result[key] = str(metadata[key])
    return result


def _resize_geometry(
    width: int,
    height: int,
    options: Mapping[str, Any],
) -> tuple[dict[str, int], dict[str, Any]]:
    if not isinstance(options, Mapping):
        raise UpgradeValidationError("resize must be a mapping")
    target_width = options.get("width")
    target_height = options.get("height")
    percent = options.get("percent")
    chosen = sum(value not in (None, "") for value in (target_width, target_height, percent))
    if not chosen:
        raise UpgradeValidationError("resize requires width, height, or percent")
    if percent not in (None, ""):
        if target_width not in (None, "") or target_height not in (None, ""):
            raise UpgradeValidationError("resize percent cannot be combined with width or height")
        try:
            multiplier = float(percent) / 100.0
        except (TypeError, ValueError) as exc:
            raise UpgradeValidationError("resize percent must be numeric") from exc
        if multiplier <= 0 or multiplier > 100:
            raise UpgradeValidationError("resize percent must be greater than 0 and at most 10000")
        target = {"width": max(1, round(width * multiplier)), "height": max(1, round(height * multiplier))}
        return target, {"mode": "percent", "percent": float(percent), "preserves_aspect": True}
    fit = str(options.get("fit") or "contain").strip().casefold()
    if fit not in {"contain", "cover", "stretch"}:
        raise UpgradeValidationError("resize fit must be contain, cover, or stretch")
    if target_width not in (None, ""):
        target_width = _positive_int(target_width, "resize width")
    if target_height not in (None, ""):
        target_height = _positive_int(target_height, "resize height")
    if target_width in (None, ""):
        target = {"height": target_height, "width": max(1, round(width * (target_height / height)))}
    elif target_height in (None, ""):
        target = {"width": target_width, "height": max(1, round(height * (target_width / width)))}
    elif fit == "stretch":
        target = {"width": target_width, "height": target_height}
    else:
        multiplier = min(target_width / width, target_height / height) if fit == "contain" else max(target_width / width, target_height / height)
        target = {"width": max(1, round(width * multiplier)), "height": max(1, round(height * multiplier))}
    return target, {"mode": fit, "preserves_aspect": fit != "stretch"}


def _crop_geometry(width: int, height: int, options: Mapping[str, Any]) -> tuple[dict[str, int], dict[str, Any]]:
    if not isinstance(options, Mapping):
        raise UpgradeValidationError("crop must be a mapping")
    try:
        requested = {
            "left": int(options.get("left", 0)),
            "top": int(options.get("top", 0)),
            "right": int(options["right"]),
            "bottom": int(options["bottom"]),
        }
    except (KeyError, TypeError, ValueError) as exc:
        raise UpgradeValidationError("crop requires integer left, top, right, and bottom coordinates") from exc
    clamp = bool(options.get("clamp", True))
    actual = dict(requested)
    if clamp:
        actual["left"] = max(0, min(actual["left"], width))
        actual["right"] = max(0, min(actual["right"], width))
        actual["top"] = max(0, min(actual["top"], height))
        actual["bottom"] = max(0, min(actual["bottom"], height))
    elif (
        actual["left"] < 0
        or actual["top"] < 0
        or actual["right"] > width
        or actual["bottom"] > height
    ):
        raise UpgradeValidationError(f"crop geometry exceeds image bounds {width}x{height}")
    if actual["left"] >= actual["right"] or actual["top"] >= actual["bottom"]:
        raise UpgradeValidationError(f"crop region has no overlap with image bounds {width}x{height}")
    target = {"width": actual["right"] - actual["left"], "height": actual["bottom"] - actual["top"]}
    return target, {"requested": requested, "applied": actual, "clamped": requested != actual}


def _convert_geometry(source: Mapping[str, Any], options: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    if not isinstance(options, Mapping):
        raise UpgradeValidationError("convert must be a mapping")
    target_format = _normalise_format(options.get("format"))
    quality = options.get("quality")
    details: dict[str, Any] = {"format": target_format}
    if quality not in (None, ""):
        quality_value = _positive_int(quality, "quality", maximum=100)
        details["quality"] = quality_value
        if target_format not in {"JPEG", "WEBP"}:
            details["quality_ignored"] = True
    animated = bool(source.get("animated")) or int(source.get("frame_count", 1)) > 1
    if animated and target_format not in _ANIMATION_FORMATS:
        if not bool(options.get("flatten_animation", False)):
            raise UpgradeValidationError(
                f"{target_format} cannot preserve animation; set flatten_animation=true to explicitly flatten it"
            )
        details["flatten_animation"] = True
    return {"format": target_format}, details


def plan_image_transform(
    source: Mapping[str, Any],
    *,
    resize: Mapping[str, Any] | None = None,
    crop: Mapping[str, Any] | None = None,
    convert: Mapping[str, Any] | None = None,
    output: str | Path | None = None,
) -> dict[str, Any]:
    """Plan a resize/crop/convert sequence and calculate its exact target.

    The sequence mirrors the least surprising execution order: resize, crop,
    then convert.  Crops default to the legacy editor's clamping behavior, but
    callers can request strict bounds with ``crop={..., "clamp": False}``.
    """
    source_metadata = normalize_image_metadata(source)
    target: dict[str, Any] = dict(source_metadata)
    operations: list[dict[str, Any]] = []
    warnings: list[str] = []
    if resize is not None:
        geometry, details = _resize_geometry(target["width"], target["height"], resize)
        before = {"width": target["width"], "height": target["height"]}
        target.update(geometry)
        operations.append({"operation": "resize", "before": before, "after": dict(geometry), "details": details})
    if crop is not None:
        geometry, details = _crop_geometry(target["width"], target["height"], crop)
        before = {"width": target["width"], "height": target["height"]}
        target.update(geometry)
        if details["clamped"]:
            warnings.append("crop coordinates were clamped to the current image bounds")
        operations.append({"operation": "crop", "before": before, "after": dict(geometry), "details": details})
    if convert is not None:
        geometry, details = _convert_geometry(target, convert)
        before_format = target.get("format")
        target.update(geometry)
        if details.get("quality_ignored"):
            warnings.append("quality only affects JPEG and WEBP encoders")
        operations.append(
            {"operation": "convert", "before": {"format": before_format}, "after": dict(geometry), "details": details}
        )
    if not operations:
        raise UpgradeValidationError("at least one of resize, crop, or convert is required")
    if output is not None:
        target["output"] = str(Path(output).expanduser())
    return {
        "kind": "image_transform_plan",
        "schema_version": 1,
        "source": source_metadata,
        "operations": operations,
        "target": target,
        "warnings": warnings,
        "valid": True,
    }


def _output_extension(format_name: str | None) -> str:
    mapping = {"PNG": ".png", "JPEG": ".jpg", "WEBP": ".webp", "BMP": ".bmp", "GIF": ".gif", "TIFF": ".tiff"}
    return mapping.get(format_name or "", ".img")


def plan_image_batch_transform(
    sources: Iterable[Mapping[str, Any]],
    transform: Mapping[str, Any] | None = None,
    *,
    output_dir: str | Path | None = None,
    collision_policy: str = "suffix",
) -> dict[str, Any]:
    """Build independent transform plans for a batch without touching files.

    Invalid items are reported beside valid items rather than cancelling the
    whole preview.  This lets a UI show exactly what needs correction before a
    user confirms a batch operation.
    """
    if transform is None:
        transform = {}
    if not isinstance(transform, Mapping):
        raise UpgradeValidationError("transform must be a mapping")
    policy = str(collision_policy or "suffix").casefold()
    if policy not in {"suffix", "error"}:
        raise UpgradeValidationError("collision_policy must be suffix or error")
    requested_dir = Path(output_dir).expanduser() if output_dir else None
    items: list[dict[str, Any]] = []
    allocated: set[str] = set()
    for index, raw_source in enumerate(sources):
        try:
            source_metadata = normalize_image_metadata(raw_source)
            source_name = Path(str(source_metadata.get("path") or f"image-{index + 1}")).stem or f"image-{index + 1}"
            plan = plan_image_transform(
                source_metadata,
                resize=transform.get("resize"),
                crop=transform.get("crop"),
                convert=transform.get("convert"),
                output=transform.get("output"),
            )
            explicit_output = plan["target"].get("output")
            if explicit_output:
                candidate = Path(str(explicit_output))
            elif requested_dir is not None:
                suffix = _output_extension(plan["target"].get("format") or source_metadata.get("format"))
                candidate = requested_dir / f"{source_name}{suffix}"
            else:
                candidate = None
            if candidate is not None:
                if str(candidate).casefold() in allocated:
                    if policy == "error":
                        raise UpgradeValidationError(f"planned output collision: {candidate}")
                    suffix_index = 2
                    while str(candidate).casefold() in allocated:
                        candidate = candidate.with_name(f"{candidate.stem}-{suffix_index}{candidate.suffix}")
                        suffix_index += 1
                plan["target"]["output"] = str(candidate)
                allocated.add(str(candidate).casefold())
            items.append({"index": index, "ok": True, "plan": plan})
        except (UpgradeValidationError, TypeError, ValueError) as exc:
            items.append({"index": index, "ok": False, "error": str(exc), "source": dict(raw_source) if isinstance(raw_source, Mapping) else None})
    return {
        "kind": "image_batch_transform_plan",
        "schema_version": 1,
        "transform": dict(transform),
        "items": items,
        "summary": {
            "total": len(items),
            "valid": sum(1 for item in items if item["ok"]),
            "invalid": sum(1 for item in items if not item["ok"]),
        },
    }


def validate_image_metadata(
    actual: Mapping[str, Any],
    expected: Mapping[str, Any],
    *,
    aspect_tolerance: float = 0.01,
) -> dict[str, Any]:
    """Compare decoded image metadata with a target/variation expectation."""
    actual_metadata = normalize_image_metadata(actual)
    if not isinstance(expected, Mapping):
        raise UpgradeValidationError("expected image metadata must be a mapping")
    checks: dict[str, bool] = {}
    warnings: list[str] = []
    errors: list[str] = []
    if "width" in expected:
        checks["width"] = actual_metadata["width"] == _positive_int(expected["width"], "expected width")
        if not checks["width"]:
            errors.append(f"width is {actual_metadata['width']}, expected {expected['width']}")
    if "height" in expected:
        checks["height"] = actual_metadata["height"] == _positive_int(expected["height"], "expected height")
        if not checks["height"]:
            errors.append(f"height is {actual_metadata['height']}, expected {expected['height']}")
    if "format" in expected and expected["format"] not in (None, ""):
        target_formats = expected["format"]
        if isinstance(target_formats, str):
            target_formats = [target_formats]
        expected_formats = {_normalise_format(value, allow_tiff=True) for value in target_formats}
        checks["format"] = actual_metadata.get("format") in expected_formats
        if not checks["format"]:
            errors.append(f"format is {actual_metadata.get('format')!r}, expected one of {sorted(expected_formats)}")
    if "max_bytes" in expected and actual_metadata.get("bytes") is not None:
        maximum = _nonnegative_int(expected["max_bytes"], "max_bytes")
        checks["max_bytes"] = actual_metadata["bytes"] <= maximum
        if not checks["max_bytes"]:
            errors.append(f"image is {actual_metadata['bytes']} bytes, exceeds max_bytes {maximum}")
    if "min_bytes" in expected and actual_metadata.get("bytes") is not None:
        minimum = _nonnegative_int(expected["min_bytes"], "min_bytes")
        checks["min_bytes"] = actual_metadata["bytes"] >= minimum
        if not checks["min_bytes"]:
            errors.append(f"image is {actual_metadata['bytes']} bytes, below min_bytes {minimum}")
    ratio_input = expected.get("aspect_ratio")
    if ratio_input is not None:
        ratio = normalize_aspect_ratio(ratio_input)
        assert ratio is not None
        tolerance = float(aspect_tolerance)
        if tolerance < 0 or tolerance > 1:
            raise UpgradeValidationError("aspect_tolerance must be between 0 and 1")
        actual_ratio = actual_metadata["width"] / actual_metadata["height"]
        checks["aspect_ratio"] = abs(actual_ratio - float(ratio["value"])) <= tolerance
        if not checks["aspect_ratio"]:
            errors.append(f"aspect ratio is {actual_ratio:.5f}, expected {ratio['label']} (+/- {tolerance})")
    if "animated" in expected:
        expected_animated = bool(expected["animated"])
        actual_animated = bool(actual_metadata.get("animated"))
        checks["animated"] = actual_animated == expected_animated
        if not checks["animated"]:
            errors.append(f"animated is {actual_animated}, expected {expected_animated}")
    if not checks:
        warnings.append("no explicit metadata assertions were supplied")
    return {
        "ok": not errors,
        "actual": actual_metadata,
        "expected": dict(expected),
        "checks": checks,
        "warnings": warnings,
        "errors": errors,
    }


def validate_transform_result(plan: Mapping[str, Any], actual: Mapping[str, Any]) -> dict[str, Any]:
    """Verify a transform output against a plan created by this module."""
    if not isinstance(plan, Mapping) or not isinstance(plan.get("target"), Mapping):
        raise UpgradeValidationError("plan must be an image transform plan with a target")
    target = dict(plan["target"])
    # Output destinations are execution details, not decoded image metadata.
    target.pop("output", None)
    return validate_image_metadata(actual, target)


# ── Action-history querying and projection ────────────────────────────────


def _parse_action_time(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def _action_projection(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Create a stable, content-minimized action projection from a ledger row."""
    action_id = raw.get("action_id")
    try:
        action_id = int(action_id) if action_id is not None else None
    except (TypeError, ValueError):
        action_id = None
    tags = raw.get("tags")
    if not isinstance(tags, Sequence) or isinstance(tags, (str, bytes, bytearray)):
        tags = []
    return {
        "action_id": action_id,
        "action_type": _normalise_text(raw.get("action_type"), "action_type", maximum=100),
        "target": _normalise_text(raw.get("target"), "target", maximum=512),
        "summary": _normalise_text(raw.get("summary"), "summary", maximum=360),
        "tool_name": _normalise_text(raw.get("tool_name"), "tool_name", maximum=160),
        "session_id": _normalise_text(raw.get("session_id"), "session_id", maximum=160),
        "task_id": _normalise_text(raw.get("task_id"), "task_id", maximum=160),
        "created_at": _normalise_text(raw.get("created_at"), "created_at", maximum=80),
        "tags": sorted({_normalise_text(tag, "tag", maximum=80) for tag in tags if str(tag or "").strip()}),
    }


def _as_set(value: str | Sequence[str] | None, field: str) -> set[str]:
    if value is None or value == "":
        return set()
    if isinstance(value, str):
        values = [value]
    elif isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        values = value
    else:
        raise UpgradeValidationError(f"{field} must be a string or a sequence of strings")
    return {str(item).strip().casefold() for item in values if str(item).strip()}


def query_action_history(
    actions: Iterable[Mapping[str, Any]],
    *,
    query: str = "",
    action_types: str | Sequence[str] | None = None,
    tags: str | Sequence[str] | None = None,
    tag_match: str = "any",
    target: str | None = None,
    tool_name: str | None = None,
    task_id: str | None = None,
    session_id: str | None = None,
    since: str | None = None,
    until: str | None = None,
    limit: int = 50,
    cursor: int | str | None = None,
    sort: str = "desc",
) -> dict[str, Any]:
    """Filter, paginate, and project privacy-minimized action ledger rows."""
    try:
        bounded_limit = max(1, min(int(limit), 10_000))
    except (TypeError, ValueError) as exc:
        raise UpgradeValidationError("limit must be an integer") from exc
    try:
        offset = max(0, int(cursor or 0))
    except (TypeError, ValueError) as exc:
        raise UpgradeValidationError("cursor must be a non-negative integer") from exc
    sort_order = str(sort or "desc").casefold()
    if sort_order not in {"asc", "desc"}:
        raise UpgradeValidationError("sort must be asc or desc")
    tag_policy = str(tag_match or "any").casefold()
    if tag_policy not in {"any", "all"}:
        raise UpgradeValidationError("tag_match must be any or all")
    type_set = _as_set(action_types, "action_types")
    tag_set = _as_set(tags, "tags")
    query_terms = [term.casefold() for term in _normalise_text(query, "query", maximum=300).split()]
    target_filter = _normalise_text(target, "target", maximum=512).casefold()
    tool_filter = _normalise_text(tool_name, "tool_name", maximum=160).casefold()
    task_filter = _normalise_text(task_id, "task_id", maximum=160).casefold()
    session_filter = _normalise_text(session_id, "session_id", maximum=160).casefold()
    since_time = _parse_action_time(since)
    until_time = _parse_action_time(until)
    if since and since_time is None:
        raise UpgradeValidationError("since must be an ISO-8601 timestamp")
    if until and until_time is None:
        raise UpgradeValidationError("until must be an ISO-8601 timestamp")
    if since_time and until_time and since_time > until_time:
        raise UpgradeValidationError("since must not be later than until")
    warnings: list[str] = []
    projected: list[dict[str, Any]] = []
    for raw in actions:
        if not isinstance(raw, Mapping):
            warnings.append("ignored a malformed action row")
            continue
        item = _action_projection(raw)
        item_time = _parse_action_time(item["created_at"])
        if item["created_at"] and item_time is None:
            warnings.append(f"action {item.get('action_id') or '?'} has an unparseable timestamp")
        item_tags = {tag.casefold() for tag in item["tags"]}
        haystack = " ".join((item["action_type"], item["target"], item["summary"], item["tool_name"], " ".join(item["tags"]))).casefold()
        if type_set and item["action_type"].casefold() not in type_set:
            continue
        if tag_set and ((not (item_tags & tag_set)) if tag_policy == "any" else not tag_set.issubset(item_tags)):
            continue
        if target_filter and target_filter not in item["target"].casefold():
            continue
        if tool_filter and tool_filter != item["tool_name"].casefold():
            continue
        if task_filter and task_filter != item["task_id"].casefold():
            continue
        if session_filter and session_filter != item["session_id"].casefold():
            continue
        if query_terms and not all(term in haystack for term in query_terms):
            continue
        if since_time and (item_time is None or item_time < since_time):
            continue
        if until_time and (item_time is None or item_time > until_time):
            continue
        projected.append(item)
    minimum = datetime.min.replace(tzinfo=timezone.utc)
    projected.sort(
        key=lambda item: (_parse_action_time(item["created_at"]) or minimum, item.get("action_id") or -1),
        reverse=sort_order == "desc",
    )
    page = projected[offset : offset + bounded_limit]
    next_cursor = offset + bounded_limit if offset + bounded_limit < len(projected) else None
    return {
        "kind": "action_history_query",
        "items": page,
        "total": len(projected),
        "limit": bounded_limit,
        "cursor": offset,
        "next_cursor": str(next_cursor) if next_cursor is not None else None,
        "sort": sort_order,
        "filters": {
            "query": query_terms,
            "action_types": sorted(type_set),
            "tags": sorted(tag_set),
            "tag_match": tag_policy,
            "target": target_filter,
            "tool_name": tool_filter,
            "task_id": task_filter,
            "session_id": session_filter,
            "since": since_time.isoformat().replace("+00:00", "Z") if since_time else None,
            "until": until_time.isoformat().replace("+00:00", "Z") if until_time else None,
        },
        "warnings": sorted(set(warnings)),
    }


def build_action_timeline(
    actions: Iterable[Mapping[str, Any]],
    *,
    bucket: str = "day",
    max_items_per_bucket: int = 50,
) -> dict[str, Any]:
    """Group action rows into a compact timeline by day/hour/task/session/type."""
    bucket_kind = str(bucket or "day").casefold()
    if bucket_kind not in {"day", "hour", "task", "session", "type"}:
        raise UpgradeValidationError("bucket must be day, hour, task, session, or type")
    cap = _positive_int(max_items_per_bucket, "max_items_per_bucket", maximum=500)
    normalized = query_action_history(actions, limit=10_000, sort="asc")["items"]
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in normalized:
        timestamp = _parse_action_time(item["created_at"])
        if bucket_kind == "day":
            key = timestamp.date().isoformat() if timestamp else "unknown-time"
        elif bucket_kind == "hour":
            key = timestamp.strftime("%Y-%m-%dT%H:00Z") if timestamp else "unknown-time"
        elif bucket_kind == "task":
            key = f"task:{item['task_id']}" if item["task_id"] else "task:unassigned"
        elif bucket_kind == "session":
            key = f"session:{item['session_id']}" if item["session_id"] else "session:unassigned"
        else:
            key = item["action_type"] or "unknown-type"
        groups[key].append(item)
    buckets: list[dict[str, Any]] = []
    for key in sorted(groups):
        rows = groups[key]
        buckets.append(
            {
                "key": key,
                "count": len(rows),
                "first_at": rows[0]["created_at"] if rows else None,
                "last_at": rows[-1]["created_at"] if rows else None,
                "action_types": dict(sorted(Counter(row["action_type"] for row in rows).items())),
                "items": rows[:cap],
                "truncated": len(rows) > cap,
            }
        )
    return {"kind": "action_timeline", "bucket": bucket_kind, "total": len(normalized), "buckets": buckets}


def build_action_chains(
    actions: Iterable[Mapping[str, Any]],
    *,
    max_gap_seconds: int = 30 * 60,
) -> dict[str, Any]:
    """Infer task/session/target action chains with explicit gap boundaries."""
    gap = _positive_int(max_gap_seconds, "max_gap_seconds", maximum=7 * 24 * 60 * 60)
    normalized = query_action_history(actions, limit=10_000, sort="asc")["items"]
    by_owner: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in normalized:
        if item["task_id"]:
            owner = f"task:{item['task_id']}"
        elif item["session_id"]:
            owner = f"session:{item['session_id']}"
        elif item["target"]:
            owner = f"target:{item['target']}"
        else:
            owner = "unlinked"
        by_owner[owner].append(item)
    chains: list[dict[str, Any]] = []
    for owner, rows in by_owner.items():
        current: list[dict[str, Any]] = []
        last_time: datetime | None = None
        for item in rows:
            item_time = _parse_action_time(item["created_at"])
            split = bool(current and item_time and last_time and (item_time - last_time).total_seconds() > gap)
            if split:
                chains.append(_chain_projection(owner, current, gap))
                current = []
            current.append(item)
            if item_time is not None:
                last_time = item_time
        if current:
            chains.append(_chain_projection(owner, current, gap))
    chains.sort(key=lambda chain: (chain["started_at"] or "", chain["chain_id"]))
    return {"kind": "action_chains", "max_gap_seconds": gap, "chains": chains, "total": len(chains)}


def _chain_projection(owner: str, rows: list[dict[str, Any]], gap: int) -> dict[str, Any]:
    start = _parse_action_time(rows[0]["created_at"])
    end = _parse_action_time(rows[-1]["created_at"])
    duration = max(0, round((end - start).total_seconds())) if start and end else None
    identity = checksum_payload({"owner": owner, "ids": [row.get("action_id") for row in rows], "start": rows[0]["created_at"]})[:16]
    return {
        "chain_id": f"chain-{identity}",
        "owner": owner,
        "started_at": rows[0]["created_at"],
        "ended_at": rows[-1]["created_at"],
        "duration_seconds": duration,
        "max_gap_seconds": gap,
        "count": len(rows),
        "action_types": dict(sorted(Counter(row["action_type"] for row in rows).items())),
        "actions": rows,
    }


def summarize_action_history(actions: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Summarize action history without inventing or persisting new records."""
    rows = query_action_history(actions, limit=10_000, sort="asc")["items"]
    timestamps = [_parse_action_time(row["created_at"]) for row in rows]
    valid_times = [value for value in timestamps if value is not None]
    target_counts = Counter(row["target"] for row in rows if row["target"])
    return {
        "kind": "action_history_summary",
        "total": len(rows),
        "time_range": {
            "first": min(valid_times).isoformat().replace("+00:00", "Z") if valid_times else None,
            "last": max(valid_times).isoformat().replace("+00:00", "Z") if valid_times else None,
        },
        "action_types": dict(sorted(Counter(row["action_type"] for row in rows if row["action_type"]).items())),
        "tools": dict(sorted(Counter(row["tool_name"] for row in rows if row["tool_name"]).items())),
        "tags": dict(sorted(Counter(tag for row in rows for tag in row["tags"]).items())),
        "tasks": len({row["task_id"] for row in rows if row["task_id"]}),
        "sessions": len({row["session_id"] for row in rows if row["session_id"]}),
        "top_targets": [{"target": name, "count": count} for name, count in target_counts.most_common(10)],
    }


def project_action_history(
    actions: Iterable[Mapping[str, Any]],
    *,
    query: str = "",
    filters: Mapping[str, Any] | None = None,
    timeline_bucket: str = "day",
    chain_gap_seconds: int = 30 * 60,
) -> dict[str, Any]:
    """Return a query page plus timeline, inferred chains, and a summary."""
    options = dict(filters or {})
    options["query"] = query
    result = query_action_history(actions, **options)
    # Use the query result rather than the raw rows so all projections describe
    # the exact same filter domain.
    selected = result["items"]
    return {
        "kind": "action_history_projection",
        "query": result,
        "timeline": build_action_timeline(selected, bucket=timeline_bucket),
        "chains": build_action_chains(selected, max_gap_seconds=chain_gap_seconds),
        "summary": summarize_action_history(selected),
    }


# ── Export planning, manifests, checksums, and verification ───────────────


_EXPORT_DATA_CATEGORIES = {
    "config",
    "memories",
    "conversations",
    "actions",
    "people",
    "goals",
    "commitments",
}
_EXPORT_CATEGORY_ALIASES = {
    "memory": "memories",
    "conversation": "conversations",
    "action": "actions",
    "person": "people",
    "goal": "goals",
    "commitment": "commitments",
}
_EXPORT_METADATA_KEYS = {
    "version",
    "exported_at",
    "export_profile",
    "secrets_redacted",
    "redaction_preview",
}
_EXPORT_CONTROLLED_KEYS = _EXPORT_DATA_CATEGORIES | {"conversation_messages"}
_DATE_FIELDS = (
    "created_at",
    "timestamp",
    "sent_at",
    "occurred_at",
    "updated_at",
    "completed_at",
    "last_activity_at",
    "due_at",
    "date",
)


def _normalise_export_categories(value: str | Sequence[str] | None, field: str) -> set[str]:
    """Validate a top-level export category selection without guessing names."""
    categories = {_EXPORT_CATEGORY_ALIASES.get(item, item) for item in _as_set(value, field)}
    unknown = sorted(categories - _EXPORT_DATA_CATEGORIES)
    if unknown:
        raise UpgradeValidationError(
            f"{field} contains unsupported categories: {', '.join(unknown)}; "
            f"use: {', '.join(sorted(_EXPORT_DATA_CATEGORIES))}"
        )
    return categories


def _export_date_bounds(since: str | None, until: str | None) -> tuple[datetime | None, datetime | None]:
    start = _parse_action_time(since)
    end = _parse_action_time(until)
    if since and start is None:
        raise UpgradeValidationError("export since must be an ISO-8601 timestamp")
    if until and end is None:
        raise UpgradeValidationError("export until must be an ISO-8601 timestamp")
    if start is not None and end is not None and start > end:
        raise UpgradeValidationError("export since must not be later than until")
    return start, end


def _record_export_time(record: Mapping[str, Any]) -> datetime | None:
    """Find the best direct timestamp for a record without interpreting content."""
    for key in _DATE_FIELDS:
        timestamp = _parse_action_time(record.get(key))
        if timestamp is not None:
            return timestamp
    return None


def filter_export_payload(
    payload: Mapping[str, Any],
    *,
    include_categories: str | Sequence[str] | None = None,
    exclude_categories: str | Sequence[str] | None = None,
    since: str | None = None,
    until: str | None = None,
) -> dict[str, Any]:
    """Safely select export sections and date-bounded records.

    The function is intentionally side-effect free.  A date boundary applies
    only to record lists.  Undated rows are omitted under a date-bounded
    export rather than silently escaping the caller's requested time window;
    the result reports those omissions.  A configuration section has no
    record timestamp, so it is kept only when no date boundary is supplied.
    """
    if not isinstance(payload, Mapping):
        raise UpgradeValidationError("export payload must be a mapping")
    include = _normalise_export_categories(include_categories, "include_categories")
    exclude = _normalise_export_categories(exclude_categories, "exclude_categories")
    overlap = sorted(include & exclude)
    if overlap:
        raise UpgradeValidationError(
            "a category cannot be both included and excluded: " + ", ".join(overlap)
        )
    selected = include if include else set(_EXPORT_DATA_CATEGORIES)
    selected -= exclude
    start, end = _export_date_bounds(since, until)
    bounded = start is not None or end is not None

    # Copy metadata first, including unknown future metadata keys, but never
    # retain an unselected data category just because a future exporter added
    # it.  JSON round-tripping detaches nested objects from store snapshots.
    result: dict[str, Any] = {
        str(key): json.loads(_json_bytes(value).decode("utf-8"))
        for key, value in payload.items()
        if str(key) in _EXPORT_METADATA_KEYS or str(key) not in _EXPORT_CONTROLLED_KEYS
    }
    section_stats: dict[str, dict[str, int]] = {}
    warnings: list[str] = []
    for category in sorted(selected):
        if category not in payload:
            continue
        if category == "conversations":
            # Conversation messages are coupled to their conversation
            # metadata for import compatibility.  They are selected together.
            related_keys = ("conversations", "conversation_messages")
        else:
            related_keys = (category,)
        for key in related_keys:
            if key not in payload:
                continue
            source = payload[key]
            if not isinstance(source, list):
                if bounded:
                    warnings.append(f"{key} was omitted because it cannot be date-filtered")
                    section_stats[str(key)] = {"source": 1, "included": 0, "undated": 1}
                else:
                    result[str(key)] = json.loads(_json_bytes(source).decode("utf-8"))
                    section_stats[str(key)] = {"source": 1, "included": 1, "undated": 0}
                continue
            filtered: list[Any] = []
            undated = 0
            for record in source:
                if not bounded:
                    filtered.append(record)
                    continue
                if not isinstance(record, Mapping):
                    undated += 1
                    continue
                timestamp = _record_export_time(record)
                if timestamp is None:
                    undated += 1
                    continue
                if start is not None and timestamp < start:
                    continue
                if end is not None and timestamp > end:
                    continue
                filtered.append(record)
            result[str(key)] = json.loads(_json_bytes(filtered).decode("utf-8"))
            section_stats[str(key)] = {
                "source": len(source),
                "included": len(filtered),
                "undated": undated,
            }
            if undated:
                warnings.append(f"{key}: omitted {undated} undated record(s) from the bounded export")
    filters = {
        "include_categories": sorted(include),
        "exclude_categories": sorted(exclude),
        "effective_categories": sorted(selected),
        "since": start.isoformat().replace("+00:00", "Z") if start else None,
        "until": end.isoformat().replace("+00:00", "Z") if end else None,
    }
    result["export_filter"] = filters
    return {
        "payload": result,
        "filters": filters,
        "section_stats": section_stats,
        "warnings": warnings,
    }


def _redact_export_value(value: Any, *, path: str = "") -> tuple[Any, list[dict[str, str]]]:
    redactions: list[dict[str, str]] = []
    if isinstance(value, Mapping):
        cleaned: dict[str, Any] = {}
        for raw_key, child in value.items():
            key = str(raw_key)
            child_path = f"{path}.{key}" if path else key
            if _SECRET_KEY.search(key):
                cleaned[key] = None
                redactions.append({"path": child_path, "reason": "sensitive_key"})
                continue
            cleaned_child, child_redactions = _redact_export_value(child, path=child_path)
            cleaned[key] = cleaned_child
            redactions.extend(child_redactions)
        return cleaned, redactions
    if isinstance(value, list):
        cleaned_items: list[Any] = []
        for index, child in enumerate(value):
            cleaned_child, child_redactions = _redact_export_value(child, path=f"{path}[{index}]")
            cleaned_items.append(cleaned_child)
            redactions.extend(child_redactions)
        return cleaned_items, redactions
    if isinstance(value, str) and _SECRET_VALUE.match(value.strip()):
        redactions.append({"path": path or "$", "reason": "secret_like_value"})
        return None, redactions
    return value, redactions


def redact_export_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Return a redacted deep projection and a path/reason redaction manifest."""
    if not isinstance(payload, Mapping):
        raise UpgradeValidationError("export payload must be a mapping")
    cleaned, redactions = _redact_export_value(payload)
    assert isinstance(cleaned, dict)
    return {
        "payload": cleaned,
        "redactions": sorted(redactions, key=lambda item: (item["path"], item["reason"])),
    }


def _section_counts(payload: Mapping[str, Any]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for key, value in payload.items():
        if isinstance(value, (list, tuple, set, Mapping)):
            counts[str(key)] = len(value)
        elif value is None:
            counts[str(key)] = 0
        else:
            counts[str(key)] = 1
    return dict(sorted(counts.items()))


def _section_checksums(payload: Mapping[str, Any]) -> dict[str, str]:
    return {str(key): checksum_payload(value) for key, value in sorted(payload.items(), key=lambda item: str(item[0]))}


def build_export_manifest(
    payload: Mapping[str, Any],
    *,
    profile: str = "full",
    redactions: Sequence[Mapping[str, str]] | None = None,
    previous_manifest: Mapping[str, Any] | None = None,
    incremental: bool = False,
) -> dict[str, Any]:
    """Build a deterministic manifest for a planned or completed export."""
    if not isinstance(payload, Mapping):
        raise UpgradeValidationError("export payload must be a mapping")
    section_checksums = _section_checksums(payload)
    previous = previous_manifest or {}
    prior_sections = previous.get("section_checksums") if isinstance(previous, Mapping) else None
    if not isinstance(prior_sections, Mapping):
        prior_sections = {}
    changed = sorted(key for key, checksum in section_checksums.items() if prior_sections.get(key) != checksum)
    unchanged = sorted(key for key, checksum in section_checksums.items() if prior_sections.get(key) == checksum)
    omitted = sorted(str(key) for key in prior_sections if key not in section_checksums)
    normalized_redactions = [
        {"path": str(item.get("path") or ""), "reason": str(item.get("reason") or "sensitive_key")}
        for item in (redactions or [])
        if isinstance(item, Mapping)
    ]
    return {
        "kind": "ares_export_manifest",
        "schema_version": 1,
        "profile": _normalise_text(profile or "full", "profile", maximum=80, required=True).casefold(),
        "checksum_sha256": checksum_payload(payload),
        "section_checksums": section_checksums,
        "section_counts": _section_counts(payload),
        "redactions": sorted(normalized_redactions, key=lambda item: (item["path"], item["reason"])),
        "incremental": {
            "enabled": bool(incremental),
            "base_checksum_sha256": previous.get("checksum_sha256") if isinstance(previous, Mapping) else None,
            "changed_sections": changed if incremental else sorted(section_checksums),
            "unchanged_sections": unchanged if incremental else [],
            "removed_sections": omitted if incremental else [],
        },
    }


def plan_export(
    payload: Mapping[str, Any],
    *,
    profile: str = "full",
    redact: bool = True,
    previous_manifest: Mapping[str, Any] | None = None,
    incremental: bool = False,
    output_path: str | Path | None = None,
) -> dict[str, Any]:
    """Prepare a safe export payload, manifest, and optional incremental delta.

    ``payload`` is never modified.  For an incremental export the complete
    redacted payload remains available as ``full_payload`` while
    ``write_payload`` contains only changed sections and a base checksum.
    """
    if not isinstance(payload, Mapping):
        raise UpgradeValidationError("export payload must be a mapping")
    if redact:
        redacted = redact_export_payload(payload)
        clean_payload = redacted["payload"]
        redactions = redacted["redactions"]
    else:
        # Round-trip through deterministic JSON to prove the unredacted plan is
        # serializable and to detach it from the caller's nested structures.
        clean_payload = json.loads(_json_bytes(payload).decode("utf-8"))
        redactions = []
    manifest = build_export_manifest(
        clean_payload,
        profile=profile,
        redactions=redactions,
        previous_manifest=previous_manifest,
        incremental=incremental,
    )
    changed_sections = manifest["incremental"]["changed_sections"]
    if incremental:
        write_payload: dict[str, Any] = {
            "incremental": True,
            "base_checksum_sha256": manifest["incremental"]["base_checksum_sha256"],
            "sections": {key: clean_payload[key] for key in changed_sections},
        }
    else:
        write_payload = clean_payload
    return {
        "kind": "export_plan",
        "schema_version": 1,
        "profile": manifest["profile"],
        "output_path": str(Path(output_path).expanduser()) if output_path is not None else None,
        "full_payload": clean_payload,
        "write_payload": write_payload,
        "manifest": manifest,
        "redaction_enabled": bool(redact),
        "warnings": (
            ["incremental export has no compatible base manifest"]
            if incremental and not manifest["incremental"]["base_checksum_sha256"]
            else []
        ),
    }


def _path_value(value: Any, path: str) -> tuple[bool, Any]:
    """Resolve a manifest-style ``a.b[0]`` path without evaluating anything.

    A legacy export also contains ``redaction_preview`` records whose literal
    dictionary keys are themselves dotted paths.  At each mapping level we
    therefore first honour the entire remaining string as an exact key before
    treating it as a traversal expression.
    """
    remaining = str(path or "")
    if not remaining:
        return False, None
    current = value
    while remaining:
        if isinstance(current, Mapping) and remaining in current:
            return True, current[remaining]
        if remaining.startswith("["):
            match = re.match(r"^\[(\d+)\](.*)$", remaining)
            if match is None or not isinstance(current, list):
                return False, None
            index = int(match.group(1))
            if index >= len(current):
                return False, None
            current = current[index]
            remaining = match.group(2)
            if remaining.startswith("."):
                remaining = remaining[1:]
            continue
        match = re.match(r"^([^.[\]]+)(.*)$", remaining)
        if match is None or not isinstance(current, Mapping):
            return False, None
        key, remaining = match.groups()
        if key not in current:
            return False, None
        current = current[key]
        if remaining.startswith("."):
            remaining = remaining[1:]
    return True, current


def verify_export_manifest(payload: Mapping[str, Any], manifest: Mapping[str, Any]) -> dict[str, Any]:
    """Verify checksums, section inventory, and declared redactions."""
    if not isinstance(payload, Mapping):
        raise UpgradeValidationError("export payload must be a mapping")
    if not isinstance(manifest, Mapping):
        raise UpgradeValidationError("manifest must be a mapping")
    errors: list[str] = []
    checks: dict[str, bool] = {}
    expected_checksum = manifest.get("checksum_sha256")
    checks["checksum_sha256"] = isinstance(expected_checksum, str) and checksum_payload(payload) == expected_checksum
    if not checks["checksum_sha256"]:
        errors.append("payload checksum does not match manifest")
    expected_sections = manifest.get("section_checksums")
    actual_sections = _section_checksums(payload)
    checks["section_checksums"] = isinstance(expected_sections, Mapping) and dict(expected_sections) == actual_sections
    if not checks["section_checksums"]:
        errors.append("section checksums do not match manifest")
    expected_counts = manifest.get("section_counts")
    actual_counts = _section_counts(payload)
    checks["section_counts"] = isinstance(expected_counts, Mapping) and dict(expected_counts) == actual_counts
    if not checks["section_counts"]:
        errors.append("section counts do not match manifest")
    redaction_errors: list[str] = []
    redactions = manifest.get("redactions")
    if not isinstance(redactions, list):
        redactions = []
    for item in redactions:
        if not isinstance(item, Mapping):
            redaction_errors.append("manifest contains a malformed redaction record")
            continue
        path = str(item.get("path") or "")
        found, value = _path_value(payload, path)
        if not found:
            redaction_errors.append(f"redacted path is missing: {path}")
        elif value is not None:
            redaction_errors.append(f"redacted value is present at: {path}")
    checks["redactions"] = not redaction_errors
    errors.extend(redaction_errors)
    return {
        "ok": not errors,
        "checks": checks,
        "errors": errors,
        "actual_checksum_sha256": checksum_payload(payload),
        "actual_section_checksums": actual_sections,
    }


def verify_export_file(path: str | Path, manifest: Mapping[str, Any] | str | Path) -> dict[str, Any]:
    """Load a JSON export and verify it against a supplied manifest or file."""
    export_path = Path(path).expanduser()
    if not export_path.is_file():
        raise UpgradeValidationError(f"export file does not exist: {export_path}")
    try:
        payload = json.loads(export_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise UpgradeValidationError(f"could not read JSON export: {exc}") from exc
    if not isinstance(payload, Mapping):
        raise UpgradeValidationError("export file must contain a JSON object")
    if isinstance(manifest, (str, Path)):
        manifest_path = Path(manifest).expanduser()
        try:
            loaded_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise UpgradeValidationError(f"could not read manifest: {exc}") from exc
        manifest = loaded_manifest
    result = verify_export_manifest(payload, manifest)
    result["path"] = str(export_path)
    return result
