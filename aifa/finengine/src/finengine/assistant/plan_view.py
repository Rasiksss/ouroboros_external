"""План от автора плана — событием для экрана (ADR-0074).

Итог подзадачи-планировщика приходит кадром; если в нём предложение плана, экран получает
`root_plan`: ревизии одного хода, шаги, критерии. Приняло ли предложение корень, служба не знает
(состояние `task_plan` живёт в ouroboros), поэтому ревизия — это предложение, а новая заменяет
прежнюю на экране.
"""

from __future__ import annotations

import json
import re
from typing import Any

from finengine.assistant.events import Event

_FENCED_BLOCK = re.compile(r"```[A-Za-z]*[ \t]*\n(.*?)```", re.DOTALL)


def _object_in(message: str) -> dict[str, Any] | None:
    """Объект предложения: сообщение целиком либо последний блок в ограждении, начинающийся с `{`."""
    candidates = [
        message.strip(),
        *(block.strip() for block in reversed(_FENCED_BLOCK.findall(message))),
    ]
    for text in candidates:
        if not text.startswith("{"):
            continue
        try:
            value = json.loads(text)
        except ValueError:
            continue
        if isinstance(value, dict):
            return value
    return None


def _text(value: Any) -> str:
    return str(value) if isinstance(value, str | int | float) else ""


def _listed(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _step(raw: Any) -> dict[str, Any]:
    step = raw if isinstance(raw, dict) else {}
    execution = step.get("execution") if isinstance(step.get("execution"), dict) else {}
    kind, instruction = _text(execution.get("kind")), _text(execution.get("instruction"))
    return {
        "id": _text(step.get("id")),
        "objective": _text(step.get("objective")),
        "depends_on": [_text(item) for item in _listed(step.get("depends_on"))],
        "execution": f"{kind}: {instruction}" if kind and instruction else kind or instruction,
        "outputs": [_text(item) for item in _listed(step.get("expected_outputs"))],
        "criteria": [
            {
                "id": _text(item.get("id")),
                "description": _text(item.get("description")),
                "verification": _text(item.get("verification_method")),
            }
            for item in _listed(step.get("acceptance_criteria"))
            if isinstance(item, dict)
        ],
    }


def root_plan_event(task_id: str, message: str) -> Event | None:
    proposal = _object_in(message)
    if proposal is None or not isinstance(proposal.get("steps"), list):
        return None
    return {
        "type": "root_plan",
        "task_id": task_id,
        "requirements": [
            {
                "id": _text(item.get("id")),
                "description": _text(item.get("description")),
                "source": _text(item.get("source")),
            }
            for item in _listed(proposal.get("requirements"))
            if isinstance(item, dict)
        ],
        "assumptions": [
            {
                "id": _text(item.get("id")),
                "description": _text(item.get("description")),
                "reason": _text(item.get("reason")),
            }
            for item in _listed(proposal.get("assumptions"))
            if isinstance(item, dict)
        ],
        "questions": [_text(item) for item in _listed(proposal.get("questions"))],
        "steps": [_step(raw) for raw in proposal["steps"]],
    }
