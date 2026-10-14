from __future__ import annotations

import asyncio
from types import ModuleType
from typing import Any

import httpx
import httpx2

from ouroboros.egress.auth.client_guard import CredentialedClientGuard, RequestOrigin
from tests.helpers.parametrize import parametrize_from_dict

CONFIGURED = "https://agent.test:8443/rpc"
NAMES = frozenset({"X-Api-Key", "Cookie"})
CREDENTIALS = {"X-Api-Key": "key-secret-17", "Cookie": "sudir=cookie-secret-42"}

stacks = {"httpx": {"stack": httpx}, "httpx2": {"stack": httpx2}}

off_origin_cases = {
    "scheme": {"url": "http://agent.test:8443/rpc"},
    "host": {"url": "https://other.test:8443/rpc"},
    "port": {"url": "https://agent.test:9443/rpc"},
    "default port under a mixed-case scheme": {"url": "HTTPS://agent.test:443/rpc"},
}

same_origin_cases = {
    "hostname case": {"configured": CONFIGURED, "url": "https://AGENT.test:8443/another/path?page=2"},
    "mixed-case scheme with the default port": {
        "configured": "https://agent.test/rpc",
        "url": "HTTPS://agent.test:443/x",
    },
    "mixed-case scheme with a custom port": {"configured": CONFIGURED, "url": "HtTpS://agent.test:8443/x"},
    "ipv6 spelling": {"configured": "https://[2001:DB8::1]:8443/rpc", "url": "https://[2001:db8:0:0::1]:8443/x"},
    "ipv6 loopback": {"configured": "http://[::1]:8080/mcp", "url": "http://[0:0:0:0:0:0:0:1]:8080/mcp/"},
}


def _sent_headers(stack: ModuleType, url: str, *, configured: str) -> Any:
    sent: list[Any] = []

    def handler(request: Any) -> Any:
        sent.append(request)
        return stack.Response(200)

    async def send() -> None:
        async with stack.AsyncClient(transport=stack.MockTransport(handler)) as client:
            CredentialedClientGuard.strip_off_origin(client, RequestOrigin.of(stack.URL(configured)), NAMES)
            await client.get(url, headers={**CREDENTIALS, "X-Other": "kept"})

    asyncio.run(send())
    (request,) = sent
    return request.headers


@parametrize_from_dict(stacks)
def test_a_guarded_client_keeps_no_cookies(stack) -> None:
    sent: list[Any] = []

    def handler(request: Any) -> Any:
        sent.append(request)
        return stack.Response(200, headers={"Set-Cookie": "session=owner-a-session; Path=/"})

    async def send_twice() -> int:
        async with stack.AsyncClient(transport=stack.MockTransport(handler)) as client:
            CredentialedClientGuard.keep_no_cookies(client)
            await client.get(CONFIGURED)
            await client.get(CONFIGURED)
            return len(client.cookies.jar)

    assert asyncio.run(send_twice()) == 0
    assert [request.headers.get("cookie") for request in sent] == [None, None]


@parametrize_from_dict(stacks)
@parametrize_from_dict(off_origin_cases)
def test_names_are_stripped_from_a_request_to_another_origin(url, stack) -> None:
    headers = _sent_headers(stack, url, configured=CONFIGURED)

    assert headers.get("x-api-key") is None
    assert headers.get("cookie") is None
    assert headers["x-other"] == "kept"


@parametrize_from_dict(stacks)
@parametrize_from_dict(same_origin_cases)
def test_a_same_origin_request_keeps_its_names(configured, url, stack) -> None:
    headers = _sent_headers(stack, url, configured=configured)

    assert RequestOrigin.of(stack.URL(url)) == RequestOrigin.of(stack.URL(configured))
    assert headers["x-api-key"] == CREDENTIALS["X-Api-Key"]
    assert headers["cookie"] == CREDENTIALS["Cookie"]


@parametrize_from_dict(stacks)
def test_a_default_port_is_the_same_origin(stack) -> None:
    headers = _sent_headers(stack, "https://agent.test:443/rpc", configured="https://agent.test/rpc")

    assert RequestOrigin.of(stack.URL("https://agent.test:443/")) == RequestOrigin.of(stack.URL("https://agent.test"))
    assert headers["x-api-key"] == CREDENTIALS["X-Api-Key"]
    assert headers["cookie"] == CREDENTIALS["Cookie"]
