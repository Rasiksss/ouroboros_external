"""Domain errors for unified memory."""

from __future__ import annotations


class MemoryNotFoundError(LookupError):
    """Raised when a named memory does not exist."""

    def __init__(self, name: str):
        super().__init__(f"Memory not found: {name}")
        self.name = name


__all__ = ["MemoryNotFoundError"]
