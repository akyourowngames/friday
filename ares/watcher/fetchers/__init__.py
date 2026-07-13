"""Built-in watcher fetcher registry."""

from ares.watcher.fetchers.base import BaseFetcher, FetchResult, FetcherError
from ares.watcher.fetchers.custom import CustomAPIFetcher
from ares.watcher.fetchers.instagram import InstagramFetcher
from ares.watcher.fetchers.tool import ToolRunner, ToolWorkflowFetcher
from ares.watcher.fetchers.website import WebsiteFetcher


def default_fetchers(
    tool_runner: ToolRunner | None = None,
    *,
    allow_mutating_tools: bool = False,
    max_tool_steps: int = 8,
    max_tool_output_chars: int = 2_000_000,
) -> dict[str, BaseFetcher]:
    shared = {
        "runner": tool_runner,
        "global_allow_mutating": allow_mutating_tools,
        "max_steps": max_tool_steps,
        "max_output_chars": max_tool_output_chars,
    }
    return {
        "website": WebsiteFetcher(),
        "custom": CustomAPIFetcher(),
        "instagram": InstagramFetcher(),
        "tool": ToolWorkflowFetcher(**shared),
        "browser": ToolWorkflowFetcher(**shared, browser=True),
    }


__all__ = [
    "BaseFetcher", "FetchResult", "FetcherError", "WebsiteFetcher",
    "CustomAPIFetcher", "InstagramFetcher", "ToolWorkflowFetcher",
    "ToolRunner", "default_fetchers",
]
