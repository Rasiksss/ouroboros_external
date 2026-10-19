"""Состояние цикла как JSON: сохраняется после каждого хода и поднимается после перезапуска (ADR-0023).

Две части: `LoopOrigin` — из чего сессию собирают (цель, корень графа, документ), и `state_of` — что в ней
накоплено ходами. Временные атомы здесь не лежат: их исходники хранит Case (`save_source`).
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from time import monotonic
from typing import Any

from finengine.contracts import GoalIR
from finengine.skills.spec.dynamic_run import JournalEntry, LoopSession
from finengine.skills.spec.room_files import FileSpec
from finengine.skills.spec.target import Target


@dataclass(frozen=True)
class LoopOrigin:
    """Всё, из чего собирается `LoopSession`, кроме накопленного: рантайм и каналы строятся заново."""

    owner_id: int | str
    case_id: str
    document_id: str
    goal: GoalIR
    target: Target
    root_extra: dict[str, str] = field(default_factory=dict)
    trace_id: str = ""
    entry_stage: str = "planning"
    methodology_version: str | None = None  # паспорт прогона должен совпасть с исходным
    budget: dict[str, Any] | None = None    # бюджет графа, под которым цикл был открыт

    def to_doc(self) -> dict[str, Any]:
        return {
            "owner_id": self.owner_id, "case_id": self.case_id, "document_id": self.document_id,
            "goal": self.goal.model_dump(mode="json"), "target": asdict(self.target),
            "root_extra": dict(self.root_extra), "trace_id": self.trace_id, "entry_stage": self.entry_stage,
            "methodology_version": self.methodology_version, "budget": self.budget,
        }

    @classmethod
    def from_doc(cls, doc: dict[str, Any]) -> LoopOrigin:
        return cls(
            owner_id=doc["owner_id"], case_id=doc["case_id"], document_id=doc["document_id"],
            goal=GoalIR.model_validate(doc["goal"]), target=Target(**doc["target"]),
            root_extra=dict(doc["root_extra"]), trace_id=doc["trace_id"], entry_stage=doc["entry_stage"],
            methodology_version=doc["methodology_version"], budget=doc["budget"],
        )


def state_of(session: LoopSession) -> dict[str, Any]:
    return {
        "turns": session.turns, "idle": session.idle, "rejected": session.rejected, "closed": session.closed,
        "elapsed": monotonic() - session.opened_at, "feedback": session.feedback,
        "created": session.created, "build_attempts": session.build_attempts,
        "evidence_generation": session.evidence_generation,
        "dead_end_actions": [[list(signature), generation] for signature, generation in session.dead_end_actions.items()],
        "plan": list(session.plan), "expected_files": [asdict(spec) for spec in session.expected_files],
        "journal": [asdict(entry) for entry in session.journal],
        "goal_status": dict(session.goal_status), "failures": dict(session.failures),
        "initial_evidence_ids": sorted(session.initial_evidence_ids),
        "initial_file_ids": sorted(session.initial_file_ids),
    }


def restore_state(session: LoopSession, doc: dict[str, Any]) -> None:
    session.turns, session.idle, session.rejected = doc["turns"], doc["idle"], doc["rejected"]
    session.closed, session.feedback = doc["closed"], doc["feedback"]
    session.opened_at = monotonic() - doc["elapsed"]
    session.created, session.build_attempts = doc["created"], doc["build_attempts"]
    session.evidence_generation = doc["evidence_generation"]
    session.dead_end_actions = {tuple(signature): generation for signature, generation in doc["dead_end_actions"]}
    session.plan = tuple(doc["plan"])
    session.expected_files = tuple(FileSpec(**spec) for spec in doc["expected_files"])
    session.journal = [JournalEntry(**entry) for entry in doc["journal"]]
    session.goal_status, session.failures = dict(doc["goal_status"]), dict(doc["failures"])
    session.initial_evidence_ids = frozenset(doc["initial_evidence_ids"])
    session.initial_file_ids = frozenset(doc["initial_file_ids"])
