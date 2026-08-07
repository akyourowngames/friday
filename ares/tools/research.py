"""Safe online research downloads, extraction, and reusable report bundles."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import mimetypes
import re
import socket
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

import httpx

from ares.attachments import MAX_ATTACHMENT_BYTES, inspect_attachment
from ares.tools.web import web_search_payload


DEFAULT_MAX_DOWNLOAD_BYTES = 20 * 1024 * 1024
MAX_DOWNLOAD_BYTES = 50 * 1024 * 1024
DOWNLOAD_CHUNK_BYTES = 64 * 1024
MAX_REDIRECTS = 5
# aria2c-style parallel/resume tuning.  A file is only parallelized when the
# server advertises Range support AND the declared size clears the minimum, so
# tiny files never pay the overhead of spawning connections.
_PARALLEL_MIN_BYTES = 1 * 1024 * 1024
_PARALLEL_MAX_CONNECTIONS = 16
_PARALLEL_CHUNK_BYTES = 256 * 1024
_SAFE_FILE_NAME = re.compile(r"[^A-Za-z0-9._ -]+")


def _bounded_bytes(value: Any, default: int = DEFAULT_MAX_DOWNLOAD_BYTES) -> int:
    try:
        return max(1 * 1024 * 1024, min(int(value), MAX_DOWNLOAD_BYTES))
    except (TypeError, ValueError):
        return default


def _safe_filename(value: str, fallback: str = "download") -> str:
    name = Path(unquote(value or "")).name.strip().strip(".")
    name = _SAFE_FILE_NAME.sub("_", name).strip(" ._")
    return name[:140] or fallback


def _filename_from_response(url: str, headers: Any) -> str:
    disposition = str(headers.get("content-disposition") or "")
    match = re.search(r"filename\*?=(?:UTF-8''|[\"'])?([^;\"']+)", disposition, re.IGNORECASE)
    if match:
        return _safe_filename(match.group(1), "download")
    path_name = Path(urlparse(url).path).name
    return _safe_filename(path_name, "download")


def _is_public_address(value: str) -> bool:
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    return bool(address.is_global)


def validate_public_remote_url(url: str) -> str:
    """Allow only public http(s) URLs, including all DNS-resolved addresses."""
    parsed = urlparse(str(url or "").strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("URL must be a complete http:// or https:// address.")
    if parsed.username or parsed.password:
        raise ValueError("URLs with embedded credentials are not allowed.")
    host = parsed.hostname.rstrip(".").casefold()
    if host in {"localhost", "localhost.localdomain"} or host.endswith(".local"):
        raise ValueError("Local network URLs are not allowed for online downloads.")
    try:
        addresses = {item[4][0] for item in socket.getaddrinfo(host, parsed.port or 443, type=socket.SOCK_STREAM)}
    except socket.gaierror as exc:
        raise ValueError(f"Could not resolve download host: {host}") from exc
    if not addresses or any(not _is_public_address(address) for address in addresses):
        raise ValueError("Download host must resolve only to public internet addresses.")
    return parsed.geturl()


class ResearchWorkspace:
    """Owns downloaded source files and generated, citation-rich research bundles."""

    def __init__(self, data_dir: str | Path) -> None:
        self.root = Path(data_dir).expanduser().resolve() / "research"
        self.downloads_dir = self.root / "downloads"
        self.reports_dir = self.root / "reports"

    def download(
        self,
        url: str,
        *,
        filename: str = "",
        max_bytes: int = DEFAULT_MAX_DOWNLOAD_BYTES,
        timeout: float = 30.0,
        query: str = "",
        resolve: bool = True,
        parallel: bool = True,
        connections: int = 8,
    ) -> dict[str, Any]:
        requested_url = validate_public_remote_url(url)
        limit = _bounded_bytes(max_bytes)
        self.downloads_dir.mkdir(parents=True, exist_ok=True)
        content_type = "application/octet-stream"
        temp_path: Path | None = None
        current_url = requested_url
        resolved_once = False

        def _result(target: Path, final_url: str, content_type: str, total: int,
                    sha: str, *, resumed: bool, parallel_used: bool, conns: int) -> dict[str, Any]:
            return {
                "url": requested_url,
                "final_url": final_url,
                "path": str(target.resolve()),
                "name": target.name,
                "content_type": content_type,
                "bytes": total,
                "sha256": sha,
                "redirected": final_url != requested_url,
                "resolved": resolved_once,
                "resumed": resumed,
                "parallel": parallel_used,
                "connections": conns,
                "downloaded_at": datetime.now(timezone.utc).isoformat(),
            }

        try:
            with httpx.Client(
                timeout=httpx.Timeout(float(timeout), connect=min(float(timeout), 10.0)),
                follow_redirects=False,
                headers={"User-Agent": "AresResearch/1.0 (+local personal assistant)"},
            ) as client:
                for _ in range(MAX_REDIRECTS + 1):
                    validate_public_remote_url(current_url)
                    with client.stream("GET", current_url) as response:
                        if response.status_code in {301, 302, 303, 307, 308}:
                            location = response.headers.get("location")
                            if not location:
                                raise ValueError("Redirect response did not include a location.")
                            current_url = str(httpx.URL(current_url).join(location))
                            continue
                        if 200 <= response.status_code < 300:
                            declared = response.headers.get("content-length")
                            size = int(declared) if (declared and declared.isdigit()) else None
                            if size and size > limit:
                                raise ValueError(f"Remote file is larger than the {limit // (1024 * 1024)} MB download limit.")
                            content_type = str(response.headers.get("content-type") or "application/octet-stream").split(";", 1)[0].strip().lower()
                            final_url = str(response.url)
                            proposed = _safe_filename(filename, "") if filename else _filename_from_response(final_url, response.headers)
                            if not Path(proposed).suffix:
                                guessed = mimetypes.guess_extension(content_type) or ""
                                proposed += guessed
                            stem = Path(proposed).stem or "download"
                            suffix = Path(proposed).suffix
                            stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
                            target = self.downloads_dir / f"{stamp}-{_safe_filename(stem, 'download')}{suffix}"
                            temp_path = target.with_suffix(target.suffix + ".part")
                            chunks_path = target.with_suffix(target.suffix + ".chunks")
                            accept_ranges = (response.headers.get("accept-ranges") or "").lower()
                            can_parallel = (
                                parallel
                                and bool(size)
                                and size >= _PARALLEL_MIN_BYTES
                                and "bytes" in accept_ranges
                            )
                            # Resume an interrupted single-stream download via Range.
                            if temp_path.exists():
                                return _result(
                                    target, final_url, content_type,
                                    *self._download_single(client, current_url, limit, temp_path, target),
                                    resumed=True, parallel_used=False, conns=1,
                                )
                            if can_parallel:
                                # Verify Range support with a 1-byte probe before
                                # fanning out. The open GET body is left unread;
                                # httpx serves the probe from a fresh pooled
                                # connection and the original stream closes on exit.
                                supports, real_size = self._probe_range(client, current_url)
                                if supports and real_size and real_size <= limit:
                                    try:
                                        return _result(
                                            target, final_url, content_type,
                                            *self._download_parallel(
                                                client, current_url, real_size, limit,
                                                chunks_path, target, connections,
                                            ),
                                            resumed=False, parallel_used=True, conns=connections,
                                        )
                                    except Exception:
                                        chunks_path.unlink(missing_ok=True)
                                # Range support lied / failed: fall back to single stream.
                                return _result(
                                    target, final_url, content_type,
                                    *self._download_single(client, current_url, limit, temp_path, target),
                                    resumed=False, parallel_used=False, conns=1,
                                )
                            # Default: read the already-open stream directly.
                            digest = hashlib.sha256()
                            total = 0
                            with temp_path.open("wb") as output:
                                for chunk in response.iter_bytes(chunk_size=DOWNLOAD_CHUNK_BYTES):
                                    if not chunk:
                                        continue
                                    total += len(chunk)
                                    if total > limit:
                                        raise ValueError(f"Remote file exceeded the {limit // (1024 * 1024)} MB download limit.")
                                    digest.update(chunk)
                                    output.write(chunk)
                            temp_path.replace(target)
                            return _result(target, final_url, content_type, total, digest.hexdigest(),
                                           resumed=False, parallel_used=False, conns=1)
                        # Non-2xx, non-redirect. If the resource is gone (404/410)
                        # or forbidden, try to rediscover the current link once.
                        if resolve and not resolved_once and response.status_code in (400, 403, 404, 410):
                            candidate = _resolve_download_url(requested_url, query)
                            if candidate and candidate != current_url:
                                current_url = candidate
                                resolved_once = True
                                continue
                        raise ValueError(
                            f"Download failed: HTTP {response.status_code} for {current_url}. "
                            "The URL may be retired or incorrect — try web_search to find the current link."
                        )
                raise ValueError(f"Too many redirects (more than {MAX_REDIRECTS}).")
        except Exception:
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)
            raise

    def _probe_range(self, client: httpx.Client, url: str) -> tuple[bool, int | None]:
        """Confirm the server honors Range requests; return (supported, total_size)."""
        try:
            with client.stream("GET", url, headers={"Range": "bytes=0-0"}) as resp:
                if resp.status_code == 206:
                    content_range = resp.headers.get("content-range", "")
                    match = re.search(r"/(\d+)\s*$", content_range)
                    size = int(match.group(1)) if match else None
                    for _ in resp.iter_bytes():
                        pass
                    return True, size
                for _ in resp.iter_bytes():
                    pass
                return False, None
        except Exception:
            return False, None

    def _download_single(
        self, client: httpx.Client, url: str, limit: int, temp_path: Path, target: Path
    ) -> tuple[int, str]:
        """Single-stream download with resume support (aria2c -c style).

        Returns ``(bytes_written, sha256_hex)``.  If a ``.part`` file already
        exists, requests the remaining bytes via ``Range`` and appends; if the
        server ignores the Range header it restarts cleanly.
        """
        resume_pos = temp_path.stat().st_size if temp_path.exists() else 0
        headers = {"Range": f"bytes={resume_pos}-"} if resume_pos > 0 else {}
        with client.stream("GET", url, headers=headers) as resp:
            if resume_pos > 0:
                if resp.status_code == 206:
                    pass
                elif resp.status_code == 200:
                    temp_path.unlink(missing_ok=True)
                    resume_pos = 0
                else:
                    resp.raise_for_status()
            elif resp.status_code != 200:
                resp.raise_for_status()
            digest = hashlib.sha256()
            if resume_pos > 0:
                with temp_path.open("rb") as prior:
                    for block in iter(lambda: prior.read(1024 * 1024), b""):
                        digest.update(block)
            total = resume_pos
            mode = "ab" if resume_pos > 0 else "wb"
            with temp_path.open(mode) as output:
                for chunk in resp.iter_bytes(chunk_size=DOWNLOAD_CHUNK_BYTES):
                    if not chunk:
                        continue
                    total += len(chunk)
                    if total > limit:
                        raise ValueError(f"Remote file exceeded the {limit // (1024 * 1024)} MB download limit.")
                    digest.update(chunk)
                    output.write(chunk)
            temp_path.replace(target)
            return total, digest.hexdigest()

    def _download_parallel(
        self, client: httpx.Client, url: str, size: int, limit: int,
        temp_path: Path, target: Path, connections: int,
    ) -> tuple[int, str]:
        """aria2c -x style: split one file into N Range requests, write in place.

        Returns ``(bytes_written, sha256_hex)``.  On any failure the ``.chunks``
        temp file is left for the caller to remove; this method never emits a
        silently-corrupt (holey) file because it verifies the final size.
        """
        connections = max(1, min(int(connections), _PARALLEL_MAX_CONNECTIONS))
        chunk_size = max(_PARALLEL_CHUNK_BYTES, (size + connections - 1) // connections)
        ranges: list[tuple[int, int]] = []
        start = 0
        while start < size:
            end = min(start + chunk_size, size) - 1
            ranges.append((start, end))
            start = end + 1

        with temp_path.open("w+b") as handle:
            handle.truncate(size)

        def _fetch_range(span: tuple[int, int]) -> None:
            begin, end = span
            expected = end - begin + 1
            with client.stream("GET", url, headers={"Range": f"bytes={begin}-{end}"}) as resp:
                # A server that ignores Range answers 200 with the full body;
                # writing that at our offset would shred the file, so reject it.
                if resp.status_code != 206:
                    raise ValueError(
                        f"Server did not honor Range for bytes={begin}-{end} "
                        f"(status {resp.status_code}); cannot download in parallel."
                    )
                written = 0
                with temp_path.open("r+b") as fh:
                    fh.seek(begin)
                    for chunk in resp.iter_bytes(chunk_size=DOWNLOAD_CHUNK_BYTES):
                        if not chunk:
                            continue
                        fh.write(chunk)
                        written += len(chunk)
                if written != expected:
                    raise ValueError(
                        f"Range bytes={begin}-{end} returned {written} bytes, expected {expected}."
                    )

        with ThreadPoolExecutor(max_workers=connections) as pool:
            list(pool.map(_fetch_range, ranges))

        actual = temp_path.stat().st_size
        if actual != size:
            raise ValueError(f"Downloaded size {actual} did not match expected {size}.")
        digest = hashlib.sha256()
        written = 0
        with temp_path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
                written += len(block)
        temp_path.replace(target)
        return written, digest.hexdigest()



    def extract_document(
        self,
        *,
        path: str = "",
        url: str = "",
        filename: str = "",
        max_bytes: int = DEFAULT_MAX_DOWNLOAD_BYTES,
        max_chars: int = 30_000,
    ) -> dict[str, Any]:
        download: dict[str, Any] | None = None
        extraction_limit = min(_bounded_bytes(max_bytes), MAX_ATTACHMENT_BYTES)
        if url:
            download = self.download(url, filename=filename, max_bytes=extraction_limit)
            path = str(download["path"])
        if not path:
            raise ValueError("Provide a local path or an online URL.")
        resolved = Path(path).expanduser().resolve(strict=True)
        if not resolved.is_file():
            raise ValueError("Document path is not a regular file.")
        if resolved.stat().st_size > extraction_limit:
            raise ValueError(
                f"Document exceeds the {extraction_limit // (1024 * 1024)} MB extraction limit. "
                "Download it without extraction if you need the original file."
            )
        inspection = inspect_attachment({"name": resolved.name, "path": str(resolved)})
        content = inspection.content
        try:
            char_limit = max(1_000, min(int(max_chars), 200_000))
        except (TypeError, ValueError):
            char_limit = 30_000
        truncated = len(content) > char_limit
        if truncated:
            content = content[:char_limit].rstrip() + f"\n\n[Extraction truncated at {char_limit} chars.]"
        return {
            "path": str(resolved),
            "name": resolved.name,
            "kind": inspection.kind,
            "content_type": inspection.media_type,
            "bytes": inspection.size,
            "content": content,
            "truncated": truncated or "truncated" in content.casefold(),
            "download": download,
        }

    def create_report(
        self,
        query: str,
        *,
        title: str = "",
        max_results: int = 8,
        fetch_top: int = 5,
        provider: str | None = None,
        domains: list[str] | None = None,
        exclude_domains: list[str] | None = None,
        file_type: str = "",
        search_mode: str = "web",
        recency_days: int | None = None,
    ) -> dict[str, Any]:
        payload = web_search_payload(
            query,
            max_results=max_results,
            provider=provider,
            fetch_top=fetch_top,
            domains=domains or [],
            exclude_domains=exclude_domains or [],
            file_type=file_type,
            search_mode=search_mode,
            recency_days=recency_days,
            cache_ttl_seconds=300,
        )
        report_title = (title or f"Research brief: {query}").strip()
        slug = _safe_filename(report_title.lower().replace(" ", "-"), "research-brief")
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        self.reports_dir.mkdir(parents=True, exist_ok=True)
        path = self.reports_dir / f"{stamp}-{slug[:80]}.md"
        lines = [
            f"# {report_title}",
            "",
            f"Generated: {datetime.now(timezone.utc).isoformat()}",
            f"Query: {query}",
            f"Provider: {payload.get('provider', 'unknown')}",
            "",
            "## Executive signal",
            payload.get("summary") or "No provider summary was available.",
            "",
            "## Sources",
        ]
        for source in payload.get("source_matrix", []):
            lines.append(
                f"{source.get('index')}. [{source.get('title') or 'Untitled'}]({source.get('url')}) "
                f"— {source.get('quality_label', 'standard')}, {source.get('freshness_label', 'undated')}"
            )
        fetched = [item for item in payload.get("fetched", []) if item.get("content")]
        if fetched:
            lines.extend(["", "## Extracted evidence"])
            for item in fetched:
                excerpt = str(item.get("content") or "").strip()
                if len(excerpt) > 1800:
                    excerpt = excerpt[:1800].rstrip() + "…"
                lines.extend([
                    "",
                    f"### {item.get('title') or item.get('url')}",
                    f"Source: {item.get('final_url') or item.get('url')}",
                    "",
                    excerpt or "No readable content extracted.",
                ])
        if payload.get("errors"):
            lines.extend(["", "## Retrieval notes", *[f"- {item}" for item in payload["errors"]]])
        path.write_text("\n".join(lines).strip() + "\n", encoding="utf-8")
        return {
            "path": str(path.resolve()),
            "name": path.name,
            "kind": "markdown",
            "query": query,
            "sources": len(payload.get("results", [])),
            "fetched_sources": len(fetched),
            "summary": payload.get("summary", ""),
            "payload": payload,
        }


def json_result(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False)


def _resolve_download_url(original_url: str, query: str = "") -> str | None:
    """Rediscover a working download URL via web search (best effort).

    Direct fetches often 404 because the document moved (e.g. OWASP retired its
    2021 PDF).  Returns a validated public URL, or None if nothing usable is
    found.  Used once by :meth:`ResearchWorkspace.download` on a 404/410.
    """
    if not query:
        stem = Path(urlparse(original_url).path).stem or original_url
        query = f"{stem} official download"
    try:
        payload = web_search_payload(query, max_results=5, search_mode="web")
    except Exception:
        return None
    for result in payload.get("results", []):
        candidate = str(result.get("url") or "")
        if not candidate.startswith(("http://", "https://")):
            continue
        try:
            return validate_public_remote_url(candidate)
        except ValueError:
            continue
    return None
