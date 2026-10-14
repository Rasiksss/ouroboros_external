from __future__ import annotations

import base64
import hashlib
import hmac
import secrets

from cryptography.hazmat.primitives.ciphers.aead import AESSIV
from pydantic import SecretStr

_KEY_SIZE = 64
_NONCE_SIZE = 16


class CredentialsKey:
    def __init__(self, encryption_key: SecretStr) -> None:
        key = self._decoded(encryption_key.get_secret_value())
        if key is None:
            raise ValueError("OUROBOROS_CREDENTIALS_ENCRYPTION_KEY is not 64 bytes of base64")
        self._key = key
        self._siv = AESSIV(key)

    def seal(self, plaintext: str, *, associated_data: bytes) -> str:
        nonce = secrets.token_bytes(_NONCE_SIZE)
        sealed = self._siv.encrypt(plaintext.encode("utf-8"), [associated_data, nonce])
        return base64.urlsafe_b64encode(nonce + sealed).decode("ascii")

    def unseal(self, sealed: str, *, associated_data: bytes) -> str:
        blob = base64.urlsafe_b64decode(sealed.encode("ascii"))
        nonce = blob[:_NONCE_SIZE]
        return self._siv.decrypt(blob[_NONCE_SIZE:], [associated_data, nonce]).decode("utf-8")

    def derive(self, label: bytes) -> bytes:
        return hmac.new(self._key, label, hashlib.sha256).digest()

    @classmethod
    def _decoded(cls, raw: str) -> bytes | None:
        text = "".join(raw.split())
        if not text:
            return None
        try:
            key = base64.b64decode(text, validate=True)
        except ValueError:
            return None
        return key if len(key) == _KEY_SIZE else None


__all__ = ["CredentialsKey"]
