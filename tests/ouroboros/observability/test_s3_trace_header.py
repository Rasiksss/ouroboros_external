"""Object storage is an outbound HTTP integration, and it carries the operation identifier.

The S3 client is boto3 rather than `httpx`, so the mechanism is botocore's own event system
instead of a request event hook — but the rule it implements is the same one every long-lived
client here follows: read the identifier at send time, never merge it in at construction.

The interception point is `before-send`, which is also where the provider's own handler runs.
A handler that returns `None` lets the emit chain continue, so the test's recorder — registered
after the provider's, because `register` appends — sees the headers the provider stamped and
short-circuits the send by returning a response.
"""

from __future__ import annotations

import pytest

from ouroboros.observability import aef_tracing
from ouroboros.storage.blob.s3_backend import S3StorageProvider
from tests.helpers.shared import TEST_SCOPE

DASHED = "f47ac10b-58cc-4372-a567-0e02b2c3d479"
PACKED = "f47ac10b58cc4372a5670e02b2c3d479"
OTHER = "463ac35c-9f64-13ad-4848-5a3953bb6124"


class _EmptyBody:
    """The `raw` an `AWSResponse` reads its content off, by either route botocore uses."""

    def stream(self, **_kwargs: object):
        yield b""

    def read(self, _amt: int | None = None) -> bytes:
        return b""


@pytest.fixture
def provider_and_headers() -> tuple[S3StorageProvider, list[dict[str, str]]]:
    from botocore.awsrequest import AWSResponse
    from pydantic import SecretStr

    from ouroboros.storage.settings.store import load_settings

    provider = S3StorageProvider(
        load_settings().model_copy(
            update={
                "OUROBOROS_S3_BUCKET": "bucket",
                "OUROBOROS_S3_REGION": "us-east-1",
                "OUROBOROS_S3_ENDPOINT": "http://s3.invalid",
                "OUROBOROS_S3_ACCESS_KEY": SecretStr("key"),
                "OUROBOROS_S3_SECRET_KEY": SecretStr("secret"),
            }
        )
    )
    seen: list[dict[str, str]] = []

    def record(request, **_kwargs):
        seen.append({str(name): str(value) for name, value in request.headers.items()})
        return AWSResponse("http://s3.invalid", 200, {"ETag": '"e"'}, _EmptyBody())

    provider._client.meta.events.register("before-send.s3.*", record)
    return provider, seen


def test_an_s3_write_under_an_operation_carries_the_identifier(provider_and_headers):
    provider, seen = provider_and_headers

    with aef_tracing.trace_scope(DASHED):
        provider.put_bytes(TEST_SCOPE, "uploads", "some/object.bin", b"payload")

    assert [headers.get(aef_tracing.TRACE_HEADER_NAME) for headers in seen] == [DASHED]


def test_an_s3_read_under_an_operation_carries_the_identifier(provider_and_headers):
    provider, seen = provider_and_headers

    with aef_tracing.trace_scope(DASHED):
        provider.get_bytes(TEST_SCOPE, "uploads", "some/object.bin")

    assert [headers.get(aef_tracing.TRACE_HEADER_NAME) for headers in seen] == [DASHED]


def test_two_operations_over_one_client_each_send_their_own_identifier(provider_and_headers):
    """The defect a header merged in at construction would have: the client is built once at
    startup, so every later object read would be attributed to whatever opened it."""
    provider, seen = provider_and_headers

    for trace_id in (DASHED, OTHER):
        with aef_tracing.trace_scope(trace_id):
            provider.put_bytes(TEST_SCOPE, "uploads", "some/object.bin", b"payload")

    assert [headers.get(aef_tracing.TRACE_HEADER_NAME) for headers in seen] == [DASHED, OTHER]


def test_an_s3_call_outside_an_operation_omits_the_header(provider_and_headers):
    provider, seen = provider_and_headers

    provider.put_bytes(TEST_SCOPE, "uploads", "some/object.bin", b"payload")

    assert [headers.get(aef_tracing.TRACE_HEADER_NAME) for headers in seen] == [None]


def test_the_header_is_not_part_of_the_signature(provider_and_headers):
    """Stamped after signing on purpose: a proxy that drops or rewrites an unrecognized header
    would otherwise turn every object operation into `SignatureDoesNotMatch`. Tracing must not
    be able to break storage I/O."""
    provider, seen = provider_and_headers

    with aef_tracing.trace_scope(DASHED):
        provider.put_bytes(TEST_SCOPE, "uploads", "some/object.bin", b"payload")

    assert "x-trace-id" not in seen[0].get("Authorization", "").lower()


def test_a_packed_identifier_reaches_s3_in_the_dashed_form(provider_and_headers):
    """One spelling on the wire, whatever spelling the caller supplied."""
    provider, seen = provider_and_headers

    with aef_tracing.trace_scope(PACKED):
        provider.put_bytes(TEST_SCOPE, "uploads", "some/object.bin", b"payload")

    assert [headers.get(aef_tracing.TRACE_HEADER_NAME) for headers in seen] == [DASHED]
