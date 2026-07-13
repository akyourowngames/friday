"""Instagram Graph API watcher.

This uses an explicit Graph API token/endpoint instead of browser credential
scraping.  DM access requires Meta permissions; mentions/comments can be
monitored through a configured Graph endpoint.
"""

from __future__ import annotations

from typing import Any

from ares.watcher.fetchers.custom import CustomAPIFetcher
from ares.watcher.fetchers.base import FetchResult


class InstagramFetcher(CustomAPIFetcher):
    async def fetch(self, target: str, config: dict[str, Any] | None = None) -> FetchResult:
        cfg = dict(config or {})
        endpoint = str(cfg.get("api_url") or target or "")
        token = str(cfg.get("access_token") or "")
        if not endpoint or not token:
            return FetchResult(False, error="Instagram monitors require api_url and access_token from a permitted Meta Graph API app")
        params = dict(cfg.get("params") or {})
        params.setdefault("access_token", token)
        cfg["params"] = params
        cfg["method"] = "GET"
        return await super().fetch(endpoint, cfg)
