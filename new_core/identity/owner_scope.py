"""The frozen identity value carried from the transport boundary to the run state.

``OwnerScope`` answers "whose data, which agent's, on which surface, in which chat" for
every row the system writes and every task it runs. It lives at engine level so
``new_core/loop`` can name the type without importing ouroboros.
"""

from __future__ import annotations

import json
import uuid
from enum import Enum
from typing import Annotated, Any, Final

from pydantic import BaseModel, ConfigDict, StringConstraints, field_validator


class SurfaceId(str, Enum):
    """The closed surface dictionary: one member per transport.

    A closed type rather than a validated string, so an unknown surface is a type error
    at the boundary instead of a runtime lookup somewhere downstream. The set of members
    equals the set of transport ``TRANSPORT_NAME`` constants, pinned by a test.
    """

    DIALOGS = "dialogs"
    BROWSER = "browser"
    A2A = "a2a"
    SKILL_BRIDGE = "skill-bridge"

    # Python 3.10 renders a str-mixin member as "SurfaceId.DIALOGS"; every consumer —
    # log lines, event payloads, the task-source normalizer — wants the value.
    __str__ = str.__str__

    @classmethod
    def coerce(cls, value: Any) -> SurfaceId | None:
        """The closed surface for ``value``, or ``None`` when it names no surface.

        Accepts the legacy spellings older rows were written with. Nothing produces
        them: they are normalized on the way in and never on the way out.
        """
        text = str(value or "").strip().lower()
        try:
            return cls(text)
        except ValueError:
            return _LEGACY_SURFACES.get(text)


#: Surfaces a person is on the other end of; the other two carry a peer agent and a skill.
SURFACES_WITH_A_PERSON: Final[frozenset[SurfaceId]] = frozenset({SurfaceId.DIALOGS, SurfaceId.BROWSER})

#: Spellings written by older code, accepted when reading a stored scope.
_LEGACY_SURFACES: Final[dict[str, SurfaceId]] = {
    "sberchat": SurfaceId.DIALOGS,
    "web": SurfaceId.BROWSER,
    "internal": SurfaceId.SKILL_BRIDGE,
    "unknown": SurfaceId.SKILL_BRIDGE,
}

#: Every axis is a non-empty string. Surrounding whitespace is never meaningful in an
#: identifier, so it is stripped rather than being allowed to make two scopes differ.
Axis = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class OwnerScope(BaseModel):
    """Owner, agent, surface, and chat — the whole identity of a run and its rows.

    Frozen and closed: every axis is required, an unknown surface is rejected, and an
    extra field is rejected rather than ignored — a payload carrying ``trace_id`` or
    ``conversation_id`` is a caller that thinks identity is wider than it is.

    ``agent_id`` is an axis rather than a later addition because the rows this value keys
    outlive it: one owner may run several agents, and an axis that arrives after the
    storage migration means migrating the same tables a second time. It is a real axis and
    not a deployment constant — a browser client selects among its owner's agents — so no
    cache key, delivery predicate or storage key may fold it away.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    owner_id: Axis
    agent_id: Axis
    surface_id: SurfaceId
    chat_id: Axis

    @field_validator("surface_id", mode="before")
    @classmethod
    def _accept_legacy_spelling(cls, value: Any) -> Any:
        """A surface no longer spelled that way is still that surface.

        An unrecognized value is handed back untouched so the enum reports it with the
        list of what was expected, rather than failing as a bare ``None``.
        """
        coerced = SurfaceId.coerce(value)
        return coerced if coerced is not None else value

    @property
    def security_token(self) -> str:
        material = json.dumps(
            [self.owner_id, self.agent_id, self.surface_id.value], ensure_ascii=True, separators=(",", ":")
        )
        return uuid.uuid5(uuid.NAMESPACE_URL, material).hex

    def to_dict(self) -> dict[str, str]:
        """The serializable form written into a task payload or a stored row."""
        return self.model_dump(mode="json")

    @classmethod
    def from_dict(cls, payload: Any) -> OwnerScope:
        """Read a stored scope strictly: no axis may be missing, no field may be extra.

        Raises ``ValidationError`` — a ``ValueError`` — rather than substituting a
        default, so a malformed payload fails where it is read.
        """
        return cls.model_validate(payload)


__all__ = ["Axis", "OwnerScope", "SurfaceId"]
