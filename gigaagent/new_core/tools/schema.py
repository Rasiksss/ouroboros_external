"""LLM-facing tool schema: name, description, parameters."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ouroboros.contracts.task_plan import ExecutionBinding


@dataclass
class ToolSchema:
    name: str
    description: str
    parameters: dict[str, Any]
    plan_executor: bool = False
    plan_closer: bool = False

    def __post_init__(self) -> None:
        if self.plan_executor:
            self.parameters = {
                **self.parameters,
                "properties": {**self.parameters.get("properties", {}), "_plan": ExecutionBinding.model_json_schema()},
            }

    def to_dict(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }
