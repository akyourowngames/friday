from html.parser import HTMLParser
import time
from pathlib import Path
from urllib.parse import urlparse

import httpx

from config import settings
from tools.registry import tool
from tools.runtime import (
    coerce_bool,
    emit_trace,
    error_payload,
    make_trace,
    normalize_int,
    normalize_response_format,
    normalize_timeout_ms,
    structured_error,
    structured_success,
    utc_now_iso,
)


_BROWSER_VERSION = "2.0.0"
_BROWSER_LOGIN_VERSION = "2.0.0"
_ENGINES = ("auto", "playwright", "httpx")
_SOURCES = ("auto", "selector", "meta", "text", "title", "url")
_READ_MODES = ("fields", "text", "dom", "full")


class _PageParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.title = ""
        self.meta = []
        self.parts = []
        self._in_title = False
        self._skip_depth = 0

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag == "title":
            self._in_title = True
        if tag in ("script", "style", "noscript", "svg"):
            self._skip_depth += 1
        if tag == "meta":
            values = {}
            for key, value in attrs:
                values[str(key or "").lower()] = str(value or "")
            content = values.get("content", "")
            if content:
                self.meta.append(
                    {
                        "name": values.get("name", ""),
                        "property": values.get("property", ""),
                        "content": content,
                    }
                )

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag == "title":
            self._in_title = False
        if tag in ("script", "style", "noscript", "svg") and self._skip_depth:
            self._skip_depth -= 1

    def handle_data(self, data):
        text = _compact_text(data)
        if not text:
            return
        if self._in_title and not self.title:
            self.title = text
        if not self._skip_depth:
            self.parts.append(text)

    def text(self) -> str:
        return _compact_text(" ".join(self.parts))


def _compact_text(value: str) -> str:
    return " ".join(str(value or "").split())


def _resolve_path(path: str) -> Path:
    return Path(path or ".").expanduser().resolve()


def _target_name(raw: str) -> str:
    value = raw.strip().strip("#").strip()
    lowered = value.lower()
    prefix = "target:"
    if lowered.startswith(prefix):
        return value[len(prefix):].strip()
    return value


def _line_key_value(line: str) -> tuple[str, str]:
    cleaned = line.strip()
    if cleaned.startswith("- "):
        cleaned = cleaned[2:].strip()
    key, marker, value = cleaned.partition(":")
    if not marker:
        return "", ""
    return key.strip().lower(), value.strip()


def _parse_field(value: str) -> dict:
    parts = [part.strip() for part in str(value or "").split("|") if part.strip()]
    if not parts:
        return {}
    first = parts[0]
    key, marker, field_value = first.partition(":")
    if marker and key.strip().lower() == "field":
        field = {"name": field_value.strip()}
    else:
        field = {"name": first.strip()}
    for part in parts[1:]:
        part_key, part_marker, part_value = part.partition(":")
        if part_marker:
            field[part_key.strip().lower()] = part_value.strip()
    if not field.get("source"):
        field["source"] = "auto"
    return field


def _load_targets(config_path: str) -> tuple[dict[str, dict], dict | None]:
    path = _resolve_path(config_path)
    if not path.exists():
        return {}, error_payload(
            "CONFIG_NOT_FOUND",
            "The browser target markdown file does not exist.",
            "config_path",
            str(path),
            "existing markdown file",
            False,
            "Create the target markdown file or pass a valid config_path.",
        )
    if not path.is_file():
        return {}, error_payload(
            "INVALID_CONFIG_PATH",
            "The browser target path is not a file.",
            "config_path",
            str(path),
            "markdown file path",
            False,
            "Pass a markdown file path.",
        )
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except UnicodeDecodeError:
        return {}, error_payload(
            "CONFIG_DECODE_FAILED",
            "The browser target markdown file is not UTF-8 text.",
            "config_path",
            str(path),
            "UTF-8 markdown file",
            False,
            "Save the target file as UTF-8 markdown.",
        )

    targets = {}
    current = None
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("# ") or line.startswith("### "):
            continue
        if line.startswith("## "):
            name = _target_name(line[3:])
            current = {"name": name, "fields": []}
            targets[name] = current
            continue
        if current is None:
            continue
        key, value = _line_key_value(line)
        if not key:
            continue
        if key == "field":
            field = _parse_field(value)
            if field.get("name"):
                current["fields"].append(field)
        else:
            current[key] = value
    return targets, None


def _load_dom_policy(config_path: str) -> dict:
    path = _resolve_path(config_path or settings.browser_dom_policy_file)
    policy = {
        "max_blocks": 80,
        "max_block_chars": 600,
        "max_links": 40,
        "max_headings": 30,
        "main_selectors": "main || article || [role=main] || #content || .content || body",
        "skip_tags": "script, style, noscript, svg, path, iframe",
        "heading_tags": "h1, h2, h3, h4, h5, h6",
        "block_tags": "p, li, td, th, blockquote, pre, figcaption, dd, dt",
    }
    if not path.exists():
        return policy
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return policy
    for raw in lines:
        line = raw.strip()
        if not line.startswith("- "):
            continue
        key, value = _line_key_value(line)
        if not key or not value:
            continue
        if key in ("max_blocks", "max_block_chars", "max_links", "max_headings"):
            try:
                policy[key] = int(value)
            except ValueError:
                continue
        else:
            policy[key] = value
    return policy


def _collect_dom_playwright(page, policy: dict) -> dict:
    payload = {
        "max_blocks": int(policy.get("max_blocks", 80)),
        "max_block_chars": int(policy.get("max_block_chars", 600)),
        "max_links": int(policy.get("max_links", 40)),
        "max_headings": int(policy.get("max_headings", 30)),
        "main_selectors": str(policy.get("main_selectors", "body")),
        "skip_tags": [item.strip() for item in str(policy.get("skip_tags", "")).split(",") if item.strip()],
        "heading_tags": [item.strip() for item in str(policy.get("heading_tags", "")).split(",") if item.strip()],
        "block_tags": [item.strip() for item in str(policy.get("block_tags", "")).split(",") if item.strip()],
    }
    try:
        snapshot = page.evaluate(
            """(policy) => {
                const skip = new Set((policy.skip_tags || []).map((t) => t.toUpperCase()));
                const blockTags = new Set((policy.block_tags || []).map((t) => t.toUpperCase()));
                const headingTags = new Set((policy.heading_tags || []).map((t) => t.toUpperCase()));
                const maxBlocks = policy.max_blocks || 80;
                const maxChars = policy.max_block_chars || 600;
                const maxLinks = policy.max_links || 40;
                const maxHeadings = policy.max_headings || 30;
                const selectors = String(policy.main_selectors || 'body').split('||').map((s) => s.trim()).filter(Boolean);
                let root = null;
                for (const sel of selectors) {
                    try {
                        root = document.querySelector(sel);
                        if (root) break;
                    } catch (e) {}
                }
                if (!root) root = document.body;
                const compact = (value) => String(value || '').split(/\\s+/).join(' ').trim();
                const blocks = [];
                const links = [];
                const headings = [];
                const walk = (el, depth) => {
                    if (!el || blocks.length >= maxBlocks) return;
                    const tag = String(el.tagName || '').toUpperCase();
                    if (skip.has(tag)) return;
                    if (headingTags.has(tag)) {
                        const text = compact(el.innerText || el.textContent);
                        if (text && headings.length < maxHeadings) {
                            headings.push({ tag: tag.toLowerCase(), text: text.slice(0, maxChars), depth });
                        }
                    }
                    if (tag === 'A') {
                        const href = el.getAttribute('href') || '';
                        const text = compact(el.innerText || el.textContent);
                        if (href && links.length < maxLinks) {
                            links.push({ text: text.slice(0, 120), href: href.slice(0, 500) });
                        }
                    }
                    if (blockTags.has(tag)) {
                        const text = compact(el.innerText || el.textContent);
                        if (text.length >= 20) {
                            blocks.push({ tag: tag.toLowerCase(), text: text.slice(0, maxChars), depth });
                        }
                    }
                    const children = el.children ? Array.from(el.children) : [];
                    for (const child of children) walk(child, depth + 1);
                };
                walk(root, 0);
                return {
                    blocks,
                    links,
                    headings,
                    root_selector: selectors[0] || 'body',
                    block_count: blocks.length,
                    link_count: links.length,
                    heading_count: headings.length,
                };
            }""",
            payload,
        )
        return snapshot if isinstance(snapshot, dict) else {}
    except Exception:
        return {"blocks": [], "links": [], "headings": [], "block_count": 0, "link_count": 0, "heading_count": 0}


def _split_values(value: str) -> list[str]:
    return [item.strip() for item in str(value or "").split(",") if item.strip()]


def _split_selectors(value: str) -> list[str]:
    return [item.strip() for item in str(value or "").split("||") if item.strip()]


def _safe_session_name(value: str) -> str:
    name = []
    for char in str(value or "").strip():
        if char.isalnum() or char in ("-", "_"):
            name.append(char)
        elif char in (" ", ".", "/", "\\", ":"):
            name.append("_")
    normalized = "".join(name).strip("_")
    return normalized or "browser_session"


def _auth_state_path(session_name: str, target_config: dict | None = None, explicit_path: str = "") -> Path:
    if explicit_path:
        return _resolve_path(explicit_path)
    if target_config and target_config.get("storage_state"):
        return _resolve_path(str(target_config.get("storage_state", "")))
    auth_dir = _resolve_path(settings.browser_auth_dir)
    return auth_dir / f"{_safe_session_name(session_name)}.json"


def _storage_state_for_load(session_name: str, target_config: dict | None, storage_state: str) -> str:
    candidate = _auth_state_path(session_name, target_config, storage_state)
    if candidate.exists() and candidate.is_file():
        return str(candidate)
    return ""


def _url_is_valid(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


def _browser_trace(started_at: str, started: float, inputs_received: int, schema_valid: bool, execution_path: str, status: str, output_fields: int, engine: str = "", error_code: str | None = None) -> dict:
    systems = ["browser"] if engine == "playwright" else ["web"] if engine == "httpx" else []
    return make_trace(
        "browser_extract",
        _BROWSER_VERSION,
        started_at,
        started,
        inputs_received,
        schema_valid,
        execution_path,
        status,
        output_fields,
        {"count": 1 if systems else 0, "systems": systems},
        error_code,
    )


def _browser_error(error: dict, response_format: str, trace_enabled: bool, started: float, started_at: str, inputs_received: int, legacy: str, execution_path: str = "input_validation", engine: str = "", schema_valid: bool = False):
    trace = _browser_trace(started_at, started, inputs_received, schema_valid, execution_path, "FAILED", 1, engine, error["code"])
    emit_trace(trace, trace_enabled)
    if response_format == "structured":
        return structured_error("browser_extract", _BROWSER_VERSION, error, started, trace)
    return legacy


def _browser_login_error(error: dict, response_format: str, trace_enabled: bool, started: float, started_at: str, inputs_received: int, legacy: str, execution_path: str = "input_validation", schema_valid: bool = False):
    trace = make_trace(
        "browser_login_session",
        _BROWSER_LOGIN_VERSION,
        started_at,
        started,
        inputs_received,
        schema_valid,
        execution_path,
        "FAILED",
        1,
        {"count": 1 if execution_path != "input_validation" else 0, "systems": ["browser"] if execution_path != "input_validation" else []},
        error["code"],
    )
    emit_trace(trace, trace_enabled)
    if response_format == "structured":
        return structured_error("browser_login_session", _BROWSER_LOGIN_VERSION, error, started, trace)
    return legacy


def _load_httpx_page(url: str, timeout_seconds: float, max_text_chars: int) -> tuple[dict | None, dict | None]:
    try:
        response = httpx.get(
            url,
            timeout=timeout_seconds,
            follow_redirects=True,
            headers={"User-Agent": settings.browser_user_agent},
        )
        response.raise_for_status()
    except httpx.TimeoutException:
        return None, error_payload(
            "PAGE_TIMEOUT",
            "The page did not load before timeout_ms.",
            "url",
            url,
            "page load before timeout",
            True,
            "Retry with a larger timeout_ms or a simpler target page.",
        )
    except httpx.HTTPError as exc:
        return None, error_payload(
            "PAGE_LOAD_FAILED",
            "The page failed to load over the HTTP fallback engine.",
            "url",
            url,
            "reachable http or https page",
            True,
            f"HTTP engine reported {exc.__class__.__name__}.",
        )
    parser = _PageParser()
    parser.feed(response.text)
    text = parser.text()
    return {
        "requested_url": url,
        "final_url": str(response.url),
        "status_code": int(response.status_code),
        "title": parser.title,
        "text": text[:max_text_chars],
        "text_truncated": len(text) > max_text_chars,
        "meta": parser.meta,
        "selector_values": {},
        "engine_used": "httpx",
        "degraded": False,
        "degraded_reason": "",
    }, None


def _playwright_missing_error() -> dict:
    return error_payload(
        "BROWSER_DEPENDENCY_MISSING",
        "Playwright is not installed in this Python environment.",
        "engine",
        "playwright",
        "installed playwright package and browser runtime",
        False,
        "Install the project requirements and Playwright browser runtime before forcing engine=playwright.",
    )


def _load_playwright_page(
    url: str,
    timeout_ms: int,
    wait_until: str,
    max_text_chars: int,
    fields: list[dict],
    storage_state: str = "",
    read_mode: str = "fields",
    dom_policy: dict | None = None,
) -> tuple[dict | None, dict | None]:
    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        return None, _playwright_missing_error()

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            context = None
            try:
                context_kwargs = {"user_agent": settings.browser_user_agent}
                if storage_state:
                    context_kwargs["storage_state"] = storage_state
                context = browser.new_context(**context_kwargs)
                page = context.new_page()
                response = page.goto(url, wait_until=wait_until or "domcontentloaded", timeout=timeout_ms)
                title = page.title()
                try:
                    body_text = page.locator("body").inner_text(timeout=min(timeout_ms, 5000))
                except Exception:
                    body_text = ""
                meta = page.locator("meta").evaluate_all(
                    "(nodes) => nodes.map((node) => ({name: node.getAttribute('name') || '', property: node.getAttribute('property') || '', content: node.getAttribute('content') || ''})).filter((item) => item.content)"
                )
                selector_values = {}
                for field in fields:
                    selector = field.get("selector", "")
                    if not selector:
                        continue
                    values = []
                    attribute = field.get("attribute", "")
                    for candidate in _split_selectors(selector):
                        for node in page.query_selector_all(candidate)[:5]:
                            value = ""
                            if attribute:
                                value = node.get_attribute(attribute) or ""
                            if not value:
                                value = node.get_attribute("content") or ""
                            if not value:
                                try:
                                    value = node.inner_text(timeout=1000)
                                except Exception:
                                    value = ""
                            value = _compact_text(value)
                            if value:
                                values.append(value)
                    if values:
                        selector_values[field.get("name", "")] = values
                final_url = page.url
                status_code = response.status if response is not None else None
                dom_snapshot = {}
                if read_mode in ("dom", "full"):
                    dom_snapshot = _collect_dom_playwright(page, dom_policy or _load_dom_policy(""))
            finally:
                if context is not None:
                    try:
                        context.close()
                    except Exception:
                        pass
                browser.close()
        text = _compact_text(body_text)
        page_payload = {
            "requested_url": url,
            "final_url": final_url,
            "status_code": status_code,
            "title": title,
            "text": text[:max_text_chars],
            "text_truncated": len(text) > max_text_chars,
            "meta": meta,
            "selector_values": selector_values,
            "engine_used": "playwright",
            "degraded": False,
            "degraded_reason": "",
            "storage_state_used": bool(storage_state),
        }
        if read_mode in ("dom", "full"):
            page_payload["dom"] = dom_snapshot
            page_payload["dom_block_count"] = dom_snapshot.get("block_count", 0)
        return page_payload, None
    except Exception as exc:
        return None, error_payload(
            "PAGE_LOAD_FAILED",
            "The browser page load failed before extraction completed.",
            "url",
            url,
            "browser-loadable page",
            True,
            f"Browser engine reported {exc.__class__.__name__}.",
        )


def _load_page(
    url: str,
    engine: str,
    timeout_ms: int,
    wait_until: str,
    max_text_chars: int,
    fields: list[dict],
    storage_state: str = "",
    read_mode: str = "fields",
    dom_policy: dict | None = None,
) -> tuple[dict | None, dict | None]:
    if engine == "playwright":
        return _load_playwright_page(url, timeout_ms, wait_until, max_text_chars, fields, storage_state, read_mode, dom_policy)
    if engine == "httpx":
        page, error = _load_httpx_page(url, timeout_ms / 1000, max_text_chars)
        if page is not None and read_mode in ("dom", "full"):
            page["dom"] = {"blocks": [], "links": [], "headings": [], "block_count": 0, "note": "dom requires playwright engine"}
            page["degraded"] = True
            page["degraded_reason"] = "DOM iteration requires Playwright; HTTP engine returned text only."
        return page, error
    page, error = _load_playwright_page(url, timeout_ms, wait_until, max_text_chars, fields, storage_state, read_mode, dom_policy)
    if page is not None:
        return page, None
    if error and error.get("code") == "BROWSER_DEPENDENCY_MISSING":
        fallback, fallback_error = _load_httpx_page(url, timeout_ms / 1000, max_text_chars)
        if fallback is not None:
            fallback["degraded"] = True
            fallback["degraded_reason"] = "Playwright unavailable; used HTTP fallback."
            return fallback, None
        return None, fallback_error
    return None, error


def _token_has_digit(token: str) -> bool:
    for char in token:
        if char.isdigit():
            return True
    return False


def _clean_value_token(token: str) -> str:
    return str(token or "").strip(" \t\r\n,;:()[]{}<>")


def _last_numeric_token(text: str) -> str:
    for token in reversed(str(text or "").split()):
        cleaned = _clean_value_token(token)
        if _token_has_digit(cleaned):
            return cleaned
    return ""


def _first_numeric_token(text: str) -> str:
    for token in str(text or "").split():
        cleaned = _clean_value_token(token)
        if _token_has_digit(cleaned):
            return cleaned
    return ""


def _snippet(text: str, position: int, size: int = 120) -> str:
    start = max(0, position - size // 2)
    end = min(len(text), position + size // 2)
    return _compact_text(text[start:end])


def _value_near_label(text: str, label: str) -> tuple[str, str]:
    haystack = str(text or "")
    needle = str(label or "").strip()
    if not haystack or not needle:
        return "", ""
    position = haystack.lower().find(needle.lower())
    if position < 0:
        return "", ""
    before = haystack[max(0, position - 80):position]
    after = haystack[position + len(needle):position + len(needle) + 80]
    value = _last_numeric_token(before) or _first_numeric_token(after)
    return value, _snippet(haystack, position)


def _meta_text(page: dict) -> str:
    parts = []
    for item in page.get("meta", []):
        content = item.get("content") if isinstance(item, dict) else ""
        if content:
            parts.append(str(content))
    return _compact_text(" ".join(parts))


def _extract_field(field: dict, page: dict) -> dict:
    name = field.get("name", "")
    source = str(field.get("source", "auto") or "auto").strip().lower()
    if source not in _SOURCES:
        source = "auto"
    label = field.get("label", name)
    selector_values = page.get("selector_values", {}).get(name, [])
    if source in ("auto", "selector") and selector_values:
        return {
            "name": name,
            "value": selector_values[0],
            "source": "selector",
            "matched": True,
            "evidence": selector_values[0][:160],
        }
    if source == "title":
        title = page.get("title", "")
        return {
            "name": name,
            "value": title,
            "source": "title",
            "matched": bool(title),
            "evidence": title[:160],
        }
    if source == "url":
        final_url = page.get("final_url", "")
        return {
            "name": name,
            "value": final_url,
            "source": "url",
            "matched": bool(final_url),
            "evidence": final_url,
        }
    if source in ("auto", "meta"):
        value, evidence = _value_near_label(_meta_text(page), label)
        if value or evidence:
            return {
                "name": name,
                "value": value,
                "source": "meta",
                "matched": bool(value),
                "evidence": evidence,
            }
    if source in ("auto", "text"):
        value, evidence = _value_near_label(page.get("text", ""), label)
        if value or evidence:
            return {
                "name": name,
                "value": value,
                "source": "text",
                "matched": bool(value),
                "evidence": evidence,
            }
    return {
        "name": name,
        "value": "",
        "source": source,
        "matched": False,
        "evidence": "",
    }


def _legacy_result(result: dict) -> str:
    lines = [
        f"Target: {result['target'] or 'direct-url'}",
        f"URL: {result['final_url'] or result['requested_url']}",
        f"Engine: {result['engine_used']}",
    ]
    if result.get("degraded"):
        lines.append(f"Degraded: {result['degraded_reason']}")
    if result.get("title"):
        lines.append(f"Title: {result['title']}")
    if result["fields"]:
        lines.append("Fields:")
        for field in result["fields"]:
            status = "matched" if field["matched"] else "empty"
            value = field["value"] if field["value"] else "(no value)"
            lines.append(f"- {field['name']}: {value} [{status}, source: {field['source']}]")
    else:
        lines.append("Fields: none requested")
    return "\n".join(lines)


def _legacy_read_result(result: dict) -> str:
    lines = [
        f"URL: {result.get('final_url') or result.get('requested_url')}",
        f"Title: {result.get('title', '')}",
        f"Engine: {result.get('engine_used', '')}",
    ]
    if result.get("degraded"):
        lines.append(f"Degraded: {result.get('degraded_reason', '')}")
    text = result.get("text", "")
    if text:
        lines.append("")
        lines.append(text[:4000])
    dom = result.get("dom", {})
    blocks = dom.get("blocks", []) if isinstance(dom, dict) else []
    if blocks:
        lines.append("")
        lines.append(f"DOM blocks ({len(blocks)}):")
        for block in blocks[:12]:
            if isinstance(block, dict):
                lines.append(f"- [{block.get('tag', 'block')}] {block.get('text', '')[:200]}")
    return "\n".join(lines)


def _browser_read_impl(
    target: str,
    url: str,
    config_path: str,
    engine: str,
    timeout_ms: int,
    max_text_chars: int,
    storage_state: str,
    read_mode: str,
    dom_policy_path: str,
    response_format: str,
    trace_enabled: bool,
    started: float,
    started_at: str,
):
    response_format = normalize_response_format(response_format)
    trace_enabled = coerce_bool(trace_enabled)
    config_path = config_path or settings.browser_targets_file
    engine = str(engine or "auto").strip().lower()
    read_mode = str(read_mode or "full").strip().lower()
    if read_mode not in _READ_MODES:
        read_mode = "full"
    return browser_extract(
        target=target,
        url=url,
        fields="",
        config_path=config_path,
        engine=engine,
        timeout_ms=timeout_ms,
        max_text_chars=max_text_chars,
        storage_state=storage_state,
        read_mode=read_mode,
        dom_policy_path=dom_policy_path,
        response_format=response_format,
        trace_enabled=trace_enabled,
    )


@tool(
    name="browser_read_page",
    description="Load a URL and return readable page text plus DOM-iterated blocks for scraping and reading page content",
    examples=[
        "read the page at https://example.com",
        "scrape dom content from my configured target",
        "open URL and return full page text",
    ],
    param_descriptions={
        "url": "http or https URL to read",
        "target": "Optional named target from BROWSER_TARGETS.md",
        "read_mode": "text, dom, or full (default full)",
        "config_path": "Browser targets markdown file",
        "dom_policy_path": "DOM iteration policy markdown file",
        "engine": "auto, playwright, or httpx",
        "timeout_ms": "Page load timeout in milliseconds",
        "max_text_chars": "Maximum visible text characters",
        "storage_state": "Optional Playwright storage state path",
        "response_format": "legacy or structured",
        "trace_enabled": "Emit machine-readable trace when true",
    },
)
def browser_read_page(
    url: str = "",
    target: str = "",
    read_mode: str = "full",
    config_path: str = "",
    dom_policy_path: str = "",
    engine: str = "auto",
    timeout_ms: int = 0,
    max_text_chars: int = 0,
    storage_state: str = "",
    response_format: str = "legacy",
    trace_enabled: bool = False,
):
    started = time.perf_counter()
    started_at = utc_now_iso()
    result = _browser_read_impl(
        target,
        url,
        config_path,
        engine,
        timeout_ms,
        max_text_chars,
        storage_state,
        read_mode,
        dom_policy_path,
        response_format,
        trace_enabled,
        started,
        started_at,
    )
    if response_format == "structured" and isinstance(result, dict) and "meta" in result:
        result["meta"]["tool"] = "browser_read_page"
    return result


@tool(
    name="browser_extract",
    description=(
        "Load a configured website target or direct URL, then extract page details "
        "from browser-visible text, meta tags, title, URL, or configured selectors."
    ),
    examples=[
        "check the configured instagram_profile target",
        "extract followers from my configured social page",
        "open a URL and return configured page details",
    ],
    param_descriptions={
        "target": "Named target from tools/BROWSER_TARGETS.md. Optional when url is provided.",
        "url": "Direct http or https URL. Overrides target URL when provided.",
        "fields": "Optional comma-separated field names to extract. Empty uses target fields.",
        "config_path": "Markdown target file. Defaults to KING_BROWSER_TARGETS_FILE.",
        "engine": "auto, playwright, or httpx. Auto prefers browser automation and falls back to HTTP when Playwright is unavailable.",
        "timeout_ms": "Page load timeout in milliseconds, from 1 to 60000.",
        "max_text_chars": "Maximum visible text characters retained for extraction, from 500 to 50000.",
        "storage_state": "Optional Playwright storage-state file to reuse a saved login session.",
        "response_format": "legacy or structured. Default legacy preserves existing behavior.",
        "read_mode": "fields (default), text, dom, or full page read including DOM blocks",
        "dom_policy_path": "Optional DOM policy markdown path. Defaults to KING_BROWSER_DOM_POLICY_FILE.",
        "trace_enabled": "When true, emit a machine-readable trace entry.",
    },
)
def browser_extract(
    target: str = "",
    url: str = "",
    fields: str = "",
    config_path: str = "",
    engine: str = "auto",
    timeout_ms: int = 0,
    max_text_chars: int = 0,
    storage_state: str = "",
    read_mode: str = "fields",
    dom_policy_path: str = "",
    response_format: str = "legacy",
    trace_enabled: bool = False,
):
    started = time.perf_counter()
    started_at = utc_now_iso()
    inputs_received = 12
    response_format = normalize_response_format(response_format)
    trace_enabled = coerce_bool(trace_enabled)
    config_path = config_path or settings.browser_targets_file
    engine = str(engine or "auto").strip().lower()
    target = str(target or "").strip()
    url = str(url or "").strip()
    read_mode = str(read_mode or "fields").strip().lower()
    dom_policy = _load_dom_policy(dom_policy_path)

    if read_mode not in _READ_MODES:
        error = error_payload(
            "INVALID_READ_MODE",
            "read_mode must be fields, text, dom, or full.",
            "read_mode",
            read_mode,
            "fields, text, dom, or full",
            False,
            "Use read_mode='dom' for structured DOM blocks or read_mode='full' for text and DOM.",
        )
        return _browser_error(error, response_format, trace_enabled, started, started_at, inputs_received, "Browser extraction failed: invalid read_mode")
    if engine not in _ENGINES:
        error = error_payload(
            "INVALID_ENGINE",
            "engine must be auto, playwright, or httpx.",
            "engine",
            engine,
            "auto, playwright, or httpx",
            False,
            "Use engine='auto' for normal browser extraction.",
        )
        return _browser_error(error, response_format, trace_enabled, started, started_at, inputs_received, "Browser extraction failed: invalid engine")

    timeout_value, timeout_error = normalize_timeout_ms(timeout_ms, settings.browser_default_timeout_ms)
    if timeout_error is not None:
        return _browser_error(timeout_error, response_format, trace_enabled, started, started_at, inputs_received, "Browser extraction failed: invalid timeout_ms")
    max_text_value = settings.browser_max_text_chars if max_text_chars in (None, 0, "0", "") else max_text_chars
    max_text_chars, max_error = normalize_int(
        max_text_value,
        "max_text_chars",
        settings.browser_max_text_chars,
        500,
        50000,
        "Use max_text_chars between 500 and 50000.",
        "INVALID_MAX_TEXT_CHARS",
    )
    if max_error is not None:
        return _browser_error(max_error, response_format, trace_enabled, started, started_at, inputs_received, "Browser extraction failed: invalid max_text_chars")

    targets, config_error = _load_targets(config_path)
    if config_error is not None:
        return _browser_error(config_error, response_format, trace_enabled, started, started_at, inputs_received, "Browser extraction failed: target config unavailable")

    target_config = {}
    if target:
        target_config = targets.get(target, {})
        if not target_config:
            error = error_payload(
                "TARGET_NOT_FOUND",
                "The requested browser target was not found in the markdown config.",
                "target",
                target,
                "target section in browser markdown config",
                False,
                "Add the target to the markdown file or pass a direct url.",
            )
            return _browser_error(error, response_format, trace_enabled, started, started_at, inputs_received, "Browser extraction failed: target not found")

    requested_url = url or str(target_config.get("url", "")).strip()
    if not requested_url:
        error = error_payload(
            "EMPTY_URL",
            "No URL was provided and the target has no URL.",
            "url",
            requested_url,
            "http or https URL",
            False,
            "Pass url directly or add url to the target section.",
        )
        return _browser_error(error, response_format, trace_enabled, started, started_at, inputs_received, "Browser extraction failed: empty URL")
    if not _url_is_valid(requested_url):
        error = error_payload(
            "INVALID_URL",
            "url must be an absolute http or https URL.",
            "url",
            requested_url,
            "absolute http or https URL",
            False,
            "Include the URL scheme and host.",
        )
        return _browser_error(error, response_format, trace_enabled, started, started_at, inputs_received, "Browser extraction failed: invalid URL")

    requested_field_names = _split_values(fields)
    configured_fields = list(target_config.get("fields", []))
    if requested_field_names:
        by_name = {field.get("name", ""): field for field in configured_fields}
        configured_fields = [by_name.get(name, {"name": name, "source": "auto", "label": name}) for name in requested_field_names]
    wait_until = str(target_config.get("wait_until", "domcontentloaded") or "domcontentloaded").strip()
    session_name = target or urlparse(requested_url).netloc or "browser_session"
    resolved_storage_state = _storage_state_for_load(session_name, target_config, storage_state)

    page, page_error = _load_page(
        requested_url,
        engine,
        timeout_value,
        wait_until,
        max_text_chars,
        configured_fields,
        resolved_storage_state,
        read_mode,
        dom_policy,
    )
    if page_error is not None:
        return _browser_error(page_error, response_format, trace_enabled, started, started_at, inputs_received, "Browser extraction failed: page load failed", "page_load", engine, True)

    extracted_fields = []
    if read_mode in ("fields", "full"):
        extracted_fields = [_extract_field(field, page) for field in configured_fields]
    matched_count = sum(1 for field in extracted_fields if field.get("matched"))
    result = {
        "target": target,
        "requested_url": requested_url,
        "final_url": page.get("final_url", ""),
        "status_code": page.get("status_code"),
        "title": page.get("title", ""),
        "engine_requested": engine,
        "engine_used": page.get("engine_used", ""),
        "degraded": page.get("degraded", False),
        "degraded_reason": page.get("degraded_reason", ""),
        "storage_state_used": page.get("storage_state_used", False),
        "storage_state_path": resolved_storage_state if resolved_storage_state else "",
        "fields_requested": [field.get("name", "") for field in configured_fields],
        "fields": extracted_fields,
        "field_count": len(extracted_fields),
        "matched_count": matched_count,
        "text_truncated": page.get("text_truncated", False),
        "text_sample": page.get("text", "")[:500],
        "read_mode": read_mode,
        "dom": page.get("dom", {}),
        "dom_block_count": page.get("dom_block_count", 0),
        "full_text": page.get("text", "") if read_mode in ("text", "full") else "",
        "source_status": "ok" if matched_count or read_mode in ("text", "dom", "full") or not configured_fields else "loaded_no_field_values",
    }
    if read_mode == "text" and not page.get("text"):
        result["source_status"] = "loaded_no_text"
    if read_mode == "dom" and not page.get("dom_block_count"):
        result["source_status"] = "loaded_no_dom_blocks"
    status = "SUCCESS" if result["source_status"] == "ok" else "PARTIAL"
    trace = _browser_trace(started_at, started, inputs_received, True, "page_extract", status, len(result), result["engine_used"], None if status == "SUCCESS" else "NO_FIELD_VALUES")
    emit_trace(trace, trace_enabled)
    if response_format == "structured":
        return structured_success("browser_extract", _BROWSER_VERSION, result, started, trace)
    return _legacy_result(result)


@tool(
    name="browser_login_session",
    description=(
        "Open a visible browser page for manual login and save Playwright storage "
        "state for later browser_extract calls. Does not collect credentials."
    ),
    examples=[
        "open the configured instagram profile login page and save session",
        "create a browser login session for a URL",
    ],
    param_descriptions={
        "target": "Named target from tools/BROWSER_TARGETS.md. Optional when url is provided.",
        "url": "Direct login or page URL. Overrides target login_url or target URL when provided.",
        "session_name": "Name for the saved session file. Defaults to target or URL host.",
        "config_path": "Markdown target file. Defaults to KING_BROWSER_TARGETS_FILE.",
        "storage_state": "Optional explicit storage-state output path.",
        "timeout_ms": "How long to keep the visible browser open for login, from 1 to configured maximum.",
        "response_format": "legacy or structured. Default legacy preserves existing behavior.",
        "trace_enabled": "When true, emit a machine-readable trace entry.",
    },
)
def browser_login_session(
    target: str = "",
    url: str = "",
    session_name: str = "",
    config_path: str = "",
    storage_state: str = "",
    timeout_ms: int = 0,
    response_format: str = "legacy",
    trace_enabled: bool = False,
):
    started = time.perf_counter()
    started_at = utc_now_iso()
    inputs_received = 8
    response_format = normalize_response_format(response_format)
    trace_enabled = coerce_bool(trace_enabled)
    config_path = config_path or settings.browser_targets_file
    target = str(target or "").strip()
    url = str(url or "").strip()
    session_name = str(session_name or "").strip()

    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        error = _playwright_missing_error()
        return _browser_login_error(error, response_format, trace_enabled, started, started_at, inputs_received, "Browser login failed: Playwright unavailable")

    timeout_value, timeout_error = normalize_timeout_ms(timeout_ms, settings.browser_login_timeout_ms, settings.browser_login_timeout_max_ms)
    if timeout_error is not None:
        return _browser_login_error(timeout_error, response_format, trace_enabled, started, started_at, inputs_received, "Browser login failed: invalid timeout_ms")

    targets, config_error = _load_targets(config_path)
    if config_error is not None:
        return _browser_login_error(config_error, response_format, trace_enabled, started, started_at, inputs_received, "Browser login failed: target config unavailable")

    target_config = {}
    if target:
        target_config = targets.get(target, {})
        if not target_config:
            error = error_payload(
                "TARGET_NOT_FOUND",
                "The requested browser target was not found in the markdown config.",
                "target",
                target,
                "target section in browser markdown config",
                False,
                "Add the target to the markdown file or pass a direct url.",
            )
            return _browser_login_error(error, response_format, trace_enabled, started, started_at, inputs_received, "Browser login failed: target not found")

    login_url = url or str(target_config.get("login_url", "") or target_config.get("url", "")).strip()
    if not login_url:
        error = error_payload(
            "EMPTY_URL",
            "No login URL was provided and the target has no login_url or URL.",
            "url",
            login_url,
            "http or https URL",
            False,
            "Pass url directly or add login_url to the target section.",
        )
        return _browser_login_error(error, response_format, trace_enabled, started, started_at, inputs_received, "Browser login failed: empty URL")
    if not _url_is_valid(login_url):
        error = error_payload(
            "INVALID_URL",
            "url must be an absolute http or https URL.",
            "url",
            login_url,
            "absolute http or https URL",
            False,
            "Include the URL scheme and host.",
        )
        return _browser_login_error(error, response_format, trace_enabled, started, started_at, inputs_received, "Browser login failed: invalid URL")

    parsed = urlparse(login_url)
    session_key = session_name or target or parsed.netloc or "browser_session"
    state_path = _auth_state_path(session_key, target_config, storage_state)
    state_path.parent.mkdir(parents=True, exist_ok=True)

    final_url = login_url
    title = ""
    save_count = 0
    closed_by_user = False
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=False)
            context = browser.new_context(user_agent=settings.browser_user_agent)
            page = context.new_page()
            page.goto(login_url, wait_until=str(target_config.get("wait_until", "domcontentloaded") or "domcontentloaded"), timeout=min(timeout_value, 60000))
            page.bring_to_front()
            interval_ms = max(1000, min(5000, timeout_value))
            deadline = time.perf_counter() + (timeout_value / 1000)
            while time.perf_counter() < deadline:
                try:
                    page.wait_for_timeout(interval_ms)
                    final_url = page.url
                    title = page.title()
                    context.storage_state(path=str(state_path))
                    save_count += 1
                except Exception:
                    closed_by_user = True
                    break
            if not closed_by_user:
                final_url = page.url
                title = page.title()
                context.storage_state(path=str(state_path))
                save_count += 1
            try:
                context.close()
            except Exception:
                pass
            try:
                browser.close()
            except Exception:
                pass
    except Exception as exc:
        if state_path.exists():
            closed_by_user = True
        else:
            error = error_payload(
                "LOGIN_SESSION_FAILED",
                "The visible browser login session failed before storage state was saved.",
                "url",
                login_url,
                "manual login session completed before timeout",
                True,
                f"Browser engine reported {exc.__class__.__name__}.",
            )
            return _browser_login_error(error, response_format, trace_enabled, started, started_at, inputs_received, "Browser login failed: session did not save", "login_session", True)

    if not state_path.exists():
        error = error_payload(
            "STORAGE_STATE_NOT_SAVED",
            "The visible browser session ended without a saved storage-state file.",
            "storage_state",
            str(state_path),
            "saved Playwright storage-state file",
            True,
            "Retry the login session and leave the browser open until the tool reports the saved state.",
        )
        return _browser_login_error(error, response_format, trace_enabled, started, started_at, inputs_received, "Browser login failed: storage state was not saved", "login_session", True)

    result = {
        "target": target,
        "login_url": login_url,
        "final_url": final_url,
        "title": title,
        "session_name": _safe_session_name(session_key),
        "storage_state_path": str(state_path),
        "storage_state_exists": state_path.exists(),
        "storage_state_saves": save_count,
        "closed_by_user": closed_by_user,
        "timeout_ms": timeout_value,
        "credentials_captured": False,
        "credential_policy": "manual browser entry only; credentials are not returned by the tool",
    }
    status = "SUCCESS" if state_path.exists() else "FAILED"
    trace = make_trace(
        "browser_login_session",
        _BROWSER_LOGIN_VERSION,
        started_at,
        started,
        inputs_received,
        True,
        "visible_login_session",
        status,
        len(result),
        {"count": 1, "systems": ["browser"]},
        None if state_path.exists() else "STORAGE_STATE_NOT_SAVED",
    )
    emit_trace(trace, trace_enabled)
    if response_format == "structured":
        return structured_success("browser_login_session", _BROWSER_LOGIN_VERSION, result, started, trace)
    return "\n".join(
        [
            f"Login session saved: {state_path.exists()}",
            f"URL: {final_url}",
            f"Title: {title}",
            f"Storage state: {state_path}",
            "Credentials captured: no",
        ]
    )
