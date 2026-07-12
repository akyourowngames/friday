"""Community skill registry client and safe SKILL.md archive installer.

The registry is an *untrusted content* boundary.  This module deliberately
separates downloading a skill from installing it, validates the archive before
anything is written to the skills directory, and never executes a downloaded
file.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import stat
import tempfile
import time
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path, PurePosixPath
from typing import Any, Callable
from urllib.parse import quote
from zipfile import BadZipFile, ZipFile

import httpx

from ares.models import SkillDependency, SkillRegistry
from ares.skills import SKILL_NAME_RE, Skill, SkillManager

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SECONDS = 8.0
MAX_ARCHIVE_BYTES = 12 * 1024 * 1024
MAX_ARCHIVE_FILES = 80
MAX_UNCOMPRESSED_BYTES = 20 * 1024 * 1024
MAX_COMPRESSION_RATIO = 100
_SAFE_SKILL_FILE_SUFFIXES = {".json", ".md", ".toml", ".txt", ".yaml", ".yml"}
_SAFE_SKILL_FILE_NAMES = {"license", "notice", "readme"}
_CATEGORY_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")


class RegistryError(RuntimeError):
    """A registry failed in a way that can be shown safely to the user."""


class SkillValidationError(ValueError):
    """A downloaded archive does not meet Ares' instruction-only policy."""


@dataclass(frozen=True)
class SkillResult:
    """A compact, display-safe search result from one skill registry."""

    slug: str
    name: str
    description: str
    version: str
    owner: str
    score: float
    registry: str
    canonical_url: str = ""
    suspicious: bool = False
    stars: int | None = None
    downloads: int | None = None

    @property
    def reference(self) -> str:
        """A registry-qualified reference that disambiguates duplicate slugs."""
        return f"@{self.owner}/{self.slug}" if self.owner else self.slug


@dataclass(frozen=True)
class SkillVersion:
    version: str
    created_at: str = ""
    changelog: str = ""
    security_status: str = ""


@dataclass(frozen=True)
class SkillDetail:
    slug: str
    name: str
    description: str
    version: str
    owner: str
    registry: str
    canonical_url: str = ""
    files: list[str] = field(default_factory=list)
    dependencies: list[SkillDependency] = field(default_factory=list)
    suspicious: bool = False
    security_status: str = "unknown"
    metadata: dict[str, Any] = field(default_factory=dict)
    stars: int | None = None
    downloads: int | None = None

    @property
    def reference(self) -> str:
        return f"@{self.owner}/{self.slug}" if self.owner else self.slug


@dataclass(frozen=True)
class SkillInstallation:
    """Result of a validated local skill installation."""

    skill: Skill
    path: Path
    dependencies: list[SkillDependency]
    replaced: bool = False


ClientFactory = Callable[[], httpx.AsyncClient]


class SkillRegistryClient:
    """Search, inspect, download, and publish through configured registries.

    Calls only ever target registries explicitly listed in ``AppConfig``.  A
    failed secondary registry is recorded in ``last_errors`` but never hides
    usable results from another registry.
    """

    def __init__(
        self,
        registries: list[SkillRegistry],
        *,
        client_factory: ClientFactory | None = None,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self.registries = sorted(
            [registry for registry in registries if registry.enabled],
            key=lambda registry: (-registry.priority, registry.name),
        )
        self._client_factory = client_factory or (
            lambda: httpx.AsyncClient(follow_redirects=True, timeout=timeout_seconds)
        )
        self.timeout_seconds = timeout_seconds
        self.last_errors: dict[str, str] = {}

    def configured_registry(self, name: str) -> SkillRegistry | None:
        needle = str(name or "").strip().casefold()
        return next((registry for registry in self.registries if registry.name.casefold() == needle), None)

    async def search(self, query: str, registry: str | None = None) -> list[SkillResult]:
        """Search all selected registries concurrently and rank stable results."""
        query = str(query or "").strip()
        if not query:
            raise ValueError("A skill search query is required.")
        targets = self._select_registries(registry)
        results = await self._collect(targets, lambda target: self._search_registry(target, query))
        flattened = [item for items in results for item in items]
        priority = {item.name: item.priority for item in self.registries}
        return sorted(
            flattened,
            key=lambda item: (-item.score, -priority.get(item.registry, 0), item.name.casefold()),
        )

    async def get_skill(self, slug: str, registry: str | None = None) -> SkillDetail | None:
        """Return one skill's public metadata, trying configured registries in order."""
        for target in self._select_registries(registry):
            try:
                return await self._get_skill_detail(target, slug)
            except RegistryError as exc:
                self.last_errors[target.name] = str(exc)
        return None

    async def get_versions(self, slug: str, registry: str | None = None) -> list[SkillVersion]:
        """Get public version metadata where a registry supports it."""
        for target in self._select_registries(registry):
            try:
                return await self._get_versions(target, slug)
            except RegistryError as exc:
                self.last_errors[target.name] = str(exc)
        return []

    async def download(
        self,
        slug: str,
        version: str | None = None,
        registry: str | None = None,
    ) -> bytes | None:
        """Download a hosted ZIP archive without following third-party handoffs.

        ClawHub can return a JSON descriptor for a GitHub-backed skill.  Ares
        does not silently follow that external handoff: the user can inspect
        the registry record first rather than accepting an arbitrary archive.
        """
        bare_slug, owner_handle = _split_skill_reference(slug)
        for target in self._select_registries(registry):
            try:
                response = await self._request(
                    target,
                    "/download",
                    params={
                        key: value
                        for key, value in {
                            "slug": bare_slug,
                            "ownerHandle": owner_handle,
                            "version": version,
                        }.items()
                        if value
                    },
                    timeout=30.0,
                )
                content_type = response.headers.get("content-type", "").casefold()
                if "json" in content_type or response.content.lstrip().startswith((b"{", b"[")):
                    raise RegistryError(
                        "Registry returned an external source handoff, not a hosted ZIP. "
                        "Inspect the skill with /skills info before installing it."
                    )
                if not response.content:
                    raise RegistryError("Registry returned an empty skill archive.")
                return response.content
            except RegistryError as exc:
                self.last_errors[target.name] = str(exc)
        return None

    async def whoami(self, registry: str = "clawhub") -> str | None:
        """Return the authenticated registry handle without exposing the token."""
        target = self.configured_registry(registry)
        if target is None:
            raise ValueError(f"Registry '{registry}' is not configured or enabled.")
        if not target.auth_token:
            return None
        response = await self._request(target, "/whoami")
        data = self._json(response, target)
        return str(data.get("handle") or data.get("user", {}).get("handle") or "") or None

    async def publish(
        self,
        *,
        skill: Skill,
        registry: str = "clawhub",
    ) -> dict[str, Any]:
        """Publish an already validated local skill using a configured token.

        The public ClawHub API accepts a multipart ``payload`` plus files.  We
        intentionally publish only the validated, instruction-only files that
        Ares would install locally.
        """
        target = self.configured_registry(registry)
        if target is None:
            raise ValueError(f"Registry '{registry}' is not configured or enabled.")
        if not target.auth_token:
            raise RegistryError("Publishing requires a configured registry token. Use /skills login first.")
        if target.name.casefold() != "clawhub" and "clawhub.ai" not in target.api_base:
            raise RegistryError("Publishing is currently supported only for the configured ClawHub registry.")

        files: list[tuple[str, tuple[str, bytes, str]]] = []
        for path in [skill.path, *skill.files]:
            relative = path.relative_to(skill.root).as_posix()
            _validate_skill_file(relative, path.read_bytes(), executable=False)
            files.append(("files[]", (relative, path.read_bytes(), "text/plain; charset=utf-8")))
        payload = {
            "slug": skill.name,
            "displayName": skill.name.replace("-", " ").title(),
            "summary": skill.description,
            "version": skill.version,
        }
        headers = self._headers(target)
        try:
            async with self._client_factory() as client:
                response = await client.post(
                    self._url(target, "/skills"),
                    data={"payload": json.dumps(payload)},
                    files=files,
                    headers=headers,
                    timeout=30.0,
                )
                self._raise_for_status(response, target)
                return self._json(response, target)
        except httpx.HTTPError as exc:
            raise RegistryError(f"Registry '{target.name}' could not be reached.") from exc

    def _select_registries(self, name: str | None) -> list[SkillRegistry]:
        if not name:
            return list(self.registries)
        target = self.configured_registry(name)
        if target is None:
            raise ValueError(f"Registry '{name}' is not configured or enabled.")
        return [target]

    async def _collect(self, registries: list[SkillRegistry], operation) -> list[list[Any]]:
        if not registries:
            return []
        responses = await asyncio.gather(
            *(operation(registry) for registry in registries),
            return_exceptions=True,
        )
        results: list[list[Any]] = []
        for registry, response in zip(registries, responses):
            if isinstance(response, Exception):
                message = str(response) or f"Registry '{registry.name}' is unavailable."
                self.last_errors[registry.name] = message
                logger.info("Skill registry %s skipped: %s", registry.name, message)
                continue
            self.last_errors.pop(registry.name, None)
            results.append(response)
        return results

    async def _search_registry(self, registry: SkillRegistry, query: str) -> list[SkillResult]:
        response = await self._request(
            registry,
            "/search",
            params={"q": query, "limit": registry.search_limit, "nonSuspiciousOnly": "true"},
        )
        data = self._json(response, registry)
        raw_items = data.get("results") or data.get("skills") or data.get("items") or []
        if not isinstance(raw_items, list):
            raise RegistryError("Registry returned an invalid search response.")
        return [result for item in raw_items if (result := self._to_result(registry, item)) is not None]

    async def _get_skill_detail(self, registry: SkillRegistry, slug: str) -> SkillDetail:
        bare_slug, owner_handle = _split_skill_reference(slug)
        response = await self._request(
            registry,
            f"/skills/{quote(bare_slug, safe='')}",
            params={"ownerHandle": owner_handle} if owner_handle else None,
        )
        data = self._json(response, registry)
        payload = data.get("skill") if isinstance(data.get("skill"), dict) else data
        if not isinstance(payload, dict):
            raise RegistryError("Registry returned invalid skill metadata.")
        latest = data.get("latestVersion") if isinstance(data.get("latestVersion"), dict) else {}
        metadata = data.get("metadata") if isinstance(data.get("metadata"), dict) else payload.get("metadata") or {}
        owner_data = data.get("owner") if isinstance(data.get("owner"), dict) else payload.get("owner") or {}
        owner = _owner_name(owner_data)
        version = str(latest.get("version") or payload.get("version") or _nested(payload, "tags", "latest") or "unknown")
        moderation = data.get("moderation") if isinstance(data.get("moderation"), dict) else payload.get("moderation") or {}
        suspicious = bool(moderation.get("isSuspicious") or moderation.get("isMalwareBlocked"))
        return SkillDetail(
            slug=str(payload.get("slug") or bare_slug),
            name=str(payload.get("displayName") or payload.get("name") or bare_slug),
            description=str(payload.get("summary") or payload.get("description") or ""),
            version=version,
            owner=owner,
            registry=registry.name,
            canonical_url=_canonical_skill_url(registry, owner, str(payload.get("slug") or bare_slug), payload),
            files=_file_names(latest.get("files") or payload.get("files") or []),
            dependencies=parse_skill_dependencies(metadata),
            suspicious=suspicious,
            security_status=str(moderation.get("verdict") or moderation.get("status") or "unknown"),
            metadata=dict(metadata),
            stars=_public_count(payload, data, latest, names=("stars", "starCount", "stargazersCount")),
            downloads=_public_count(payload, data, latest, names=("downloads", "downloadCount", "installs", "installCount")),
        )

    async def _get_versions(self, registry: SkillRegistry, slug: str) -> list[SkillVersion]:
        bare_slug, owner_handle = _split_skill_reference(slug)
        response = await self._request(
            registry,
            f"/skills/{quote(bare_slug, safe='')}/versions",
            params={"ownerHandle": owner_handle} if owner_handle else None,
        )
        data = self._json(response, registry)
        entries = data.get("versions") or data.get("items") or []
        if not isinstance(entries, list):
            raise RegistryError("Registry returned an invalid versions response.")
        versions = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            security = entry.get("security") if isinstance(entry.get("security"), dict) else {}
            versions.append(
                SkillVersion(
                    version=str(entry.get("version") or "unknown"),
                    created_at=str(entry.get("createdAt") or entry.get("publishedAt") or ""),
                    changelog=str(entry.get("changelog") or ""),
                    security_status=str(security.get("status") or security.get("verdict") or ""),
                )
            )
        return versions

    def _to_result(self, registry: SkillRegistry, raw: Any) -> SkillResult | None:
        item = raw.get("skill", raw) if isinstance(raw, dict) else None
        if not isinstance(item, dict):
            return None
        slug = str(item.get("slug") or item.get("name") or "").strip()
        if not slug:
            return None
        owner = _owner_name(raw.get("owner") or item.get("owner") or {})
        latest = raw.get("latestVersion") if isinstance(raw.get("latestVersion"), dict) else {}
        moderation = raw.get("moderation") if isinstance(raw.get("moderation"), dict) else item.get("moderation") or {}
        score_value = raw.get("score", item.get("score", 0))
        try:
            score = float(score_value or 0)
        except (TypeError, ValueError):
            score = 0.0
        return SkillResult(
            slug=slug,
            name=str(item.get("displayName") or item.get("name") or slug),
            description=str(item.get("summary") or item.get("description") or ""),
            version=str(latest.get("version") or item.get("version") or _nested(item, "tags", "latest") or "unknown"),
            owner=owner,
            score=score,
            registry=registry.name,
            canonical_url=_canonical_skill_url(registry, owner, slug, item),
            suspicious=bool(moderation.get("isSuspicious") or moderation.get("isMalwareBlocked")),
            stars=_public_count(raw, item, latest, names=("stars", "starCount", "stargazersCount")),
            downloads=_public_count(raw, item, latest, names=("downloads", "downloadCount", "installs", "installCount")),
        )

    async def _request(
        self,
        registry: SkillRegistry,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> httpx.Response:
        try:
            async with self._client_factory() as client:
                response = await client.get(
                    self._url(registry, path),
                    params=params,
                    headers=self._headers(registry),
                    timeout=timeout or self.timeout_seconds,
                )
        except httpx.HTTPError as exc:
            raise RegistryError(f"Registry '{registry.name}' could not be reached.") from exc
        self._raise_for_status(response, registry)
        return response

    @staticmethod
    def _url(registry: SkillRegistry, path: str) -> str:
        return f"{registry.api_base.rstrip('/')}/{path.lstrip('/')}"

    @staticmethod
    def _headers(registry: SkillRegistry) -> dict[str, str]:
        headers = {"Accept": "application/json", "User-Agent": "ares-marketplace/0.1"}
        if registry.auth_token:
            headers["Authorization"] = f"Bearer {registry.auth_token}"
        return headers

    @staticmethod
    def _raise_for_status(response: httpx.Response, registry: SkillRegistry) -> None:
        if response.status_code == 429:
            retry_after = response.headers.get("retry-after")
            suffix = f" Retry after {retry_after}s." if retry_after else ""
            raise RegistryError(f"Registry '{registry.name}' rate limited this request.{suffix}")
        if response.status_code >= 400:
            if response.status_code == 401:
                detail = "authentication is required or the configured token is invalid"
            elif response.status_code == 404:
                detail = "the requested skill was not found"
            else:
                detail = f"returned HTTP {response.status_code}"
            raise RegistryError(f"Registry '{registry.name}': {detail}.")

    @staticmethod
    def _json(response: httpx.Response, registry: SkillRegistry) -> dict[str, Any]:
        try:
            payload = response.json()
        except (TypeError, ValueError) as exc:
            raise RegistryError(f"Registry '{registry.name}' returned invalid JSON.") from exc
        if not isinstance(payload, dict):
            raise RegistryError(f"Registry '{registry.name}' returned an invalid JSON object.")
        return payload


class SafeSkillInstaller:
    """Install a ZIP only after proving it is a small instruction-only skill."""

    def __init__(self, skills_dir: Path | str) -> None:
        self.skills_dir = Path(skills_dir).expanduser()

    def install(
        self,
        archive: bytes,
        *,
        provenance: dict[str, Any] | None = None,
        replace: bool = False,
    ) -> SkillInstallation:
        """Validate then atomically install a community skill archive.

        The temporary directory is in the destination filesystem so the final
        ``os.replace`` is atomic.  Invalid archives leave the skills directory
        untouched.
        """
        if not archive:
            raise SkillValidationError("The downloaded archive is empty.")
        if len(archive) > MAX_ARCHIVE_BYTES:
            raise SkillValidationError("The archive exceeds the 12 MiB safety limit.")
        self.skills_dir.mkdir(parents=True, exist_ok=True)
        temp_root: Path | None = None
        backup: Path | None = None
        try:
            with ZipFile(BytesIO(archive)) as bundle:
                entries, source_root = self._validate_archive(bundle)
                temp_root = Path(tempfile.mkdtemp(prefix=".ares-skill-", dir=self.skills_dir))
                for info, relative in entries:
                    payload = bundle.read(info)
                    _validate_skill_file(relative.as_posix(), payload, executable=False)
                    output = temp_root.joinpath(*relative.parts)
                    output.parent.mkdir(parents=True, exist_ok=True)
                    output.write_bytes(payload)

            skill_file = temp_root / "SKILL.md"
            skill = SkillManager.parse_skill_file(skill_file)
            if not skill.content.strip() or "#" not in skill.content:
                raise SkillValidationError("SKILL.md must contain non-empty markdown instructions with a heading.")
            if not SKILL_NAME_RE.match(skill.name):
                raise SkillValidationError("Skill metadata contains an invalid name.")
            category = _safe_category(skill.category)
            dependencies = parse_skill_dependencies(skill.metadata)
            record = {
                "schema_version": 1,
                "installed_at": int(time.time()),
                "dependencies": [dependency.model_dump() for dependency in dependencies],
                **(provenance or {}),
            }
            (temp_root / ".ares-marketplace.json").write_text(
                json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )
            destination = self.skills_dir / category / skill.name
            destination.parent.mkdir(parents=True, exist_ok=True)
            replaced = destination.exists()
            if replaced and not replace:
                raise FileExistsError(f"Skill '{skill.name}' is already installed. Use /skills update {skill.name}.")
            if replaced:
                backup = destination.with_name(f".{destination.name}.backup-{int(time.time() * 1000)}")
                os.replace(destination, backup)
            os.replace(temp_root, destination)
            temp_root = None
            if backup is not None:
                shutil.rmtree(backup, ignore_errors=True)
            installed = SkillManager.parse_skill_file(destination / "SKILL.md")
            return SkillInstallation(installed, destination, dependencies, replaced=replaced)
        except (BadZipFile, OSError, ValueError) as exc:
            if backup is not None and backup.exists():
                destination = backup.with_name(backup.name.split(".backup-", 1)[0].lstrip("."))
                if not destination.exists():
                    os.replace(backup, destination)
            if isinstance(exc, SkillValidationError):
                raise
            raise SkillValidationError(str(exc) or "Unable to validate the skill archive.") from exc
        finally:
            if temp_root is not None:
                shutil.rmtree(temp_root, ignore_errors=True)

    def _validate_archive(self, bundle: ZipFile) -> tuple[list[tuple[Any, PurePosixPath]], PurePosixPath]:
        infos = [info for info in bundle.infolist() if not info.is_dir()]
        if not infos:
            raise SkillValidationError("The archive does not contain files.")
        if len(infos) > MAX_ARCHIVE_FILES:
            raise SkillValidationError(f"The archive contains more than {MAX_ARCHIVE_FILES} files.")
        total_size = 0
        normalized: list[tuple[Any, PurePosixPath]] = []
        for info in infos:
            relative = _safe_archive_path(info.filename)
            mode = info.external_attr >> 16
            if stat.S_ISLNK(mode):
                raise SkillValidationError("Skill archives cannot contain symbolic links.")
            if mode & 0o111:
                raise SkillValidationError("Skill archives cannot contain executable files.")
            if info.file_size > MAX_UNCOMPRESSED_BYTES or info.file_size < 0:
                raise SkillValidationError("An archive file exceeds the safety limit.")
            if info.compress_size and info.file_size / info.compress_size > MAX_COMPRESSION_RATIO:
                raise SkillValidationError("Archive compression ratio exceeds the safety limit.")
            total_size += info.file_size
            if total_size > MAX_UNCOMPRESSED_BYTES:
                raise SkillValidationError("Uncompressed archive content exceeds the 20 MiB safety limit.")
            normalized.append((info, relative))

        skill_entries = [(info, path) for info, path in normalized if path.name.casefold() == "skill.md"]
        if len(skill_entries) != 1:
            raise SkillValidationError("The archive must contain exactly one SKILL.md file.")
        source_root = skill_entries[0][1].parent
        extracted: list[tuple[Any, PurePosixPath]] = []
        for info, path in normalized:
            try:
                relative = path.relative_to(source_root)
            except ValueError as exc:
                raise SkillValidationError("All archive files must stay inside the SKILL.md directory.") from exc
            if not relative.parts:
                continue
            extracted.append((info, relative))
        return extracted, source_root


def parse_skill_dependencies(metadata: dict[str, Any] | None) -> list[SkillDependency]:
    """Normalize the common MCP dependency shapes found in skill frontmatter."""
    metadata = metadata or {}
    raw: list[Any] = []
    for key in ("dependencies", "requires", "mcp_servers", "mcp"):
        value = metadata.get(key)
        if isinstance(value, list):
            raw.extend(value)
        elif isinstance(value, dict):
            for nested_key in ("mcp", "mcp_servers", "servers"):
                nested = value.get(nested_key)
                if isinstance(nested, list):
                    raw.extend({"type": "mcp_server", "name": item} if isinstance(item, str) else item for item in nested)
            if value.get("name"):
                raw.append(value)
        elif isinstance(value, str):
            raw.append(value)

    dependencies: list[SkillDependency] = []
    seen: set[tuple[str, str]] = set()
    for item in raw:
        if isinstance(item, str):
            kind, _, name = item.partition(":")
            if not name:
                kind, name = "mcp_server", kind
            kind = {"mcp": "mcp_server", "server": "mcp_server"}.get(kind.strip().casefold(), kind.strip().casefold())
            data: dict[str, Any] = {"type": kind or "mcp_server", "name": name.strip()}
        elif isinstance(item, dict):
            data = dict(item)
            kind = str(data.get("type") or data.get("kind") or "mcp_server").casefold()
            data["type"] = {"mcp": "mcp_server", "server": "mcp_server"}.get(kind, kind)
            data["name"] = str(data.get("name") or data.get("server") or "").strip()
        else:
            continue
        if data.get("type") not in {"mcp_server", "tool", "skill"} or not data.get("name"):
            continue
        try:
            dependency = SkillDependency(**data)
        except (TypeError, ValueError):
            continue
        key = (dependency.type, dependency.name.casefold())
        if key not in seen:
            dependencies.append(dependency)
            seen.add(key)
    return dependencies


def marketplace_record(skill: Skill) -> dict[str, Any] | None:
    """Read local marketplace provenance without treating it as instructions."""
    path = skill.root / ".ares-marketplace.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _safe_archive_path(value: str) -> PurePosixPath:
    normalized = PurePosixPath(str(value).replace("\\", "/"))
    if normalized.is_absolute() or not normalized.parts or any(part in {"", ".", ".."} for part in normalized.parts):
        raise SkillValidationError("Archive paths must stay inside the skill directory.")
    if any(":" in part for part in normalized.parts):
        raise SkillValidationError("Archive paths cannot contain drive-qualified names.")
    return normalized


def _validate_skill_file(relative: str, content: bytes, *, executable: bool) -> None:
    path = PurePosixPath(relative)
    name = path.name.casefold()
    if executable:
        raise SkillValidationError("Skill files must not be executable.")
    if name != "skill.md" and path.suffix.casefold() not in _SAFE_SKILL_FILE_SUFFIXES and name not in _SAFE_SKILL_FILE_NAMES:
        raise SkillValidationError(f"Unsupported skill file '{relative}'. Skills may contain instructions and data only.")
    if b"\x00" in content:
        raise SkillValidationError(f"Skill file '{relative}' appears to be binary.")
    try:
        content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SkillValidationError(f"Skill file '{relative}' must be UTF-8 text.") from exc


def _safe_category(value: str) -> str:
    category = str(value or "general").strip().lower().replace("_", "-").replace(" ", "-")
    if not _CATEGORY_RE.match(category):
        return "general"
    return category


def _owner_name(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("handle") or value.get("name") or value.get("username") or "")
    return str(value or "")


def _public_count(*values: Any, names: tuple[str, ...]) -> int | None:
    """Read an optional public popularity field without inventing one.

    Registries use several shapes and many do not publish popularity at all.
    A missing value remains ``None`` so callers can say that clearly instead
    of relabelling search relevance as a star count.
    """
    for value in values:
        if not isinstance(value, dict):
            continue
        containers = (value, value.get("stats"), value.get("metrics"), value.get("metadata"))
        for container in containers:
            if not isinstance(container, dict):
                continue
            for name in names:
                raw = container.get(name)
                if isinstance(raw, bool):
                    continue
                try:
                    count = int(raw)
                except (TypeError, ValueError):
                    continue
                if count >= 0:
                    return count
    return None


def _nested(value: dict[str, Any], *keys: str) -> Any:
    current: Any = value
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _file_names(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    names = []
    for item in value:
        if isinstance(item, str):
            names.append(item)
        elif isinstance(item, dict) and item.get("path"):
            names.append(str(item["path"]))
    return names


def _split_skill_reference(value: str) -> tuple[str, str]:
    """Split an optional ``@publisher/slug`` without guessing a publisher."""
    normalized = str(value or "").strip()
    if normalized.startswith("@") and "/" in normalized:
        owner, slug = normalized[1:].split("/", 1)
        if owner.strip() and slug.strip():
            return slug.strip(), owner.strip()
    return normalized, ""


def _canonical_skill_url(registry: SkillRegistry, owner: str, slug: str, payload: dict[str, Any]) -> str:
    explicit = payload.get("url") or payload.get("canonicalUrl")
    if isinstance(explicit, str) and explicit.startswith("https://"):
        return explicit
    if "clawhub.ai" in registry.api_base and owner:
        return f"https://clawhub.ai/{owner}/skills/{slug}"
    return ""
