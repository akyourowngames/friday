from __future__ import annotations


def file_understanding(file_record: dict, content: str | None = None) -> dict:
    metadata = file_record.get("metadata") if isinstance(file_record.get("metadata"), dict) else {}
    profile = metadata.get("content_profile") if isinstance(metadata.get("content_profile"), dict) else {}
    content_text = str(content or "")
    result = {
        "content_profile": profile,
        "content_chars_available": len(content_text),
        "language": str(metadata.get("language") or ""),
        "document_kind": str(metadata.get("document_kind") or ""),
        "media_kind": str(metadata.get("media_kind") or ""),
        "format": str(metadata.get("format") or ""),
        "extractor": str(metadata.get("extractor") or ""),
        "extractor_status": str(metadata.get("extractor_status") or ""),
        "structure": {},
        "symbols": {},
    }

    structure = result["structure"]
    for key in ("headings", "top_level_keys", "nested_key_paths"):
        value = profile.get(key) if key == "headings" else metadata.get(key)
        if value:
            structure[key] = value

    symbols = result["symbols"]
    for key in ("imports", "classes", "functions", "class_details", "function_details"):
        value = metadata.get(key)
        if value:
            symbols[key] = value
    for key in ("import_count", "class_count", "function_count"):
        if key in metadata:
            symbols[key] = metadata[key]

    return result
