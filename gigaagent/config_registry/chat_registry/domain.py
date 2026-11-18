from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from enum import StrEnum

TITLE_MAX_LENGTH = 60


def chat_title(text: str) -> str:
    lines = str(text or "").splitlines()
    if not lines:
        return ""
    opening = "".join(" " if unicodedata.category(char) == "Cc" else char for char in lines[0])
    return " ".join(opening.split())[:TITLE_MAX_LENGTH]


class TitleSource(StrEnum):
    PROVISIONAL = "provisional"
    MODEL = "model"
    OWNER = "owner"


@dataclass(frozen=True)
class ChatEntity:
    owner_id: str
    agent_id: str
    chat_id: str
    title: str
    title_source: TitleSource
    title_attempts: int
    is_active: bool
    created_at: str


__all__ = ["TITLE_MAX_LENGTH", "ChatEntity", "TitleSource", "chat_title"]
