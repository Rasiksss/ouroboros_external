"""Запись хода: что служба знает о ходе для привязки вызовов инструментов (CONTRACTS.md §3.0).

Пишется при создании хода (`POST /chat/turns`), читается вызовом инструмента, который может прийти
на любую реплику, — поэтому лежит в общем кэше координации рядом с состоянием прогона
(`run_orchestrator`), а не в памяти процесса.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from typing import Any

from finengine.skills import run_orchestrator
from finengine.store.cache import get_cache

# Как у общего состояния прогона (`run_orchestrator._СОСТОЯНИЕ_TTL`): запись живёт не дольше прогона.
TTL_SEC = 6 * 3600.0


@dataclass(frozen=True)
class TurnRecord:
    """`owner_id`/`chat_id` — идентичность в ouroboros (ADR-0014); `user_id`/`case_id` — владелец
    и Case в службе. `message` — исходное сообщение, цель `aifa_goal` (ADR-0004); `doc_ids` —
    документы хода, уже проверенные по владельцу (`_chat_turn_docs`)."""

    run_id: str
    owner_id: str
    chat_id: str
    message: str
    user_id: int
    case_id: str
    doc_ids: tuple[str, ...]
    active_doc_id: str
    role: str
    root_task_id: str = ""


def _key(run_id: str) -> str:
    return f"ouro_turn:{run_id}"


def remember(record: TurnRecord) -> None:
    get_cache().set(_key(record.run_id), asdict(record), TTL_SEC)


def recall(run_id: str) -> TurnRecord | None:
    raw: dict[str, Any] | None = get_cache().get(_key(run_id))
    if raw is None:
        return None
    return TurnRecord(**{**raw, "doc_ids": tuple(raw["doc_ids"])})


def with_root(record: TurnRecord, task_id: str) -> TurnRecord:
    """Корень хода назван: первый вызов инструмента несёт его `task_id` (ADR-0021 п. 3)."""
    named = replace(record, root_task_id=task_id)
    remember(named)
    return named


def run_is_active(run_id: str) -> bool:
    """Прогон хода идёт: в общем состоянии он есть и не закрыт (на любой реплике)."""
    state = run_orchestrator.состояние_прогона(run_id)
    return state is not None and not state.get("finished")
