from __future__ import annotations

import ipaddress
from dataclasses import dataclass
from http.cookiejar import DefaultCookiePolicy
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import httpx
    import httpx2

_DEFAULT_PORTS = {"ftp": 21, "http": 80, "https": 443, "ws": 80, "wss": 443}


@dataclass(frozen=True)
class RequestOrigin:
    scheme: str
    host: str
    port: int | None

    @classmethod
    def of(cls, url: httpx.URL | httpx2.URL) -> RequestOrigin:
        host = str(ipaddress.IPv6Address(url.raw_host.decode("ascii"))) if b":" in url.raw_host else url.host
        port = None if url.port == _DEFAULT_PORTS.get(url.scheme) else url.port
        return cls(scheme=url.scheme, host=host, port=port)


class OffOriginCredentialStripper:
    def __init__(self, origin: RequestOrigin, names: frozenset[str]) -> None:
        self._origin = origin
        self._names = names

    async def __call__(self, request: httpx.Request | httpx2.Request) -> None:
        if RequestOrigin.of(request.url) == self._origin:
            return
        for name in self._names:
            request.headers.pop(name, None)


class CredentialedClientGuard:
    @classmethod
    def keep_no_cookies(cls, client: httpx.AsyncClient | httpx2.AsyncClient) -> None:
        client.cookies.jar.set_policy(DefaultCookiePolicy(allowed_domains=[]))

    @classmethod
    def strip_off_origin(
        cls, client: httpx.AsyncClient | httpx2.AsyncClient, origin: RequestOrigin, names: frozenset[str]
    ) -> None:
        client.event_hooks["request"] = [*client.event_hooks["request"], OffOriginCredentialStripper(origin, names)]


__all__ = ["CredentialedClientGuard", "OffOriginCredentialStripper", "RequestOrigin"]
