"""SQLA-backed serializer: validates ORM objects into `ReadSchema` instances."""

from __future__ import annotations

from collections.abc import Sequence
from typing import TypeVar

from pydantic import BaseModel

from app.serializers.base import BaseSerializer

M = TypeVar("M")
S = TypeVar("S", bound=BaseModel)


class SQLASerializer[M, S: BaseModel](BaseSerializer):
    """Wrap one `ReadSchema` and turn SQLA rows into Pydantic instances.

    Relies on `ReadSchema.model_config["from_attributes"] = True`.
    """

    def __init__(self, read_schema: type[S]) -> None:
        self._read_schema = read_schema

    async def get_single(self, obj: M | None) -> S | None:
        if obj is None:
            return None
        return self._read_schema.model_validate(obj)

    async def get_list(self, objects: Sequence[M]) -> list[S]:
        return [self._read_schema.model_validate(obj) for obj in objects]
