"""Optional AES-256-GCM field encryption for runtime DB message columns."""

from __future__ import annotations

import base64
import logging
import secrets
from functools import lru_cache

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

_ENC_PREFIX = "enc:v1:"
_NONCE_SIZE = 12
_TAG_SIZE = 16
_KEY_SIZE = 32

log = logging.getLogger(__name__)


class FieldKeyUnavailable(ValueError):
    """This process has no key for an encrypted field; the field itself may be perfectly good."""


def _decode_encryption_key(raw: str) -> bytes | None:
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
    key = _decode_encryption_key(settings.OUROBOROS_CHAT_ENCRYPTION_KEY.get_secret_value()) if enabled else None
    if enabled and key is None:
        from ouroboros.storage.settings.profile import is_production

        msg = "OUROBOROS_CHAT_ENCRYPTION_ENABLED requires a valid 32-byte base64 OUROBOROS_CHAT_ENCRYPTION_KEY"
        if is_production():
            # The key is this process's, not the row's: a reader that mistook it for corruption
            # would park every encrypted row it touched, fleet-wide and durably.
            raise FieldKeyUnavailable(msg)
        log.warning("%s; chat encryption disabled", msg)
        enabled = False
    return enabled, key


def reset_encryption_cache_for_tests() -> None:
    _encryption_config.cache_clear()


def encryption_active() -> bool:
    enabled, key = _encryption_config()
    return enabled and key is not None


def validate_chat_encryption() -> None:
    _encryption_config()


def _seal(plaintext: str, key: bytes) -> str:
    nonce = secrets.token_bytes(_NONCE_SIZE)
    ciphertext = AESGCM(key).encrypt(nonce, plaintext.encode("utf-8"), None)
    return base64.urlsafe_b64encode(nonce + ciphertext).decode("ascii")


def _unseal(sealed: str, key: bytes) -> str:
    blob = base64.urlsafe_b64decode(sealed.encode("ascii"))
    if len(blob) < _NONCE_SIZE + _TAG_SIZE:
        raise ValueError("invalid encrypted field")
    return AESGCM(key).decrypt(blob[:_NONCE_SIZE], blob[_NONCE_SIZE:], None).decode("utf-8")


def encrypt_field(plaintext: str) -> str:
    text = str(plaintext or "")
    if not text or not encryption_active():
        return text
    _, key = _encryption_config()
    assert key is not None
    return f"{_ENC_PREFIX}{_seal(text, key)}"


def decrypt_field(stored: str, *, dialect_name: str = "") -> str:
    text = str(stored or "")
    if not text.startswith(_ENC_PREFIX):
        return text
    _, key = _encryption_config()
    if key is None:
        raise FieldKeyUnavailable("encrypted message field but chat encryption key is not configured")
    return _unseal(text[len(_ENC_PREFIX) :], key)


__all__ = [
    "FieldKeyUnavailable",
    "decrypt_field",
    "encrypt_field",
    "encryption_active",
    "reset_encryption_cache_for_tests",
    "validate_chat_encryption",
]
