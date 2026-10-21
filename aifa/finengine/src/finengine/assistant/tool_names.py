"""Имена инструментов для экрана (CONTRACTS.md §2.5, шаг 10): прежние названия инструментов AIFA.

Экран показывает название инструмента как есть (`toolLabel`), а знает он старые — поэтому `aifa_move`
называется для него `financial_pipeline` («Считаю показатель каноническим конвейером»), а `aifa_open` —
`read_source`: он открывает цикл по документу хода (ADR-0060). Контракт —
`aifa/contracts/tool_names.json`; эта копия нужна, потому что каталог `aifa/` в образ службы не попадает,
совпадение держит `tests/assistant/test_tool_events.py`.
"""

from __future__ import annotations

SCREEN_NAMES: dict[str, str] = {
    "aifa_open": "read_source",
    "aifa_wait": "financial_pipeline",
    "aifa_state": "read_source",
    "aifa_doc": "read_source",
    "aifa_move": "financial_pipeline",
    "aifa_register_atom": "financial_pipeline",
    "aifa_report": "financial_pipeline",
    "aifa_finish": "financial_pipeline",
}


def screen_name(tool: str) -> str:
    """Незнакомый инструмент показываем как есть: лучше техническое слово, чем красивая ложь."""
    return SCREEN_NAMES.get(tool, tool)
