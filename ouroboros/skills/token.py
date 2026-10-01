"""Opaque skill token helpers for the Host Service API."""

from __future__ import annotations

from copy import Error as CopyError
from typing import Any, SupportsIndex


class SkillToken:
    """Credential wrapper that refuses accidental stringification."""

    _REDACTED = "<SkillToken redacted>"

    def __init__(self, value: str):
        token = str(value or "").strip()
        if not token:
            raise ValueError("SkillToken cannot be empty")
        self._value = token

    @classmethod
    def from_env(cls, key: str = "HOST_SERVICE_TOKEN") -> SkillToken:
        from ouroboros.storage.settings.store import load_settings

        return cls(str(load_settings().get(key, "") or ""))

    def use_in_request(self) -> str:
        """Explicitly reveal the token at an HTTP-auth call site."""
        return self._value

    def __repr__(self) -> str:
        return self._REDACTED

    def __str__(self) -> str:
        return self._REDACTED

    def __format__(self, _format_spec: str) -> str:
        return self._REDACTED

    def __reduce__(self) -> Any:
        raise TypeError("SkillToken cannot be pickled")

    def __reduce_ex__(self, _protocol: SupportsIndex) -> Any:
        raise TypeError("SkillToken cannot be pickled")

    def __copy__(self) -> SkillToken:
        raise CopyError("SkillToken cannot be copied")

    def __deepcopy__(self, _memo: dict[int, Any]) -> SkillToken:
        raise CopyError("SkillToken cannot be deep-copied")

    def __getstate__(self) -> dict[str, Any]:
        raise TypeError("SkillToken state is not serializable")


__all__ = ["SkillToken"]
