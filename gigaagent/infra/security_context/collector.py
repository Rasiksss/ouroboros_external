from __future__ import annotations

import logging
from collections.abc import Iterable

from infra.security_context.declaration import HeaderCollectorDeclaration
from infra.security_context.interface import (
    ISecurityContextCollector,
    SecurityParameter,
    SecurityParameterType,
)

log = logging.getLogger(__name__)


class DeclaredParametersCollector(ISecurityContextCollector):
    _COOKIE_HEADER = "cookie"

    def __init__(self, declaration: HeaderCollectorDeclaration) -> None:
        self._declaration = declaration
        self._header_names = frozenset(
            parameter.key.name for parameter in declaration.parameters if parameter.type is SecurityParameterType.HEADER
        )

    def collect(self, headers: Iterable[tuple[str, str]]) -> tuple[SecurityParameter, ...]:
        values: dict[str, str] = {}
        cookie_headers: list[str] = []
        for name, value in headers:
            folded = name.lower()
            if folded == self._COOKIE_HEADER:
                cookie_headers.append(value)
            elif folded in values:
                log.warning("The handshake carries the declared header %s more than once; it is not recorded", folded)
                values[folded] = ""
            elif folded in self._header_names:
                values[folded] = value
        cookies = self._cookies(cookie_headers)
        collected: list[SecurityParameter] = []
        for parameter in self._declaration.parameters:
            key = parameter.key
            if key.type is SecurityParameterType.HEADER:
                value = values.get(key.name, "")
            else:
                value = cookies.get(key.name, "")
            if value:
                collected.append(SecurityParameter(key, value, parameter.ttl_sec))
        return tuple(collected)

    @classmethod
    def _cookies(cls, headers: Iterable[str]) -> dict[str, str]:
        claimed: dict[str, str] = {}
        for header in headers:
            for chunk in header.split(";"):
                name, separator, value = chunk.strip(" \t").partition("=")
                if separator and name:
                    claimed.setdefault(name, value)
        return {name: value for name, value in claimed.items() if value}


__all__ = ["DeclaredParametersCollector"]
