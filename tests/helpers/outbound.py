"""An outbound boundary that records what a run handed it, for tests that assert on it."""

from __future__ import annotations

from typing import Any

from new_core.tools.interfaces import ToolOutbound
from ouroboros.contracts.attachments import StoredFile


class RecordingOutbound(ToolOutbound):
    """Keeps every publish and reports whatever outcome the test asked for.

    ``outcome`` is the ``(accepted, detail)`` pair the real boundary returns: acceptance,
    never delivery.
    """

    def __init__(self, outcome: tuple[bool, str] = (True, "ok")) -> None:
        self.outcome = outcome
        self.files: list[tuple[StoredFile, dict[str, Any]]] = []
        self.images: list[tuple[StoredFile, dict[str, Any]]] = []

    def send_file(self, media: StoredFile, **kwargs: Any) -> tuple[bool, str]:
        self.files.append((media, dict(kwargs)))
        return self.outcome

    def send_image(self, media: StoredFile, **kwargs: Any) -> tuple[bool, str]:
        self.images.append((media, dict(kwargs)))
        return self.outcome

    @property
    def published(self) -> list[tuple[StoredFile, dict[str, Any]]]:
        """Every publish in order of kind: files first, then images."""
        return [*self.files, *self.images]


class RaisingOutbound(ToolOutbound):
    """A boundary whose publish raises, to exercise the tool's own guard."""

    def __init__(self, error: Exception | None = None) -> None:
        self._error = error if error is not None else RuntimeError("the outbox is gone")

    def send_file(self, media: StoredFile, **kwargs: Any) -> tuple[bool, str]:
        raise self._error

    def send_image(self, media: StoredFile, **kwargs: Any) -> tuple[bool, str]:
        raise self._error


__all__ = ["RaisingOutbound", "RecordingOutbound"]
