"""Password-protected JSON export envelopes.

This module is deliberately independent from the legacy exporter.  Callers
must opt in by encrypting an already-prepared JSON export payload, which keeps
the existing plain JSON export/import contract stable while providing a safe
building block for an advanced encrypted-export flow.

The envelope uses scrypt to derive a unique AES-256-GCM key from a password.
All non-ciphertext envelope metadata is authenticated as additional data, so a
modified KDF configuration, nonce, or format marker cannot be accepted as a
valid export.  Passwords are only used in memory and are never returned,
stored, or included in error messages.
"""

from __future__ import annotations

import base64
import json
import os
from collections.abc import Mapping
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt


class EncryptedExportError(ValueError):
    """Raised when an encrypted export cannot be validated or decrypted."""


_KIND = "ares_encrypted_export"
_SCHEMA_VERSION = 1
_FORMAT = "json"
_ALGORITHM = "AES-256-GCM"
_KDF_NAME = "scrypt"
_KEY_LENGTH = 32
_SALT_LENGTH = 16
_NONCE_LENGTH = 12
_DEFAULT_SCRYPT_N = 2**14
_DEFAULT_SCRYPT_R = 8
_DEFAULT_SCRYPT_P = 1
_MIN_SCRYPT_N = 2**14
_MAX_SCRYPT_N = 2**16
_MAX_SCRYPT_R = 16
_MAX_SCRYPT_P = 4
_AAD_PREFIX = b"ares-export-encryption:v1\x00"


def _canonical_json_bytes(value: Any) -> bytes:
    """Serialize JSON deterministically without accepting NaN or infinities."""
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise EncryptedExportError("Export payload must be JSON-serializable.") from exc


def _password_bytes(password: str | bytes | bytearray) -> bytes:
    if isinstance(password, str):
        try:
            encoded = password.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise EncryptedExportError("The export password is not valid UTF-8 text.") from exc
    elif isinstance(password, (bytes, bytearray)):
        encoded = bytes(password)
    else:
        raise EncryptedExportError("An export password must be text or bytes.")
    if not encoded:
        raise EncryptedExportError("An export password is required.")
    if len(encoded) > 1024:
        raise EncryptedExportError("The export password is too long.")
    return encoded


def _validate_scrypt_parameters(*, n: Any, r: Any, p: Any, length: Any = _KEY_LENGTH) -> tuple[int, int, int]:
    """Validate bounded scrypt inputs before they can consume memory/CPU."""
    # bool is an int subclass but is never a meaningful security parameter;
    # rejecting all non-integers also prevents silently truncating JSON floats.
    if any(type(value) is not int for value in (n, r, p, length)):
        raise EncryptedExportError("Encrypted export KDF parameters are invalid.")
    normalized_n = n
    normalized_r = r
    normalized_p = p
    normalized_length = length
    if (
        normalized_n < _MIN_SCRYPT_N
        or normalized_n > _MAX_SCRYPT_N
        or normalized_n & (normalized_n - 1)
        or normalized_r < 1
        or normalized_r > _MAX_SCRYPT_R
        or normalized_p < 1
        or normalized_p > _MAX_SCRYPT_P
        or normalized_length != _KEY_LENGTH
        # scrypt's memory cost is roughly 128 * N * r bytes.  Keep any
        # envelope supplied by an untrusted caller below a 64 MiB budget.
        or normalized_n * normalized_r > 2**19
    ):
        raise EncryptedExportError("Encrypted export KDF parameters are unsupported.")
    return normalized_n, normalized_r, normalized_p


def _encode(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")


def _decode(value: Any, field: str) -> bytes:
    if not isinstance(value, str) or not value:
        raise EncryptedExportError(f"Encrypted export {field} is missing or invalid.")
    try:
        return base64.b64decode(value.encode("ascii"), validate=True)
    except (UnicodeEncodeError, ValueError) as exc:
        raise EncryptedExportError(f"Encrypted export {field} is not valid base64.") from exc


def _header(
    *,
    salt: str,
    nonce: str,
    n: int,
    r: int,
    p: int,
) -> dict[str, Any]:
    """Return the complete, authenticated non-ciphertext envelope header."""
    return {
        "kind": _KIND,
        "schema_version": _SCHEMA_VERSION,
        "format": _FORMAT,
        "algorithm": _ALGORITHM,
        "kdf": {
            "name": _KDF_NAME,
            "n": n,
            "r": r,
            "p": p,
            "length": _KEY_LENGTH,
            "salt": salt,
        },
        "nonce": nonce,
    }


def _aad(header: Mapping[str, Any]) -> bytes:
    return _AAD_PREFIX + _canonical_json_bytes(header)


def _derive_key(password: bytes, *, salt: bytes, n: int, r: int, p: int) -> bytes:
    return Scrypt(salt=salt, length=_KEY_LENGTH, n=n, r=r, p=p).derive(password)


def _validated_envelope(envelope: Mapping[str, Any]) -> tuple[dict[str, Any], bytes, bytes, bytes, tuple[int, int, int]]:
    """Validate an envelope before deriving a key or attempting decryption."""
    if not isinstance(envelope, Mapping):
        raise EncryptedExportError("Encrypted export must be a JSON object.")
    if envelope.get("kind") != _KIND or envelope.get("schema_version") != _SCHEMA_VERSION:
        raise EncryptedExportError("Unsupported encrypted export format.")
    if envelope.get("format") != _FORMAT or envelope.get("algorithm") != _ALGORITHM:
        raise EncryptedExportError("Unsupported encrypted export algorithm.")
    kdf = envelope.get("kdf")
    if not isinstance(kdf, Mapping) or kdf.get("name") != _KDF_NAME:
        raise EncryptedExportError("Unsupported encrypted export KDF.")
    n, r, p = _validate_scrypt_parameters(
        n=kdf.get("n"),
        r=kdf.get("r"),
        p=kdf.get("p"),
        length=kdf.get("length"),
    )
    salt = _decode(kdf.get("salt"), "salt")
    nonce = _decode(envelope.get("nonce"), "nonce")
    ciphertext = _decode(envelope.get("ciphertext"), "ciphertext")
    if len(salt) != _SALT_LENGTH:
        raise EncryptedExportError("Encrypted export salt has an invalid length.")
    if len(nonce) != _NONCE_LENGTH:
        raise EncryptedExportError("Encrypted export nonce has an invalid length.")
    # AES-GCM adds a 16-byte authentication tag, so anything smaller cannot be
    # a valid ciphertext and should not reach the crypto implementation.
    if len(ciphertext) < 16:
        raise EncryptedExportError("Encrypted export ciphertext is invalid.")
    header = _header(salt=kdf["salt"], nonce=envelope["nonce"], n=n, r=r, p=p)
    return header, salt, nonce, ciphertext, (n, r, p)


def encrypt_export_payload(
    payload: Mapping[str, Any],
    password: str | bytes | bytearray,
    *,
    scrypt_n: int = _DEFAULT_SCRYPT_N,
) -> dict[str, Any]:
    """Encrypt a JSON export mapping into a self-contained AES-GCM envelope.

    ``scrypt_n`` is exposed only to support deliberate, bounded future tuning;
    values below the current security floor or above the resource ceiling are
    rejected.  The returned mapping has no password or plaintext fields.
    """
    if not isinstance(payload, Mapping):
        raise EncryptedExportError("Export payload must be a JSON object.")
    password_bytes = _password_bytes(password)
    n, r, p = _validate_scrypt_parameters(
        n=scrypt_n,
        r=_DEFAULT_SCRYPT_R,
        p=_DEFAULT_SCRYPT_P,
    )
    plaintext = _canonical_json_bytes(dict(payload))
    salt_bytes = os.urandom(_SALT_LENGTH)
    nonce_bytes = os.urandom(_NONCE_LENGTH)
    salt = _encode(salt_bytes)
    nonce = _encode(nonce_bytes)
    header = _header(salt=salt, nonce=nonce, n=n, r=r, p=p)
    key = _derive_key(password_bytes, salt=salt_bytes, n=n, r=r, p=p)
    ciphertext = AESGCM(key).encrypt(nonce_bytes, plaintext, _aad(header))
    return {**header, "ciphertext": _encode(ciphertext)}


def decrypt_export_payload(
    envelope: Mapping[str, Any],
    password: str | bytes | bytearray,
) -> dict[str, Any]:
    """Authenticate and decrypt an export envelope back into its JSON mapping.

    Wrong passwords and modified ciphertext/header data intentionally receive
    the same error.  This prevents the API from becoming a password or
    plaintext validity oracle.
    """
    password_bytes = _password_bytes(password)
    header, salt, nonce, ciphertext, (n, r, p) = _validated_envelope(envelope)
    key = _derive_key(password_bytes, salt=salt, n=n, r=r, p=p)
    try:
        plaintext = AESGCM(key).decrypt(nonce, ciphertext, _aad(header))
    except InvalidTag as exc:
        raise EncryptedExportError(
            "Unable to decrypt export: the password is incorrect or the export was modified."
        ) from exc
    try:
        decoded = json.loads(plaintext.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        # This should be impossible after GCM authentication for payloads
        # produced here, but retain a safe failure mode for malformed inputs.
        raise EncryptedExportError("Encrypted export plaintext is invalid.") from exc
    if not isinstance(decoded, dict):
        raise EncryptedExportError("Encrypted export plaintext must be a JSON object.")
    return decoded


def inspect_encrypted_export(envelope: Mapping[str, Any]) -> dict[str, Any]:
    """Validate and project non-sensitive metadata without decrypting content."""
    header, _salt, _nonce, ciphertext, (n, r, p) = _validated_envelope(envelope)
    return {
        "kind": header["kind"],
        "schema_version": header["schema_version"],
        "format": header["format"],
        "algorithm": header["algorithm"],
        "kdf": {
            "name": _KDF_NAME,
            "n": n,
            "r": r,
            "p": p,
            "length": _KEY_LENGTH,
        },
        "encrypted_bytes": len(ciphertext),
    }
