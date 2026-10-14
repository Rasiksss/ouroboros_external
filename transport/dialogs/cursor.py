"""The cached SDK update cursor, backed by the transport's repository."""

from __future__ import annotations

import logging
import threading
from typing import Any

from transport.dialogs.interfaces import IDialogsCursorRepository

log = logging.getLogger(__name__)


class DialogsCursor:
    """Reads the committed offset once, then commits every advance."""

    def __init__(self, repository: IDialogsCursorRepository) -> None:
        self._repository = repository
        self._lock = threading.Lock()
        self._value = 0
        self._loaded = False

    @property
    def value(self) -> int:
        with self._lock:
            if not self._loaded:
                self._value = self._read()
                self._loaded = True
            return self._value

    def commit(self, cursor: Any) -> None:
        """Persist an acknowledged offset; a message is handled before this runs."""
        if cursor is None:
            return
        try:
            value = int(cursor)
        except (TypeError, ValueError):
            return
        with self._lock:
            self._repository.save_cursor(value)
            self._value = value
            self._loaded = True

    def _read(self) -> int:
        try:
            return self._repository.load_cursor()
        except Exception:
            log.warning("Failed to read the dialogs cursor; starting from zero", exc_info=True)
            return 0


__all__ = ["DialogsCursor"]
