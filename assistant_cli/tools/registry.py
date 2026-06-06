from __future__ import annotations

from pathlib import Path

import httpx

from assistant_cli.config import Settings

from . import (
    base64_decode,
    base64_encode,
    calculator,
    current_time,
    file_list,
    file_read,
    geocode,
    hash_text,
    json_format,
    note_add,
    note_list,
    password_generate,
    project_manage,
    random_number,
    realtime_search,
    reverse_geocode,
    unit_convert,
    url_fetch,
    uuid_generate,
    weather,
)
from .core import StatelessHttpClient, ToolHandler, ToolRegistry, ToolSpec, ToolContext


TOOL_MODULES = (
    realtime_search,
    weather,
    geocode,
    reverse_geocode,
    current_time,
    calculator,
    unit_convert,
    hash_text,
    base64_encode,
    base64_decode,
    json_format,
    uuid_generate,
    random_number,
    password_generate,
    project_manage,
    url_fetch,
    file_list,
    file_read,
    note_add,
    note_list,
)


def build_default_registry(
    settings: Settings,
    workspace_root: Path | None = None,
    http_client: httpx.Client | None = None,
) -> ToolRegistry:
    context = ToolContext(
        settings=settings,
        workspace_root=(workspace_root or Path.cwd()).resolve(),
        http=http_client or StatelessHttpClient(settings.tool_timeout_seconds),
    )
    registry = ToolRegistry(context)
    for module in TOOL_MODULES:
        spec: ToolSpec = module.SPEC
        handler: ToolHandler = module.run
        registry.register(spec, handler)
    return registry
