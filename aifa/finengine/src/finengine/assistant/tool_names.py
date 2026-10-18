"""Имена инструментов для экрана (CONTRACTS.md §2.5, шаг 10): прежние названия инструментов AIFA.

Экран показывает название инструмента как есть (`toolLabel`), а знает он старые — поэтому `aifa_goal`
называется для него `financial_pipeline` («Считаю показатель каноническим конвейером»). Контракт —
`aifa/contracts/tool_names.json`; эта копия нужна, потому что каталог `aifa/` в образ службы не попадает,
совпадение держит `tests/assistant/test_tool_events.py`.
"""

from __future__ import annotations

SCREEN_NAMES: dict[str, str] = {
    "aifa_goal": "financial_pipeline",
    "aifa_wait": "financial_pipeline",
}


def screen_name(tool: str) -> str:
    """Незнакомый инструмент показываем как есть: лучше техническое слово, чем красивая ложь."""
    return SCREEN_NAMES.get(tool, tool)
