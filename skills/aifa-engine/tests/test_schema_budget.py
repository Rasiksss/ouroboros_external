"""F4: схемы инструментов `aifa-engine` помещаются в первый запрос и равны снимку (ADR-0047 п. 1)."""

from __future__ import annotations

import json
from typing import Any

from aifa_engine_support import SNAPSHOT, Registered, registered_tools, schema_chars

from ouroboros.storage.settings.schema import AppSettings

TARGET_CHARS = 7000
EXPECTED_TOOLS = {"aifa_goal", "aifa_wait"}


def _shape(tool: Registered) -> dict[str, Any]:
    """То, что снимок держит о каждом инструменте: имя, аргументы, обязательные поля, политика, срок."""
    return {
        "arguments": sorted(tool.schema.get("properties", {})),
        "required": sorted(tool.schema.get("required", [])),
        "safety_policy": tool.options["safety_policy"],
        "capability_class": tool.options["capability_class"],
        "timeout_sec": tool.options["timeout_sec"],
        "result_limit": tool.options["result_limit"],
    }


def test_the_plugin_registers_exactly_the_tools_this_step_owns() -> None:
    """F4: набор инструментов — `aifa_goal` и `aifa_wait`; остальные шесть приходят шагами 13 и 16."""
    assert set(registered_tools()) == EXPECTED_TOOLS


def test_the_schemas_fit_the_first_request_budget_with_room_to_spare() -> None:
    """F4: сумма знаков схем в пределах цели 7000 и строго меньше предела ядра."""
    total = sum(schema_chars(tool) for tool in registered_tools().values())
    limit = AppSettings.model_fields["OUROBOROS_INITIAL_EXTENSION_SCHEMA_BUDGET"].default

    assert limit == 8000, "предел ядра изменился — перечитай ADR-0022 п. 5"
    assert total <= TARGET_CHARS < limit


def test_the_registered_contract_equals_the_snapshot() -> None:
    """F4: имена, аргументы, обязательные поля и политики равны снимку; смена — только новой ADR (F13)."""
    snapshot = json.loads(SNAPSHOT.read_text(encoding="utf-8"))

    actual = {name: _shape(tool) for name, tool in registered_tools().items()}

    assert actual == snapshot["tools"]


def test_the_snapshot_records_the_budget_it_was_taken_at() -> None:
    """F4: бюджет в снимке — то, что посчитано сейчас, чтобы разбухание описаний было видно в диффе."""
    snapshot = json.loads(SNAPSHOT.read_text(encoding="utf-8"))

    total = sum(schema_chars(tool) for tool in registered_tools().values())

    assert snapshot["budget_chars"] == total


def test_every_tool_is_read_by_the_dispatcher_without_a_safety_model_call() -> None:
    """ADR-0022 п. 4: каждый вызов проверяет код службы, лёгкая модель его не проверяет."""
    for name, tool in registered_tools().items():
        assert tool.options["safety_policy"] == "skip", name
        assert tool.options["result_limit"] == 12_000, name


def test_every_description_is_one_line() -> None:
    """ADR-0022 п. 5: описание — одна строка, правила — в навыке `aifa-planning`."""
    for name, tool in registered_tools().items():
        assert tool.description and "\n" not in tool.description, name
