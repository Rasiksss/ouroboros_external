"""Domain errors for unified memory."""

from __future__ import annotations


class MemoryNotFoundError(LookupError):
    """Raised when a named memory does not exist."""

    def __init__(self, name: str):
        super().__init__(f"Memory not found: {name}")
        self.name = name


class MemoryConflictError(RuntimeError):
    """The named memory changed since the caller read it."""

    def __init__(self, name: str, expected: float, actual: float | None):
        super().__init__(f"Memory {name!r} changed: expected {expected}, actual {actual}")
        self.name, self.expected, self.actual = name, expected, actual


__all__ = ["MemoryConflictError", "MemoryNotFoundError"]
