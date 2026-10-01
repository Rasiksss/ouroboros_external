"""Optional AES-256-GCM field encryption for runtime DB message columns."""

from __future__ import annotations

import base64
import logging
import secrets
from functools import lru_cache

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

_ENC_PREFIX = "enc:v1:"
_NONCE_SIZE = 12
_KEY_SIZE = 32

log = logging.getLogger(__name__)


def _decode_key(raw: str) -> bytes | None:
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        key = base64.b64decode(text, validate=True)
    except Exception:
        return None
    if len(key) != _KEY_SIZE:
        return None
    return key


@lru_cache(maxsize=1)
def _encryption_config() -> tuple[bool, bytes | None]:
    from ouroboros.storage.settings.store import load_settings

    settings = load_settings()
    enabled = bool(getattr(settings, "OUROBOROS_CHAT_ENCRYPTION_ENABLED", False))
    key = _decode_key(settings.OUROBOROS_CHAT_ENCRYPTION_KEY.get_secret_value()) if enabled else None
    if enabled and key is None:
        from ouroboros.storage.settings.profile import is_production

        msg = "OUROBOROS_CHAT_ENCRYPTION_ENABLED requires a valid 32-byte base64 OUROBOROS_CHAT_ENCRYPTION_KEY"
        if is_production():
            raise ValueError(msg)
        log.warning("%s; chat encryption disabled", msg)
        enabled = False
    return enabled, key


def reset_encryption_cache_for_tests() -> None:
    _encryption_config.cache_clear()


def encryption_active_for_dialect(dialect_name: str) -> bool:
    if dialect_name != "postgresql":
        return False
    enabled, key = _encryption_config()
    return enabled and key is not None


def encrypt_field(plaintext: str, *, dialect_name: str) -> str:
    text = str(plaintext or "")
    if not text or not encryption_active_for_dialect(dialect_name):
        return text
    _, key = _encryption_config()
    assert key is not None
    nonce = secrets.token_bytes(_NONCE_SIZE)
    ciphertext = AESGCM(key).encrypt(nonce, text.encode("utf-8"), None)
    blob = base64.urlsafe_b64encode(nonce + ciphertext).decode("ascii")
    return f"{_ENC_PREFIX}{blob}"


def decrypt_field(stored: str, *, dialect_name: str) -> str:
    text = str(stored or "")
    if not text.startswith(_ENC_PREFIX):
        return text
    _, key = _encryption_config()
    if key is None:
        raise ValueError("encrypted message field but chat encryption key is not configured")
    blob = base64.urlsafe_b64decode(text[len(_ENC_PREFIX) :].encode("ascii"))
    if len(blob) < _NONCE_SIZE + 16:
        raise ValueError("invalid encrypted message field")
    nonce = blob[:_NONCE_SIZE]
    ciphertext = blob[_NONCE_SIZE:]
    return AESGCM(key).decrypt(nonce, ciphertext, None).decode("utf-8")


__all__ = [
    "decrypt_field",
    "encrypt_field",
    "encryption_active_for_dialect",
    "reset_encryption_cache_for_tests",
]
