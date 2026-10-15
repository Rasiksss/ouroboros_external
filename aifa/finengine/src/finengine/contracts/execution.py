"""Область «исполнение»: состояние одного запуска одного узла графа.

Каждая смена состояния — новая неизменяемая запись `NodeRun`, а не мутация
прежней: то же правило идентичности/версионирования, что и для доказательств.

`NodeStatus` — автомат состояний узла, заведённый в Срезе 3 (A2): «`NodeRun`
проходит состояния `planned → ready → running → succeeded|failed|blocked|
skipped`». Фаза 0 держала `status` свободной строкой ровно потому, что этого
автомата ещё не было (карточка A2 — эта сессия); теперь, когда он есть,
`Literal` — не косметика, а часть контракта: `GraphExecutor` не имеет права
записать в `NodeRun` состояние вне этого перечня."""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field

from .base import Contract, NonEmptyStr, new_id
from .planning import NodeKey

NodeStatus = Literal["planned", "ready", "running", "succeeded", "failed", "blocked", "skipped"]


class NodeRun(Contract):
    """Один запуск одного узла (A2.1.7: время начала/окончания, статус, число
    попыток, входные/выходные идентификаторы, версия атома, модель/инструкция
    при наличии, профиль песочницы). Версия атома уже несёт `node_key.
    atom_version` — второго поля под неё не заводим."""

    node_run_id: str = Field(default_factory=new_id)
    node_key: NodeKey
    run_id: NonEmptyStr
    status: NodeStatus
    sandbox_profile: NonEmptyStr
    attempt: int = Field(default=1, ge=1)
    produced_evidence_ids: list[str] = Field(default_factory=list)
    model_version: str | None = None
    prompt_version: str | None = None
    error: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
