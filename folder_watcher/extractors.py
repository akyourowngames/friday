from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from .configuration import WatcherConfig


def extract_document_or_media(path: Path, mime_type: str, config: WatcherConfig) -> tuple[dict, str]:
    suffix = path.suffix.lower()
    if mime_type == "application/pdf" or suffix == ".pdf":
        return _pdf(path, config)
    if suffix == ".docx":
        return _docx(path, config)
    if mime_type.startswith("image/"):
        return _image(path, config)
    if mime_type.startswith("audio/"):
        return _audio(path, config)
    if mime_type.startswith("video/"):
        return _video(path)
    return {}, ""


def _pdf(path: Path, config: WatcherConfig) -> tuple[dict, str]:
    try:
        from pypdf import PdfReader
    except ImportError:
        return {"extractor": "pypdf", "extractor_status": "missing_dependency"}, ""
    try:
        reader = PdfReader(str(path))
        info = reader.metadata or {}
        metadata = {
            "document_kind": "pdf",
            "page_count": len(reader.pages),
            "title": str(info.get("/Title", "") or ""),
            "author": str(info.get("/Author", "") or ""),
            "creator": str(info.get("/Creator", "") or ""),
            "producer": str(info.get("/Producer", "") or ""),
            "extractor": "pypdf",
            "extractor_status": "ok",
        }
        chunks = []
        for page in reader.pages:
            if sum(len(item) for item in chunks) >= config.max_content_chars:
                break
            try:
                chunks.append(page.extract_text() or "")
            except Exception:
                chunks.append("")
        return metadata, "\n".join(chunks)[: config.max_content_chars]
    except Exception as exc:
        return {"document_kind": "pdf", "extractor": "pypdf", "extractor_status": "failed", "extractor_error": str(exc)}, ""


def _docx(path: Path, config: WatcherConfig) -> tuple[dict, str]:
    try:
        from docx import Document
    except ImportError:
        return {"extractor": "python-docx", "extractor_status": "missing_dependency"}, ""
    try:
        document = Document(str(path))
        props = document.core_properties
        paragraphs = [item.text for item in document.paragraphs if item.text]
        content = "\n".join(paragraphs)[: config.max_content_chars]
        return {
            "document_kind": "docx",
            "paragraph_count": len(paragraphs),
            "title": props.title or "",
            "author": props.author or "",
            "subject": props.subject or "",
            "extractor": "python-docx",
            "extractor_status": "ok",
        }, content
    except Exception as exc:
        return {"document_kind": "docx", "extractor": "python-docx", "extractor_status": "failed", "extractor_error": str(exc)}, ""


def _image(path: Path, config: WatcherConfig) -> tuple[dict, str]:
    try:
        from PIL import Image, ExifTags
    except ImportError:
        return {"media_kind": "image", "extractor": "Pillow", "extractor_status": "missing_dependency"}, ""
    try:
        with Image.open(path) as image:
            exif = {}
            raw = image.getexif()
            for key, value in raw.items():
                label = ExifTags.TAGS.get(key, str(key))
                exif[str(label)] = _jsonable(value)
            metadata = {
                "media_kind": "image",
                "width": image.width,
                "height": image.height,
                "mode": image.mode,
                "format": image.format or "",
                "exif": exif,
                "extractor": "Pillow",
                "extractor_status": "ok",
            }
            ocr_text = _ocr(path, config)
            if ocr_text:
                metadata["ocr_status"] = "ok"
            elif config.ocr_enabled:
                metadata["ocr_status"] = "unavailable_or_empty"
            return metadata, ocr_text[: config.max_content_chars]
    except Exception as exc:
        return {"media_kind": "image", "extractor": "Pillow", "extractor_status": "failed", "extractor_error": str(exc)}, ""


def _ocr(path: Path, config: WatcherConfig) -> str:
    if not config.ocr_enabled:
        return ""
    try:
        import pytesseract
        from PIL import Image
    except ImportError:
        return ""
    try:
        with Image.open(path) as image:
            return pytesseract.image_to_string(image) or ""
    except Exception:
        return ""


def _audio(path: Path, config: WatcherConfig) -> tuple[dict, str]:
    try:
        from mutagen import File as MutagenFile
    except ImportError:
        return {"media_kind": "audio", "extractor": "mutagen", "extractor_status": "missing_dependency"}, ""
    metadata: dict[str, Any] = {"media_kind": "audio", "extractor": "mutagen"}
    try:
        audio = MutagenFile(str(path), easy=False)
        if audio is None:
            metadata["extractor_status"] = "unsupported"
        else:
            info = getattr(audio, "info", None)
            metadata["extractor_status"] = "ok"
            metadata["duration_seconds"] = float(getattr(info, "length", 0) or 0)
            metadata["bitrate"] = int(getattr(info, "bitrate", 0) or 0)
            metadata["sample_rate"] = int(getattr(info, "sample_rate", 0) or 0)
            metadata["channels"] = int(getattr(info, "channels", 0) or 0)
            metadata["tags"] = _audio_tags(getattr(audio, "tags", None))
    except Exception as exc:
        metadata["extractor_status"] = "failed"
        metadata["extractor_error"] = str(exc)
    transcript = _transcript(path, config)
    if transcript:
        metadata["transcription_status"] = "ok"
    elif config.transcription_enabled:
        metadata["transcription_status"] = "unavailable_or_empty"
    return metadata, transcript[: config.max_content_chars]


def _audio_tags(tags: Any) -> dict:
    result = {}
    if not tags:
        return result
    for key in tags.keys():
        value = tags.get(key)
        result[str(key)] = _jsonable(value)
    return result


def _transcript(path: Path, config: WatcherConfig) -> str:
    if not config.transcription_enabled:
        return ""
    for suffix in (".txt", ".transcript.txt", ".vtt", ".srt"):
        sidecar = path.with_suffix(path.suffix + suffix) if suffix.startswith(".transcript") else path.with_suffix(suffix)
        if sidecar.exists() and sidecar.is_file():
            try:
                return sidecar.read_text(encoding="utf-8", errors="replace")
            except OSError:
                return ""
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        return ""
    try:
        model = WhisperModel("base", device="cpu", compute_type="int8")
        segments, _ = model.transcribe(str(path))
        return "\n".join(segment.text for segment in segments)
    except Exception:
        return ""


def _video(path: Path) -> tuple[dict, str]:
    metadata: dict[str, Any] = {"media_kind": "video"}
    try:
        import cv2
    except ImportError:
        return {"media_kind": "video", "extractor": "opencv", "extractor_status": "missing_dependency"}, ""
    capture = None
    try:
        capture = cv2.VideoCapture(str(path))
        if not capture.isOpened():
            metadata["extractor"] = "opencv"
            metadata["extractor_status"] = "unsupported"
            return metadata, ""
        fps = float(capture.get(cv2.CAP_PROP_FPS) or 0)
        frame_count = float(capture.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        width = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
        height = int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
        metadata.update(
            {
                "extractor": "opencv",
                "extractor_status": "ok",
                "fps": fps,
                "frame_count": frame_count,
                "width": width,
                "height": height,
                "duration_seconds": (frame_count / fps) if fps else 0,
                "ffprobe": _ffprobe(path),
            }
        )
        return metadata, ""
    except Exception as exc:
        metadata["extractor"] = "opencv"
        metadata["extractor_status"] = "failed"
        metadata["extractor_error"] = str(exc)
        return metadata, ""
    finally:
        if capture is not None:
            capture.release()


def _ffprobe(path: Path) -> dict:
    try:
        completed = subprocess.run(
            [
                "ffprobe",
                "-v",
                "quiet",
                "-print_format",
                "json",
                "-show_format",
                "-show_streams",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return {"status": "unavailable"}
    if completed.returncode != 0:
        return {"status": "failed"}
    try:
        parsed = json.loads(completed.stdout or "{}")
    except json.JSONDecodeError:
        return {"status": "decode_failed"}
    return {"status": "ok", "data": parsed}


def _jsonable(value: Any) -> Any:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, bytes):
        return value.hex()
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    return str(value)
