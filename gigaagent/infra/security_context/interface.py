from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType


class SecurityParameterType(StrEnum):
    HEADER = "header"
    COOKIE = "cookie"


@dataclass(frozen=True)
class SecurityParameterKey:
    type: SecurityParameterType
    name: str

    def __post_init__(self) -> None:
        kind = SecurityParameterType(self.type)
        object.__setattr__(self, "type", kind)
        if kind is SecurityParameterType.HEADER:
            object.__setattr__(self, "name", self.name.lower())

    @property
    def label(self) -> str:
        return f"{self.type.value}:{self.name}"


@dataclass(frozen=True, repr=False)
class SecurityParameter:
    key: SecurityParameterKey
    value: str
    ttl_sec: int

    def __post_init__(self) -> None:
        ttl_sec: object = self.ttl_sec
        if isinstance(ttl_sec, bool) or not isinstance(ttl_sec, int) or ttl_sec <= 0:
            raise ValueError(f"security parameter {self.key.label} requires a positive integer ttl_sec")

    @classmethod
    def by_key(cls, parameters: Collection[SecurityParameter]) -> dict[SecurityParameterKey, SecurityParameter]:
        held: dict[SecurityParameterKey, SecurityParameter] = {}
        for parameter in parameters:
            if parameter.key in held:
                raise ValueError(f"security parameter {parameter.key.label} is given more than once")
            held[parameter.key] = parameter
        return held

    def __repr__(self) -> str:
        return f"SecurityParameter({self.key.label}, ttl_sec={self.ttl_sec})"

    def __str__(self) -> str:
        return self.__repr__()


@dataclass(frozen=True, repr=False)
class SecurityContext:
    parameters: Mapping[SecurityParameterKey, str]

    def __init__(self, parameters: Mapping[SecurityParameterKey, str]) -> None:
        object.__setattr__(self, "parameters", MappingProxyType(dict(parameters)))

    def value(self, key: SecurityParameterKey) -> str | None:
        return self.parameters.get(key)

    def __repr__(self) -> str:
        names = ", ".join(key.label for key in self.parameters)
        return f"SecurityContext(parameters=[{names}])"

    def __str__(self) -> str:
        return self.__repr__()


class ISecurityContextStore(ABC):
    @abstractmethod
    async def save(self, token: str, parameters: Collection[SecurityParameter]) -> None:
        pass

    @abstractmethod
    async def load(self, token: str, keys: Collection[SecurityParameterKey]) -> SecurityContext:
        pass


class ISecurityContextCollector(ABC):
    @abstractmethod
    def collect(self, headers: Iterable[tuple[str, str]]) -> tuple[SecurityParameter, ...]:
        pass


__all__ = [
    "ISecurityContextCollector",
    "ISecurityContextStore",
    "SecurityContext",
    "SecurityParameter",
    "SecurityParameterKey",
    "SecurityParameterType",
]
