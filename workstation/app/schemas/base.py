"""Base Pydantic v2 schemas.

Rule:
    - `BaseSchema` (extra=forbid) — for inbound request payloads.
    - `ReadSchema`  (extra=ignore + from_attributes) — for outbound responses
      produced by `model_validate()` on SQLA instances.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ConfigDict


class BaseSchema(BaseModel):
    """Strict payload schema: reject unknown fields on input."""

    model_config = ConfigDict(extra="forbid")


class ReadSchema(BaseModel):
    """Response schema: ignore unknown attributes, build from ORM instances."""

    model_config = ConfigDict(extra="ignore", from_attributes=True)


@dataclass
class SchemaField:
    """Container to group schemas by operation kind (optional, for large APIs)."""

    get_list: Any = field(default=None)
    get_single: Any = field(default=None)
    create_update: Any = field(default=None)
    create: Any = field(default=None)
    update: Any = field(default=None)


@dataclass
class ModelSchema:
    """Group `SchemaField` bundles by audience (optional, for large APIs)."""

    manager: SchemaField | None = None
    customer: SchemaField | None = None
    internal: SchemaField | None = None
    audit: SchemaField | None = None
