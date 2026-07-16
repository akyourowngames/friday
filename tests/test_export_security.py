"""Focused tests for opt-in password-protected export envelopes."""

from __future__ import annotations

import base64
from copy import deepcopy

import pytest

from ares.tools.export_security import (
    EncryptedExportError,
    decrypt_export_payload,
    encrypt_export_payload,
    inspect_encrypted_export,
)


def test_encrypted_export_round_trip_and_metadata_shape():
    payload = {
        "version": 5,
        "export_profile": "memories",
        "memories": [{"fact_text": "Krish prefers dark mode", "importance": 0.8}],
    }

    encrypted = encrypt_export_payload(payload, "correct horse battery staple")

    assert set(encrypted) == {
        "kind",
        "schema_version",
        "format",
        "algorithm",
        "kdf",
        "nonce",
        "ciphertext",
    }
    assert encrypted["kind"] == "ares_encrypted_export"
    assert encrypted["algorithm"] == "AES-256-GCM"
    assert encrypted["kdf"]["name"] == "scrypt"
    assert "correct horse" not in repr(encrypted)
    assert "Krish prefers" not in repr(encrypted)
    assert decrypt_export_payload(encrypted, "correct horse battery staple") == payload

    inspection = inspect_encrypted_export(encrypted)
    assert inspection == {
        "kind": "ares_encrypted_export",
        "schema_version": 1,
        "format": "json",
        "algorithm": "AES-256-GCM",
        "kdf": {"name": "scrypt", "n": 2**14, "r": 8, "p": 1, "length": 32},
        "encrypted_bytes": len(base64.b64decode(encrypted["ciphertext"])),
    }


@pytest.mark.parametrize("mutation", ["ciphertext", "nonce", "kdf_salt"])
def test_encrypted_export_rejects_tampering(mutation: str):
    encrypted = encrypt_export_payload({"memories": ["private"]}, "safe password")
    tampered = deepcopy(encrypted)
    if mutation == "ciphertext":
        value = tampered["ciphertext"]
        tampered["ciphertext"] = ("A" if value[0] != "A" else "B") + value[1:]
    elif mutation == "nonce":
        value = tampered["nonce"]
        tampered["nonce"] = ("A" if value[0] != "A" else "B") + value[1:]
    else:
        value = tampered["kdf"]["salt"]
        tampered["kdf"]["salt"] = ("A" if value[0] != "A" else "B") + value[1:]

    with pytest.raises(EncryptedExportError, match="password is incorrect or the export was modified"):
        decrypt_export_payload(tampered, "safe password")


def test_encrypted_export_rejects_wrong_password_and_unsafe_kdf_parameters():
    encrypted = encrypt_export_payload({"memories": []}, "the correct password")

    with pytest.raises(EncryptedExportError, match="password is incorrect or the export was modified"):
        decrypt_export_payload(encrypted, "the wrong password")

    with pytest.raises(EncryptedExportError, match="KDF parameters are unsupported"):
        encrypt_export_payload({"memories": []}, "safe password", scrypt_n=2**12)

    unsafe_header = deepcopy(encrypted)
    unsafe_header["kdf"]["n"] = 2**20
    with pytest.raises(EncryptedExportError, match="KDF parameters are unsupported"):
        decrypt_export_payload(unsafe_header, "the correct password")
