"""Safe orchestration for opt-in advanced Ares exports.

The legacy :mod:`ares.tools.exporter` API deliberately returns a path and
writes ordinary JSON.  This module sits *above* that stable API: it turns an
already-built payload into a non-mutating preview plan, then writes only when
``write_advanced_export`` is explicitly called.  The plan keeps payload data
private so a tool/UI can safely return its public projection without leaking
the export contents (or an encryption password) into chat history or logs.

It composes the focused helpers rather than duplicating their policy:

* ``filter_export_payload`` for section/date selection;
* ``plan_export`` for redaction, checksums, and incremental deltas;
* ``write_export_payload`` for atomic JSON writes; and
* ``export_security`` for password-protected AES-GCM envelopes.
"""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from collections.abc import Mapping, Sequence

from ares.tools.export_security import (
    EncryptedExportError,
    decrypt_export_payload,
    encrypt_export_payload,
    inspect_encrypted_export,
)
from ares.tools.exporter import write_export_payload
from ares.tools.media_export_upgrades import (
    UpgradeValidationError,
    build_export_manifest,
    checksum_payload,
    filter_export_payload,
    plan_export,
    verify_export_file,
    verify_export_manifest,
)


class AdvancedExportError(ValueError):
    """Raised for invalid advanced export plans or writes.

    Errors deliberately describe configuration/state only.  They never echo
    a password or export payload value.
    """


def _json_clone(value: Any) -> Any:
    """Detach JSON data while proving it is safe to write as JSON."""
    try:
        return json.loads(
            json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)
        )
    except (TypeError, ValueError) as exc:
        raise AdvancedExportError("The export payload must be JSON-serializable.") from exc


def _json_byte_count(value: Any) -> int:
    try:
        return len(
            json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        )
    except (TypeError, ValueError) as exc:
        raise AdvancedExportError("The export payload must be JSON-serializable.") from exc


def _normalise_path(value: str | Path | None, field_name: str) -> Path | None:
    if value is None:
        return None
    if isinstance(value, str) and not value.strip():
        raise AdvancedExportError(f"{field_name} cannot be blank.")
    try:
        path = Path(value).expanduser()
    except TypeError as exc:
        raise AdvancedExportError(f"{field_name} must be a filesystem path.") from exc
    if not path.name or path.name == ".":
        raise AdvancedExportError(f"{field_name} must name a file.")
    return path


def _same_path(first: Path | None, second: Path | None) -> bool:
    if first is None or second is None:
        return False
    try:
        return first.resolve(strict=False) == second.resolve(strict=False)
    except OSError:
        return str(first) == str(second)


def _load_manifest(value: Mapping[str, Any] | str | Path | None) -> Mapping[str, Any] | None:
    if value is None:
        return None
    if isinstance(value, Mapping):
        clone = _json_clone(dict(value))
        assert isinstance(clone, dict)
        return clone
    path = _normalise_path(value, "previous_manifest")
    assert path is not None
    try:
        decoded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AdvancedExportError("Unable to read the previous export manifest.") from exc
    if not isinstance(decoded, Mapping):
        raise AdvancedExportError("The previous export manifest must be a JSON object.")
    clone = _json_clone(dict(decoded))
    assert isinstance(clone, dict)
    return clone


def _validate_password_reference(password: str | bytes | bytearray | None) -> bool:
    """Validate only shape/presence, never retain or reveal a password."""
    if password is None:
        return False
    if not isinstance(password, (str, bytes, bytearray)):
        raise AdvancedExportError("The encryption password must be text or bytes.")
    if not password:
        raise AdvancedExportError("An encryption password cannot be empty.")
    if len(password) > 1024:
        raise AdvancedExportError("The encryption password is too long.")
    return True


def _manifest_for_written_payload(
    *,
    export_plan: Mapping[str, Any],
    profile: str,
) -> dict[str, Any]:
    """Build the manifest that matches exactly what will be persisted.

    A non-incremental export writes the fully redacted payload, so its normal
    manifest already verifies the written file and its redaction paths.  An
    incremental export writes a small delta wrapper; it needs a separate
    plaintext manifest for that wrapper while the full manifest remains in
    the preview as the provenance/base comparison artifact.
    """
    full_manifest = export_plan["manifest"]
    if not export_plan["manifest"]["incremental"]["enabled"]:
        assert isinstance(full_manifest, dict)
        return _json_clone(full_manifest)
    write_payload = export_plan["write_payload"]
    assert isinstance(write_payload, Mapping)
    return build_export_manifest(
        write_payload,
        profile=profile,
        redactions=[],
        incremental=False,
    )


def _manifest_summary(manifest: Mapping[str, Any]) -> dict[str, Any]:
    """Project safe manifest metadata for plans/results without payload data."""
    incremental = manifest.get("incremental")
    return {
        "kind": str(manifest.get("kind") or "ares_export_manifest"),
        "schema_version": manifest.get("schema_version"),
        "profile": manifest.get("profile"),
        "checksum_sha256": manifest.get("checksum_sha256"),
        "section_counts": _json_clone(manifest.get("section_counts") or {}),
        "section_checksums": _json_clone(manifest.get("section_checksums") or {}),
        "redaction_count": len(manifest.get("redactions") or []),
        "incremental": _json_clone(incremental if isinstance(incremental, Mapping) else {}),
    }


@dataclass(frozen=True)
class AdvancedExportPlan:
    """A previewable plan whose plaintext payloads are intentionally private.

    ``as_dict`` is the only public projection intended for tool/UI responses.
    The plan does not retain an encryption password; callers supply it again
    at write time, which prevents a rendered/serialized plan from carrying a
    credential.
    """

    preview: dict[str, Any]
    _write_payload: dict[str, Any] = field(repr=False, compare=False)
    _full_payload: dict[str, Any] = field(repr=False, compare=False)
    _write_manifest: dict[str, Any] = field(repr=False, compare=False)
    _full_manifest: dict[str, Any] = field(repr=False, compare=False)
    _output_path: Path | None = field(repr=False, compare=False)
    _manifest_path: Path | None = field(repr=False, compare=False)
    _encrypted: bool = field(repr=False, compare=False)

    @property
    def output_path(self) -> Path | None:
        """The planned output path, if one was supplied."""
        return self._output_path

    @property
    def manifest_path(self) -> Path | None:
        """The planned plaintext sidecar-manifest path, if supplied."""
        return self._manifest_path

    @property
    def encrypted(self) -> bool:
        """Whether the plan requires encryption at write time."""
        return self._encrypted

    def as_dict(self) -> dict[str, Any]:
        """Return a detached, safe-to-render plan projection."""
        cloned = _json_clone(self.preview)
        assert isinstance(cloned, dict)
        return cloned


def plan_advanced_export(
    payload: Mapping[str, Any],
    *,
    profile: str = "full",
    redact: bool = True,
    include_categories: str | Sequence[str] | None = None,
    exclude_categories: str | Sequence[str] | None = None,
    since: str | None = None,
    until: str | None = None,
    previous_manifest: Mapping[str, Any] | str | Path | None = None,
    incremental: bool = False,
    output_path: str | Path | None = None,
    manifest_path: str | Path | None = None,
    encryption_password: str | bytes | bytearray | None = None,
) -> AdvancedExportPlan:
    """Create a side-effect-free advanced export plan.

    This does not create any file.  Its public projection contains selection,
    redaction, checksum, and output metadata but never a plaintext payload or
    password.  Use :func:`write_advanced_export` to carry out the explicit
    write, providing the encryption password again if encryption is enabled.
    """
    if not isinstance(payload, Mapping):
        raise AdvancedExportError("The export payload must be a JSON object.")
    if type(redact) is not bool:
        raise AdvancedExportError("redact must be a boolean.")
    if type(incremental) is not bool:
        raise AdvancedExportError("incremental must be a boolean.")

    output = _normalise_path(output_path, "output_path")
    manifest_output = _normalise_path(manifest_path, "manifest_path")
    if _same_path(output, manifest_output):
        raise AdvancedExportError("output_path and manifest_path must be different files.")
    encrypted = _validate_password_reference(encryption_password)
    prior_manifest = _load_manifest(previous_manifest)

    try:
        filtered = filter_export_payload(
            payload,
            include_categories=include_categories,
            exclude_categories=exclude_categories,
            since=since,
            until=until,
        )
        selected_payload = filtered["payload"]
        export_plan = plan_export(
            selected_payload,
            profile=profile,
            redact=redact,
            previous_manifest=prior_manifest,
            incremental=incremental,
            output_path=output,
        )
        full_payload = export_plan["full_payload"]
        write_payload = export_plan["write_payload"]
        full_manifest = export_plan["manifest"]
        if not all(isinstance(value, Mapping) for value in (full_payload, write_payload, full_manifest)):
            raise AdvancedExportError("The export helpers returned an invalid plan.")
        full_verification = verify_export_manifest(full_payload, full_manifest)
        if not full_verification["ok"]:
            raise AdvancedExportError("The planned export failed its manifest verification.")
        write_manifest = _manifest_for_written_payload(
            export_plan=export_plan,
            profile=str(export_plan["profile"]),
        )
        write_verification = verify_export_manifest(write_payload, write_manifest)
        if not write_verification["ok"]:
            raise AdvancedExportError("The planned written export failed its manifest verification.")
    except UpgradeValidationError as exc:
        raise AdvancedExportError(str(exc)) from exc

    warnings = [*filtered.get("warnings", []), *export_plan.get("warnings", [])]
    profile_value = str(export_plan["profile"])
    preview = {
        "kind": "advanced_export_plan",
        "schema_version": 1,
        "profile": profile_value,
        "preview_only": True,
        "output_path": str(output) if output is not None else None,
        "manifest_path": str(manifest_output) if manifest_output is not None else None,
        "filters": _json_clone(filtered["filters"]),
        "section_stats": _json_clone(filtered["section_stats"]),
        "redaction": {
            "enabled": redact,
            "count": len(full_manifest.get("redactions") or []),
        },
        "incremental": _json_clone(full_manifest.get("incremental") or {}),
        "encryption": {
            "enabled": encrypted,
            "algorithm": "AES-256-GCM" if encrypted else None,
            "kdf": "scrypt" if encrypted else None,
        },
        "full_manifest": _manifest_summary(full_manifest),
        "write_manifest": _manifest_summary(write_manifest),
        "write": {
            "payload_checksum_sha256": checksum_payload(write_payload),
            "payload_bytes": _json_byte_count(write_payload),
            "encrypted": encrypted,
        },
        "verification": {
            "full_manifest": bool(full_verification["ok"]),
            "write_manifest": bool(write_verification["ok"]),
        },
        "warnings": [str(item) for item in warnings],
    }
    return AdvancedExportPlan(
        preview=preview,
        _write_payload=_json_clone(dict(write_payload)),
        _full_payload=_json_clone(dict(full_payload)),
        _write_manifest=_json_clone(write_manifest),
        _full_manifest=_json_clone(dict(full_manifest)),
        _output_path=output,
        _manifest_path=manifest_output,
        _encrypted=encrypted,
    )


def write_advanced_export(
    plan: AdvancedExportPlan,
    *,
    encryption_password: str | bytes | bytearray | None = None,
) -> dict[str, Any]:
    """Atomically write an already-previewed advanced export plan.

    For encrypted exports the on-disk JSON is an AES-GCM envelope.  The
    plaintext is decrypted once in memory and verified against the manifest
    before returning.  Neither the decrypted payload nor password is included
    in the result.
    """
    if not isinstance(plan, AdvancedExportPlan):
        raise AdvancedExportError("write_advanced_export requires an AdvancedExportPlan.")
    if plan.output_path is None:
        raise AdvancedExportError("An output_path is required before an export can be written.")
    supplied_password = _validate_password_reference(encryption_password)
    if plan.encrypted and not supplied_password:
        raise AdvancedExportError("An encryption password is required to write this export.")
    if not plan.encrypted and supplied_password:
        raise AdvancedExportError(
            "This plan is not encrypted; create a new plan before adding encryption."
        )

    write_payload = _json_clone(plan._write_payload)
    assert isinstance(write_payload, dict)
    write_manifest = _json_clone(plan._write_manifest)
    assert isinstance(write_manifest, dict)
    full_manifest = _json_clone(plan._full_manifest)
    assert isinstance(full_manifest, dict)

    encrypted_metadata: dict[str, Any] | None = None
    round_trip_ok: bool | None = None
    try:
        if plan.encrypted:
            # The password is intentionally scoped to these crypto calls and
            # never copied into the plan, result, file manifest, or an error.
            envelope = encrypt_export_payload(write_payload, encryption_password)  # type: ignore[arg-type]
            decrypted = decrypt_export_payload(envelope, encryption_password)  # type: ignore[arg-type]
            round_trip_ok = checksum_payload(decrypted) == checksum_payload(write_payload)
            if not round_trip_ok:
                raise AdvancedExportError("Encrypted export round-trip verification failed.")
            manifest_verification = verify_export_manifest(decrypted, write_manifest)
            if not manifest_verification["ok"]:
                raise AdvancedExportError("Encrypted export manifest verification failed.")
            persisted_payload = envelope
            encrypted_metadata = inspect_encrypted_export(envelope)
        else:
            manifest_verification = verify_export_manifest(write_payload, write_manifest)
            if not manifest_verification["ok"]:
                raise AdvancedExportError("Export manifest verification failed.")
            persisted_payload = write_payload

        output = write_export_payload(plan.output_path, persisted_payload)
        if plan.manifest_path is not None:
            manifest_output = write_export_payload(plan.manifest_path, write_manifest)
        else:
            manifest_output = None

        if plan.encrypted:
            # We verified the exact envelope plaintext immediately before the
            # atomic write.  Re-inspect the written JSON without decrypting it
            # so corrupt/truncated output cannot be reported as successful.
            written_envelope = json.loads(output.read_text(encoding="utf-8"))
            if not isinstance(written_envelope, Mapping):
                raise AdvancedExportError("The encrypted export file is not a JSON object.")
            written_metadata = inspect_encrypted_export(written_envelope)
            if written_metadata != encrypted_metadata:
                raise AdvancedExportError("The written encrypted export does not match the verified envelope.")
            file_verification: dict[str, Any] = {
                "ok": True,
                "encrypted_envelope": True,
                "manifest": manifest_verification,
            }
        else:
            file_verification = verify_export_file(output, write_manifest)
            if not file_verification["ok"]:
                raise AdvancedExportError("The written export failed file verification.")
    except (EncryptedExportError, UpgradeValidationError) as exc:
        raise AdvancedExportError(str(exc)) from exc

    artifacts = [
        {
            "kind": "encrypted_export" if plan.encrypted else "export",
            "path": str(output),
            "checksum_sha256": checksum_payload(persisted_payload),
        }
    ]
    if manifest_output is not None:
        artifacts.append(
            {
                "kind": "export_manifest",
                "path": str(manifest_output),
                "checksum_sha256": checksum_payload(write_manifest),
            }
        )
    return {
        "kind": "advanced_export_result",
        "schema_version": 1,
        "status": "completed",
        "output_path": str(output),
        "manifest_path": str(manifest_output) if manifest_output is not None else None,
        "encrypted": plan.encrypted,
        "encryption": encrypted_metadata
        if encrypted_metadata is not None
        else {"enabled": False, "algorithm": None, "kdf": None},
        "full_manifest": _manifest_summary(full_manifest),
        "write_manifest": _manifest_summary(write_manifest),
        "verification": {
            "round_trip": round_trip_ok,
            "file": file_verification,
        },
        "artifacts": artifacts,
        "warnings": deepcopy(plan.preview.get("warnings") or []),
    }


# A concise alias makes the intended plan-then-write workflow discoverable to
# integrations without introducing a second implementation.
prepare_advanced_export = plan_advanced_export


__all__ = [
    "AdvancedExportError",
    "AdvancedExportPlan",
    "plan_advanced_export",
    "prepare_advanced_export",
    "write_advanced_export",
]
