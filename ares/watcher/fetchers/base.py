"""Fetcher contracts and shared request safety."""

from __future__ import annotations

import abc
import ipaddress
import socket
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse


@dataclass(slots=True)
class FetchResult:
    success: bool
    content: Any = None
    metadata: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    status_code: int | None = None
    elapsed_ms: int | None = None


class FetcherError(RuntimeError):
    pass


def validate_target_url(url: str, *, allow_private_network: bool = False) -> str:
    """Reject dangerous/non-HTTP targets and local-network SSRF by default."""
    parsed = urlparse(str(url or "").strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise FetcherError("Target must be an absolute http:// or https:// URL")
    if parsed.username or parsed.password:
        raise FetcherError("Credentials in target URLs are not allowed")
    if allow_private_network:
        return parsed.geturl()
    hostname = parsed.hostname.lower()
    if hostname in {"localhost", "localhost.localdomain"} or hostname.endswith(".local"):
        raise FetcherError("Local-network targets require allow_private_network=true")
    try:
        addresses = {item[4][0] for item in socket.getaddrinfo(hostname, parsed.port or (443 if parsed.scheme == "https" else 80), type=socket.SOCK_STREAM)}
    except socket.gaierror as exc:
        raise FetcherError(f"Could not resolve target host: {hostname}") from exc
    for address in addresses:
        ip = ipaddress.ip_address(address)
        if not ip.is_global:
            raise FetcherError("Private, loopback, link-local, and reserved targets are blocked by default")
    return parsed.geturl()


class BaseFetcher(abc.ABC):
    @abc.abstractmethod
    async def fetch(self, target: str, config: dict[str, Any] | None = None) -> FetchResult:
        raise NotImplementedError

    async def close(self) -> None:
        return None
