---
name: image-batch-processor
description: Batch process images in a folder — resize to common dimensions, convert between formats (PNG/JPEG/WebP), rename patterns. Use for "batch resize these images", "convert all PNGs to WebP", "optimize images in this folder".
category: utilities
version: 1.0.0
---

# Image Batch Processor

## Procedure

1. **Scan** — Use `glob_pattern` or `list_directory` to find all image files in the target folder (`.png`, `.jpg`, `.jpeg`, `.webp`, `.bmp`, `.gif`). Show the user what was found.

2. **Confirm scope** — If the user didn't specify parameters, ask:
   - Target format (PNG, JPEG, WebP, BMP, GIF)
   - Dimensions (max width or height, keep aspect ratio)
   - Output folder (default: same folder, or `processed/` subfolder)
   - Whether to overwrite originals or create copies

3. **Process each image sequentially**:
   - Call `image_info` on each file first to check dimensions/format
   - Call `convert_image` and/or `resize_image` as specified
   - If converting + resizing, do both in sequence per file

4. **Report** — Summarize what was done:
   - Files processed (count)
   - Before/after sizes or dimensions
   - Output location
   - Any files that failed and why

## Rules
- Process images one at a time, not in parallel.
- Use `image_info` before any operation to validate the file.
- For JPEG output with RGBA source, the tool handles transparency automatically.
- If no output format is specified, keep the original format and just resize.
