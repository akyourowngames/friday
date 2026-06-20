# Power Tools for Ares — Design Spec

**Date:** 2026-06-20
**Status:** Draft
**Author:** Claude (brainstorming session)

---

## Overview

Add seven new power tools to Ares that transform it from a chat assistant into a full execution environment. Inspired by Hermes Agent's architecture: unrestricted code execution, shell command running, and image generation with editing. All tools use free APIs (no paid subscriptions) and local execution (no cloud sandboxes).

## Current State

Ares has 22 tools across 4 categories:
- **Memory:** store, search, update, delete
- **Tasks:** create, list, search, complete, cancel, get_due_soon, get_execution_status
- **Files:** read, search, list_directory, get_file_info, glob_pattern, disk_usage, checksum, copy, find_duplicates, tail, head, count_lines, file_tree, write, edit, create_directory, delete, move
- **Web:** web_search, fetch_url

## Design Goals

1. **Unrestricted execution:** Run any Python code or shell command — no sandboxing, no allowlists
2. **Image generation:** Text-to-image via Pollinations.ai (free, no API key)
3. **Image editing:** Basic editing via Pillow — info, resize, convert, crop
4. **Real-time output:** Stream command output as it happens
5. **Timeout protection:** Kill runaway commands after configurable timeout
6. **Safe defaults:** Timeout + output caps prevent infinite loops and memory exhaustion

---

## New Tools

### 1. `run_code` — Execute Python Code

Run arbitrary Python code in an isolated subprocess. Full access to standard library, pip packages, filesystem, network. Output (stdout + stderr) returned to Ares.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `code` | string | required | Python code to execute |
| `timeout` | integer | `30` | Max seconds before kill (1–300) |
| `cwd` | string | `None` | Working directory (defaults to ~/ares_images or home) |

**Behavior:**
1. Write code to a temp file (avoids shell injection via command-line args)
2. Run via `subprocess.Popen` with:
   - `sys.executable` (current Python interpreter)
   - `-u` flag (unbuffered output)
   - `stdout=PIPE, stderr=PIPE`
   - `text=True`
   - `bufsize=1` (line-buffered)
3. Capture stdout + stderr line by line
4. Kill on timeout: `SIGTERM` → 2s grace → `SIGKILL` (Unix) / `taskkill /F /T` (Windows)
5. Return structured result:
   ```
   Exit code: 0
   --- stdout ---
   [output lines]
   --- stderr ---
   [error lines]
   ```

**Implementation pattern** (from Hermes Agent's `code_execution_tool` + Python subprocess docs):

```python
import subprocess
import sys
import tempfile
import os

def run_code(code: str, timeout: int = 30, cwd: str | None = None) -> str:
    """Execute Python code in an isolated subprocess."""
    timeout = max(1, min(timeout, 300))

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".py", delete=False, encoding="utf-8"
    ) as f:
        f.write(code)
        f.flush()
        temp_path = f.name

    try:
        result = subprocess.run(
            [sys.executable, "-u", temp_path],
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=cwd,
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        )

        output_parts = []
        if result.stdout:
            output_parts.append(f"--- stdout ---\n{result.stdout.rstrip()}")
        if result.stderr:
            output_parts.append(f"--- stderr ---\n{result.stderr.rstrip()}")

        if not output_parts:
            return f"Exit code: {result.returncode}\n(No output)"

        return f"Exit code: {result.returncode}\n" + "\n".join(output_parts)

    except subprocess.TimeoutExpired:
        return f"Error: Code execution timed out after {timeout}s"
    except Exception as e:
        return f"Error: {e}"
    finally:
        try:
            os.unlink(temp_path)
        except OSError:
            pass
```

**Key design decisions:**
- **`subprocess.run()` over `asyncio.create_subprocess_exec()`:** The tool handler is synchronous (ToolExecutor.execute is sync). `subprocess.run()` has built-in `timeout=` and `capture_output=`. Streaming isn't needed for code execution — we want the full output at once for the LLM to process.
- **Temp file, not `-c` flag:** Avoids shell escaping issues with quotes, backslashes, and multi-line code.
- **`-u` flag:** Unbuffered output ensures print() statements appear immediately.
- **`PYTHONIOENCODING=utf-8`:** Prevents encoding errors on Windows.

---

### 2. `run_command` — Execute Shell Commands

Run arbitrary shell commands (bash, git, npm, python, docker, etc.). Full access to the system.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `command` | string | required | Shell command to execute |
| `timeout` | integer | `30` | Max seconds before kill (1–300) |
| `cwd` | string | `None` | Working directory |

**Behavior:**
1. Run via `subprocess.Popen` with `shell=True` (needed for pipes, redirects, && chains)
2. Capture stdout + stderr via `PIPE`
3. Read line-by-line in a thread (non-blocking for the LLM)
4. Kill entire process tree on timeout (process group)
5. Return structured result

**Implementation pattern** (from Hermes Agent's `terminal_tool` + subprocess best practices):

```python
import subprocess
import os
import signal
import sys

def run_command(command: str, timeout: int = 30, cwd: str | None = None) -> str:
    """Execute a shell command with output capture."""
    timeout = max(1, min(timeout, 300))

    # Use process group for clean timeout kills
    kwargs = {
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "text": True,
        "cwd": cwd,
        "bufsize": 1,
    }

    # On Unix, create new session for process group kills
    if sys.platform != "win32":
        kwargs["start_new_session"] = True

    try:
        proc = subprocess.Popen(command, shell=True, **kwargs)

        try:
            stdout, stderr = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            # Kill entire process group on Unix
            if sys.platform != "win32":
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                    proc.wait(timeout=2)
                except (ProcessLookupError, subprocess.TimeoutExpired):
                    try:
                        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                    except ProcessLookupError:
                        pass
            else:
                proc.kill()

            stdout, stderr = proc.communicate()
            return (
                f"Error: Command timed out after {timeout}s\n"
                f"{stdout.rstrip() if stdout else ''}"
            )

        output_parts = []
        if stdout and stdout.strip():
            output_parts.append(f"--- stdout ---\n{stdout.rstrip()}")
        if stderr and stderr.strip():
            output_parts.append(f"--- stderr ---\n{stderr.rstrip()}")

        if not output_parts:
            return f"Exit code: {proc.returncode}\n(No output)"

        result = f"Exit code: {proc.returncode}\n" + "\n".join(output_parts)

        # Cap output at 50KB to prevent context overflow (from Hermes patterns)
        max_chars = 50_000
        if len(result) > max_chars:
            result = result[:max_chars] + f"\n... (output truncated at {max_chars} chars)"

        return result

    except Exception as e:
        return f"Error running command: {e}"
```

**Key design decisions:**
- **`shell=True`:** Required for pipes (`|`), redirects (`>`), command chaining (`&&`), and environment variable expansion. Without it, each word becomes a separate argument.
- **Process group kills:** Hermes uses `start_new_session=True` + `os.killpg()` to kill the entire process tree on timeout. Without this, child processes become orphans.
- **Output cap at 50KB:** Hermes caps stdout at 50KB, stderr at 10KB. We use 50KB total to prevent context window overflow.
- **Sync execution:** Same reasoning as `run_code` — synchronous handler is simpler and `subprocess.run()` has built-in timeout.

---

### 3. `generate_image` — Text-to-Image Generation

Generate images from text prompts using Pollinations.ai (free, no API key).

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `prompt` | string | required | Text description of the image |
| `width` | integer | `1024` | Output width in pixels |
| `height` | integer | `1024` | Output height in pixels |
| `model` | string | `"flux"` | Model: flux, turbo, stable-diffusion |
| `seed` | integer | `None` | Deterministic seed (None = random) |

**Behavior:**
1. URL-encode the prompt
2. Build Pollinations API URL: `https://image.pollinations.ai/prompt/{encoded_prompt}`
3. Add query params: `model`, `width`, `height`, `seed`
4. HTTP GET with timeout (120s — image gen can be slow)
5. Validate Content-Type is image
6. Save to `~/ares_images/{timestamp}_{hash}.jpg`
7. Return file path + dimensions

**Implementation pattern** (from Pollinations.ai API docs):

```python
import httpx
import hashlib
import os
from pathlib import Path
from urllib.parse import quote

IMAGES_DIR = Path("~/.ares/images").expanduser()

def generate_image(
    prompt: str,
    width: int = 1024,
    height: int = 1024,
    model: str = "flux",
    seed: int | None = None,
) -> str:
    """Generate an image from a text prompt via Pollinations.ai."""
    IMAGES_DIR.mkdir(parents=True, exist_ok=True)

    # Build URL
    encoded_prompt = quote(prompt)
    url = f"https://image.pollinations.ai/prompt/{encoded_prompt}"

    params = {
        "width": width,
        "height": height,
        "model": model,
    }
    if seed is not None:
        params["seed"] = seed

    # Generate filename from prompt hash
    prompt_hash = hashlib.md5(prompt.encode()).hexdigest()[:8]
    filename = f"{prompt_hash}.jpg"
    filepath = IMAGES_DIR / filename

    try:
        with httpx.Client(timeout=120.0) as client:
            response = client.get(url, params=params)
            response.raise_for_status()

            # Validate response is an image
            content_type = response.headers.get("content-type", "")
            if "image" not in content_type:
                return f"Error: Expected image, got {content_type}"

            filepath.write_bytes(response.content)

        return f"Image saved to {filepath}"

    except httpx.TimeoutException:
        return "Error: Image generation timed out after 120s"
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 429:
            return "Error: Rate limited by Pollinations.ai. Wait 15 seconds and try again."
        return f"Error: HTTP {e.response.status_code}: {e}"
    except Exception as e:
        return f"Error generating image: {e}"
```

**Pollinations.ai API details** (from research):
- Endpoint: `GET https://image.pollinations.ai/prompt/{prompt}`
- Returns binary image data (JPEG by default)
- Free tier: ~1 req/15s (anonymous), ~1 req/5s (free account)
- Models: `flux` (default, high quality), `turbo` (fast), `stable-diffusion` (classic)
- Parameters: `width`, `height`, `model`, `seed`, `nologo`, `enhance`, `safe`
- 120s timeout recommended (generation can be slow)
- Content-Type: `image/jpeg` (or `image/png` with `transparent=true`)

---

### 4. `image_info` — Get Image Metadata

Get dimensions, format, mode, file size, and EXIF data.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `path` | string | required | Image file path |

**Returns:**
- Width × Height (pixels)
- Format (JPEG, PNG, WebP, GIF, etc.)
- Color mode (RGB, RGBA, L, P, CMYK)
- File size (human-readable)
- Animated (for GIF/WebP)
- EXIF orientation + camera info (if present)

**Implementation pattern** (from Pillow docs):

```python
from PIL import Image
import os

def image_info(path: str) -> str:
    """Get metadata about an image file."""
    if not os.path.isfile(path):
        return f"Error: File not found: {path}"

    try:
        file_size = os.path.getsize(path)
        size_human = _human_size(file_size)

        with Image.open(path) as img:
            img.load()  # Force decode to catch corrupt files

            info = [
                f"Format: {img.format} ({img.format_description})",
                f"Dimensions: {img.size[0]}×{img.size[1]}",
                f"Mode: {img.mode}",
                f"Size: {size_human}",
            ]

            is_animated = getattr(img, "is_animated", False)
            n_frames = getattr(img, "n_frames", 1)
            if is_animated:
                info.append(f"Animated: Yes ({n_frames} frames)")

            # EXIF
            exif = img.getexif()
            if exif:
                from PIL import ExifTags
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

def _human_size(nbytes: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if nbytes < 1024:
            return f"{nbytes:.1f} {unit}"
        nbytes /= 1024
    return f"{nbytes:.1f} TB"
```

---

### 5. `resize_image` — Resize with Aspect Ratio

Resize an image. Preserves aspect ratio by default.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `path` | string | required | Source image path |
| `width` | integer | `None` | Target width |
| `height` | integer | `None` | Target height |
| `percent` | integer | `None` | Scale by percentage (e.g. 50 = half) |
| `output` | string | `None` | Output path (default: overwrite source) |

**Behavior:**
- If both width and height given: resize to fit within bounding box (preserves aspect ratio)
- If only width: scale proportionally
- If only height: scale proportionally
- If percent: scale by percentage
- Uses LANCZOS resampling (highest quality)
- Uses `ImageOps.exif_transpose()` before resize to fix orientation

**Implementation:**

```python
from PIL import Image, ImageOps

def resize_image(
    path: str,
    width: int | None = None,
    height: int | None = None,
    percent: int | None = None,
    output: str | None = None,
) -> str:
    """Resize an image preserving aspect ratio."""
    try:
        img = Image.open(path)
        img.load()
        img = ImageOps.exif_transpose(img)

        original_w, original_h = img.size

        if percent is not None:
            factor = percent / 100.0
            new_size = (int(original_w * factor), int(original_h * factor))
        elif width and height:
            ratio = min(width / original_w, height / original_h)
            new_size = (int(original_w * ratio), int(original_h * ratio))
        elif width:
            ratio = width / original_w
            new_size = (width, int(original_h * ratio))
        elif height:
            ratio = height / original_h
            new_size = (int(original_w * ratio), height)
        else:
            return "Error: Specify width, height, or percent"

        img = img.resize(new_size, Image.Resampling.LANCZOS)
        save_path = output or path
        img.save(save_path)
        img.close()

        return f"Resized {original_w}×{original_h} → {new_size[0]}×{new_size[1]}, saved to {save_path}"

    except Exception as e:
        return f"Error: {e}"
```

---

### 6. `convert_image` — Convert Format

Convert between image formats (PNG, JPEG, WebP, BMP, GIF).

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `path` | string | required | Source image path |
| `format` | string | required | Target format: png, jpeg, webp, bmp, gif |
| `output` | string | `None` | Output path (default: same name, new extension) |
| `quality` | integer | `85` | JPEG/WebP quality (1-100) |

**Key behavior:**
- Handles RGBA→JPEG by compositing onto white background
- Handles palette mode (P)→JPEG by converting to RGB first
- Applies EXIF orientation before conversion
- Strips EXIF by default (privacy)

---

### 7. `crop_image` — Crop Image Region

Crop a rectangular region from an image.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `path` | string | required | Source image path |
| `left` | integer | `0` | Left edge (pixels) |
| `top` | integer | `0` | Top edge (pixels) |
| `right` | integer | required | Right edge (pixels, exclusive) |
| `bottom` | integer | required | Bottom edge (pixels, exclusive) |
| `output` | string | `None` | Output path (default: overwrite source) |

---

## File Structure

```
ares/
├── image_generate.py    # NEW — Pollinations.ai text-to-image
├── image_edit.py        # NEW — Pillow-based image operations
├── code_execution.py    # NEW — Python code execution
├── shell_execution.py   # NEW — Shell command execution
├── tools.py             # Updated — 7 new tool definitions + handlers
```

### `image_generate.py`
- Single function: `generate_image(prompt, width, height, model, seed) -> str`
- Uses `httpx` (already a dependency) for HTTP requests
- Creates `~/ares_images/` directory on first use
- Returns file path on success, error message on failure

### `image_edit.py`
- Functions: `image_info`, `resize_image`, `convert_image`, `crop_image`
- All use Pillow (PIL) — needs to be added as dependency
- All handle EXIF orientation via `ImageOps.exif_transpose()`
- All handle corrupt files via `img.load()` + try/except

### `code_execution.py`
- Single function: `run_code(code, timeout, cwd) -> str`
- Uses `subprocess.run()` with temp file
- 30s default timeout, 300s max
- Returns exit code + stdout + stderr

### `shell_execution.py`
- Single function: `run_command(command, timeout, cwd) -> str`
- Uses `subprocess.Popen()` with `shell=True`
- Process group kills on timeout (Unix)
- 50KB output cap to prevent context overflow
- Returns exit code + stdout + stderr

### `tools.py` changes
- 7 new tool definitions in `get_tool_definitions()`
- 7 new handlers in `ToolExecutor`
- Import from new modules

---

## Safety Model

### Hard Limits

| Resource | Default | Configurable | Purpose |
|----------|---------|--------------|---------|
| Code execution timeout | 30s | Yes (per-call, 1-300) | Prevent infinite loops |
| Command timeout | 30s | Yes (per-call, 1-300) | Prevent hanging processes |
| Command output cap | 50KB | No | Prevent context overflow |
| Image generation timeout | 120s | No | Pollinations.ai can be slow |
| Image file size cap | None | No | Pillow handles this |

### What's NOT Restricted (by design)

- No import restrictions in code execution
- No filesystem restrictions
- No network restrictions
- No command allowlists
- No confirmation prompts

**Rationale:** Ares is a local personal assistant, not a cloud service. The user trusts it with their data (memory, tasks, files). Adding restrictions would make it less useful. Hermes Agent's approach confirms this — their "YOLO mode" disables all checks, and their `--yolo` flag is the primary way developers use it.

### Timeout Kill Strategy

From Hermes Agent + Python subprocess docs:
1. `SIGTERM` (graceful kill)
2. Wait 2 seconds
3. `SIGKILL` (force kill) if still alive
4. On Windows: `proc.kill()` (Windows doesn't support SIGTERM/SIGKILL distinction)

---

## Dependencies

| Package | Used By | Already Installed? |
|---------|---------|-------------------|
| `httpx` | image_generate.py | ✅ Yes (used by LLM client) |
| `Pillow` (PIL) | image_edit.py | ❌ Needs install |

**Install:** `pip install Pillow`

---

## Error Handling

All tools return structured error messages, not exceptions:

| Scenario | Response |
|----------|----------|
| Code execution timeout | `Error: Code execution timed out after {timeout}s` |
| Command timeout | `Error: Command timed out after {timeout}s` + partial output |
| Command output too large | Output truncated at 50KB with notice |
| Image file not found | `Error: File not found: {path}` |
| Corrupt image | `Error reading image: {exception}` |
| Pollinations rate limit | `Error: Rate limited by Pollinations.ai. Wait 15 seconds and try again.` |
| Pollinations server error | `Error: HTTP {status}: {message}` |
| Invalid image format | `Error: Unsupported format: {format}` |
| Crop coordinates invalid | `Error: Invalid crop region` |

---

## Testing Plan

1. **Unit tests for each tool** in `tests/test_power_tools.py`
2. **Code execution tests:** simple print, import, timeout, error output
3. **Shell execution tests:** basic command, pipe, redirect, timeout, output cap
4. **Image generation tests:** mock HTTP response (don't hit Pollinations in tests)
5. **Image editing tests:** real Pillow operations on test fixtures
6. **Timeout tests:** verify kill mechanism works
7. **Edge cases:** empty output, binary output, very long output, corrupt files

---

## Out of Scope

- Full interactive terminal panel (separate project)
- PTY emulation for interactive commands
- Docker/container isolation
- Command approval/allowlist system
- Image generation from URLs (image-to-image)
- Batch image processing
- GPU-accelerated image operations

---

## Sources

- Hermes Agent GitHub: https://github.com/NousResearch/hermes-agent
- Hermes Agent Code Execution Docs: https://hermes-agent.nousresearch.com/docs/user-guide/features/code-execution/
- Hermes Agent Security Guide: https://fast.io/resources/hermes-agent-security/
- Hermes Desktop GitHub: https://github.com/fathah/hermes-desktop
- Hermes IDE: https://github.com/hermes-hq/hermes-ide
- Pollinations.ai API Docs: https://pollinations-ai.com/api.html
- Pollinations GitHub: https://github.com/pollinations/pollinations
- Python subprocess docs: https://docs.python.org/3/library/subprocess.html
- Pillow Image docs: https://pillow.readthedocs.io/en/stable/reference/Image.html
- E2B Sandbox Architecture: https://deepwiki.com/e2b-dev/E2B/1.1-system-architecture
- Async subprocess patterns: https://superfastpython.com/asyncio-subprocess/
