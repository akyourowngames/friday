"""Async website fetcher with CSS extraction and normalized monitoring text."""

from __future__ import annotations

import re
import time
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx

from ares.watcher.fetchers.base import BaseFetcher, FetchResult, FetcherError, validate_target_url
from ares.watcher.models import redact_url


DEFAULT_USER_AGENT = "Ares-Watcher/1.0 (+local proactive monitor)"


class WebsiteFetcher(BaseFetcher):
    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._owns_client = client is None
        self.client = client or httpx.AsyncClient(follow_redirects=True, max_redirects=5)

    async def fetch(self, target: str, config: dict[str, Any] | None = None) -> FetchResult:
        cfg = config or {}
        started = time.perf_counter()
        try:
            url = validate_target_url(target, allow_private_network=bool(cfg.get("allow_private_network")))
            headers = {"User-Agent": DEFAULT_USER_AGENT, "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.5"}
            headers.update({str(k): str(v) for k, v in (cfg.get("headers") or {}).items()})
            timeout = max(1.0, min(float(cfg.get("timeout", 30)), 120.0))
            response = None
            current_url = url
            original_origin = (urlsplit(url).scheme, urlsplit(url).netloc)
            for _ in range(6):
                response = await self.client.get(current_url, headers=headers, timeout=timeout, follow_redirects=False)
                if not response.is_redirect:
                    break
                location = response.headers.get("location")
                if not location:
                    break
                next_url = validate_target_url(urljoin(current_url, location), allow_private_network=bool(cfg.get("allow_private_network")))
                next_origin = (urlsplit(next_url).scheme, urlsplit(next_url).netloc)
                if next_origin != original_origin and not cfg.get("allow_cross_origin_redirects"):
                    raise FetcherError("Cross-origin redirect blocked; enable allow_cross_origin_redirects explicitly")
                if next_origin != original_origin:
                    headers = {key:value for key,value in headers.items() if key.lower() not in {"authorization","cookie","x-api-key"}}
                current_url = next_url
            else:
                raise FetcherError("Too many redirects")
            assert response is not None
            response.raise_for_status()
            max_bytes = int(cfg.get("max_response_bytes", 5 * 1024 * 1024))
            payload = response.content
            if len(payload) > max_bytes:
                raise FetcherError(f"Response exceeds the {max_bytes:,}-byte safety limit")
            html = response.text
            content, extracted = self._extract(html, cfg.get("extractors") or [])
            return FetchResult(True, content, {
                "url": redact_url(str(response.url)), "title": self._title(html), "content_type": response.headers.get("content-type", ""),
                "etag": response.headers.get("etag"), "last_modified": response.headers.get("last-modified"),
                "bytes": len(payload), "extracted": extracted,
            }, status_code=response.status_code, elapsed_ms=round((time.perf_counter() - started) * 1000))
        except (httpx.HTTPError, FetcherError, ValueError) as exc:
            status = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None
            return FetchResult(False, error=str(exc), status_code=status, elapsed_ms=round((time.perf_counter() - started) * 1000))

    def _extract(self, html: str, extractors: list[dict[str, Any]]) -> tuple[Any, dict[str, Any]]:
        try:
            from bs4 import BeautifulSoup
        except ImportError as exc:
            raise FetcherError("Website extraction needs beautifulsoup4; reinstall the normal Ares dependencies") from exc
        soup = BeautifulSoup(html, "html.parser")
        for tag in soup(["script", "style", "noscript", "template"]):
            tag.decompose()
        if not extractors:
            text = "\n".join(part.strip() for part in soup.get_text("\n").splitlines() if part.strip())
            return text, {"content": text}
        values: dict[str, Any] = {}
        for item in extractors:
            field = str(item.get("field") or "value")
            selector = item.get("selector")
            if not selector:
                raise FetcherError(f"Extractor {field!r} needs a CSS selector")
            elements = soup.select(str(selector))
            if not elements:
                values[field] = None
                continue
            extract_type = str(item.get("type") or "text").lower()
            attribute = item.get("attribute")
            raw_values = [element.get(str(attribute), "") if attribute else (str(element) if extract_type == "html" else element.get_text(" ", strip=True)) for element in elements]
            normalized = [self._coerce(value, extract_type) for value in raw_values]
            values[field] = normalized if item.get("all") else normalized[0]
        return values, values

    @staticmethod
    def _coerce(value: str, extract_type: str) -> Any:
        if extract_type in {"price", "number", "float"}:
            cleaned = re.sub(r"[^\d,.\-]", "", value)
            if cleaned.count(",") == 1 and "." not in cleaned and len(cleaned.rsplit(",", 1)[-1]) <= 2:
                cleaned = cleaned.replace(",", ".")
            else:
                cleaned = cleaned.replace(",", "")
            try:
                return float(cleaned)
            except ValueError:
                return None
        if extract_type in {"int", "integer"}:
            match = re.search(r"-?\d+", value.replace(",", ""))
            return int(match.group()) if match else None
        if extract_type in {"bool", "boolean"}:
            return value.strip().lower() in {"1", "true", "yes", "available", "in stock"}
        return value.strip()

    @staticmethod
    def _title(html: str) -> str:
        match = re.search(r"<title[^>]*>(.*?)</title>", html, re.I | re.S)
        return re.sub(r"\s+", " ", match.group(1)).strip()[:300] if match else ""

    async def close(self) -> None:
        if self._owns_client:
            await self.client.aclose()
