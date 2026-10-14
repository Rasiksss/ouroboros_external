from __future__ import annotations

import base64
import hashlib
import hmac
import json

import pytest
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESSIV
from pydantic import SecretStr

from infra.credentials_key import CredentialsKey
from tests.helpers.parametrize import parametrize_from_dict
from tests.helpers.security_context import sealed_under_the_chat_key

RAW_KEY = b"k" * 64
KEY = SecretStr(base64.b64encode(RAW_KEY).decode("ascii"))
OTHER_KEY = SecretStr(base64.b64encode(b"q" * 64).decode("ascii"))
SECRET = "Bearer s3cr3t-value"
AAD = json.dumps(["security-token", "header", "authorization"]).encode("utf-8")


def test_a_sealed_value_opens_with_its_key_and_associated_data() -> None:
    key = CredentialsKey(KEY)

    assert key.unseal(key.seal(SECRET, associated_data=AAD), associated_data=AAD) == SECRET


def test_two_seals_of_one_value_differ() -> None:
    key = CredentialsKey(KEY)

    assert key.seal(SECRET, associated_data=AAD) != key.seal(SECRET, associated_data=AAD)


def test_a_sealed_value_is_aes_256_siv_with_its_nonce_in_front() -> None:
    blob = base64.urlsafe_b64decode(CredentialsKey(KEY).seal(SECRET, associated_data=AAD))

    assert AESSIV(RAW_KEY).decrypt(blob[16:], [AAD, blob[:16]]) == SECRET.encode("utf-8")


@parametrize_from_dict(
    {
        "another key": {"key": OTHER_KEY, "associated_data": AAD},
        "another associated data": {
            "key": KEY,
            "associated_data": json.dumps(["security-token", "cookie", "authorization"]).encode("utf-8"),
        },
    }
)
def test_a_value_opens_only_under_its_key_and_associated_data(key, associated_data) -> None:
    sealed = CredentialsKey(KEY).seal(SECRET, associated_data=AAD)

    with pytest.raises(InvalidTag):
        CredentialsKey(key).unseal(sealed, associated_data=associated_data)


def test_a_value_sealed_under_a_chat_encryption_key_does_not_open() -> None:
    sealed = sealed_under_the_chat_key(SECRET, associated_data=AAD)

    with pytest.raises(InvalidTag):
        CredentialsKey(KEY).unseal(sealed, associated_data=AAD)


@parametrize_from_dict(
    {
        "empty": {"raw": ""},
        "not base64": {"raw": "not-base64-at-all"},
        "16 bytes": {"raw": base64.b64encode(b"k" * 16).decode("ascii")},
        "32 bytes, the size of a chat encryption key": {"raw": base64.b64encode(b"k" * 32).decode("ascii")},
        "48 bytes": {"raw": base64.b64encode(b"k" * 48).decode("ascii")},
        "80 bytes": {"raw": base64.b64encode(b"k" * 80).decode("ascii")},
    }
)
def test_a_key_that_is_not_64_base64_bytes_is_refused_without_its_value(raw) -> None:
    with pytest.raises(ValueError, match="OUROBOROS_CREDENTIALS_ENCRYPTION_KEY") as refused:
        CredentialsKey(SecretStr(raw))

    if raw:
        assert raw not in str(refused.value)
    assert refused.value.__cause__ is None
    assert refused.value.__context__ is None


def test_a_key_broken_over_lines_is_the_key_it_encodes() -> None:
    encoded = KEY.get_secret_value()
    wrapped = CredentialsKey(SecretStr(f"{encoded[:64]}\n{encoded[64:]}\n"))

    assert CredentialsKey(KEY).unseal(wrapped.seal(SECRET, associated_data=AAD), associated_data=AAD) == SECRET


def test_a_derived_key_is_the_hmac_sha256_of_its_label() -> None:
    label = b"ouroboros:a2a-task-handle:v1"

    assert CredentialsKey(KEY).derive(label) == hmac.new(RAW_KEY, label, hashlib.sha256).digest()
