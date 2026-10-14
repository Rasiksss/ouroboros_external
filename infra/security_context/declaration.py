from __future__ import annotations

from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from infra.security_context.interface import SecurityParameterKey, SecurityParameterType
from ouroboros.storage.settings.schema import HTTP_TOKEN_RE


class SecurityParameterDeclaration(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    type: SecurityParameterType
    name: str
    ttl_sec: int = Field(strict=True, gt=0)

    @model_validator(mode="after")
    def _validate_name(self) -> Self:
        if not HTTP_TOKEN_RE.fullmatch(self.name):
            raise ValueError(f"a declared {self.type.value} name must be a non-empty HTTP token")
        if self.type is SecurityParameterType.HEADER and self.name.lower() == "cookie":
            raise ValueError("a header named cookie is refused; declare cookies with type cookie")
        return self

    @property
    def key(self) -> SecurityParameterKey:
        return SecurityParameterKey(self.type, self.name)


class HeaderCollectorDeclaration(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    parameters: tuple[SecurityParameterDeclaration, ...]

    @model_validator(mode="after")
    def _refuse_duplicate_keys(self) -> Self:
        declared: set[SecurityParameterKey] = set()
        for parameter in self.parameters:
            key = parameter.key
            if key in declared:
                raise ValueError(f"security parameter {key.label} is declared more than once")
            declared.add(key)
        return self


__all__ = ["HeaderCollectorDeclaration", "SecurityParameterDeclaration"]
