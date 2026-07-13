"""REST/JSON API monitor fetcher."""

from __future__ import annotations

import time
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx

from ares.watcher.fetchers.base import BaseFetcher, FetchResult, FetcherError, validate_target_url
from ares.watcher.models import redact_url


def json_path_get(value: Any, path: str) -> Any:
    """Resolve the practical $.foo.bar[0] subset without a heavy dependency."""
    if path in {"", "$"}:
        return value
    normalized = path.removeprefix("$").lstrip(".").replace("[", ".").replace("]", "")
    current = value
    for segment in filter(None, normalized.split(".")):
        try:
            current = current[int(segment)] if isinstance(current, list) else current[segment]
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise FetcherError(f"JSON path {path!r} was not found at {segment!r}") from exc
    return current


class CustomAPIFetcher(BaseFetcher):
    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._owns_client = client is None
        self.client = client or httpx.AsyncClient(follow_redirects=True, max_redirects=3)

    async def fetch(self, target: str, config: dict[str, Any] | None = None) -> FetchResult:
        cfg, started = config or {}, time.perf_counter()
        try:
            url = validate_target_url(target or cfg.get("api_url", ""), allow_private_network=bool(cfg.get("allow_private_network")))
            method = str(cfg.get("method", "GET")).upper()
            if method not in {"GET", "HEAD", "POST"}:
                raise FetcherError("Custom monitors only allow GET, HEAD, or POST")
            headers = dict(cfg.get("headers") or {})
            current_url, original_origin, response = url, (urlsplit(url).scheme, urlsplit(url).netloc), None
            request_params = cfg.get("params")
            for _ in range(4):
                response = await self.client.request(method,current_url,headers=headers,params=request_params,
                    json=cfg.get("body") if method=="POST" else None,timeout=max(1,min(float(cfg.get("timeout",30)),120)),follow_redirects=False)
                if not response.is_redirect: break
                location=response.headers.get("location")
                if not location: break
                next_url=validate_target_url(urljoin(current_url,location),allow_private_network=bool(cfg.get("allow_private_network")))
                next_origin=(urlsplit(next_url).scheme,urlsplit(next_url).netloc)
                if next_origin!=original_origin and not cfg.get("allow_cross_origin_redirects"):
                    raise FetcherError("Cross-origin redirect blocked; enable allow_cross_origin_redirects explicitly")
                if next_origin!=original_origin:
                    headers={key:value for key,value in headers.items() if key.lower() not in {"authorization","cookie","x-api-key"}}
                current_url=next_url
                request_params=None
            else: raise FetcherError("Too many redirects")
            assert response is not None
            response.raise_for_status()
            if len(response.content) > int(cfg.get("max_response_bytes", 5 * 1024 * 1024)):
                raise FetcherError("API response exceeded the configured safety limit")
            data = response.json()
            extractors = cfg.get("extractors") or []
            extracted = {str(item.get("field") or "value"): json_path_get(data, str(item.get("json_path") or "$")) for item in extractors}
            content = extracted if extractors else data
            return FetchResult(True,content,{"url":redact_url(str(response.url)),"content_type":response.headers.get("content-type", ""),"bytes":len(response.content),"extracted":extracted},
                status_code=response.status_code,elapsed_ms=round((time.perf_counter()-started)*1000))
        except (httpx.HTTPError, ValueError, FetcherError) as exc:
            status = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None
            return FetchResult(False,error=str(exc),status_code=status,elapsed_ms=round((time.perf_counter()-started)*1000))

    async def close(self) -> None:
        if self._owns_client:
            await self.client.aclose()
