"""F6 — в кадрах хода нет вложений (ADR-0040 п. 2, CONTRACTS.md §2.2).

Статически: клиент хода не называет ключей вложений. Во время исполнения: клиент пропускает только
поля §2.2, всё остальное — `ForbiddenFrame` до отправки (`test_assistant_client.py`).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from finengine.assistant.ouro_client import CHAT_FRAME_KEYS, STOP_FRAME_KEYS

ASSISTANT = Path(__file__).resolve().parents[2] / "src" / "finengine" / "assistant"
ATTACHMENT_WORDS = ("attachment_ids", "detached")


@pytest.mark.parametrize("word", ATTACHMENT_WORDS)
def test_the_turn_client_never_names_the_attachment_keys(word: str) -> None:
    """F6 (ADR-0040): `attachment_ids` и `detached` не встречаются в коде клиента хода — даже в комментарии."""
    named = [p.name for p in sorted(ASSISTANT.glob("*.py")) if word in p.read_text(encoding="utf-8")]

    assert named == [], f"F6 (ADR-0040): `{word}` в {named}: документ идёт только инструментом aifa_doc"


def test_the_allowed_frame_fields_are_exactly_the_contract_ones() -> None:
    """F6: список разрешённых полей кадра `chat` — §2.2, кадра `/stop` — §2.7; новое поле — только через ADR."""
    assert CHAT_FRAME_KEYS == {"type", "content", "client_message_id", "sender_session_id"}
    assert STOP_FRAME_KEYS == {"type", "cmd", "client_message_id"}


def test_the_scan_sees_the_client_files() -> None:
    """Страховка F6: пустое множество файлов — зелёная проверка, поэтому оно должно быть непустым."""
    assert {"ouro_client.py", "turn_gateway.py", "frames.py"} <= {p.name for p in ASSISTANT.glob("*.py")}
