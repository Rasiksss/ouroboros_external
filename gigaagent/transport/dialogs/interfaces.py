from __future__ import annotations

from abc import ABC, abstractmethod


class IDialogsCursorRepository(ABC):
    @abstractmethod
    def load_cursor(self) -> int:
        pass

    @abstractmethod
    def save_cursor(self, cursor: int) -> None:
        pass
