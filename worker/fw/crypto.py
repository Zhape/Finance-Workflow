"""Encryption for stored OAuth tokens.

Every refresh token in `connections` is another company's standing access to
their accounting system, so it never sits in the database in plaintext.

This is envelope-shaped but not yet envelope-encrypted: one key from the
environment, applied per row. That is the right shape for a spike and the
wrong shape for production, where the key belongs in a KMS with rotation and
per-org data keys. `key_id` is stored alongside every ciphertext so rotation
is a migration rather than a redesign.
"""

from __future__ import annotations

import os

from cryptography.fernet import Fernet, InvalidToken


class EncryptionError(RuntimeError):
    pass


class Cipher:
    def __init__(self, key: str | None = None, key_id: str = "env-1"):
        raw = key or os.environ.get("FW_ENCRYPTION_KEY", "")
        if not raw:
            raise EncryptionError(
                "FW_ENCRYPTION_KEY is not set. Generate one with:\n"
                "  python -c \"from cryptography.fernet import Fernet; "
                "print(Fernet.generate_key().decode())\""
            )
        try:
            self._fernet = Fernet(raw.encode() if isinstance(raw, str) else raw)
        except (ValueError, TypeError) as exc:
            raise EncryptionError(f"FW_ENCRYPTION_KEY is not a valid Fernet key: {exc}")
        self.key_id = key_id

    def encrypt(self, plaintext: str) -> bytes:
        return self._fernet.encrypt(plaintext.encode("utf-8"))

    def decrypt(self, ciphertext: bytes) -> str:
        try:
            return self._fernet.decrypt(bytes(ciphertext)).decode("utf-8")
        except InvalidToken:
            raise EncryptionError(
                "Stored credential could not be decrypted — FW_ENCRYPTION_KEY "
                "does not match the key it was written with."
            ) from None


def generate_key() -> str:
    return Fernet.generate_key().decode()
