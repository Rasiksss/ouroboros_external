"""LLM-facing tool schema: name, description, parameters."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class ToolSchema:
    name: str
    description: str
    parameters: dict[str, Any]

    def to_dict(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }
