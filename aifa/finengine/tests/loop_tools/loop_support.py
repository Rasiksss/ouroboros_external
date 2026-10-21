"""Опоры тестов инструментов цикла: открытый цикл хода и клиент, играющий ouroboros.

Клиент ведёт цикл вызовами `aifa_state` → `aifa_move` → … → `aifa_finish` через настоящий HTTP-роут,
привязку, очередь и `LoopSession`; решения — те же записанные ответы, что у тестов `DynamicRun`,
только аргументами вызова. Каталоги тестов не пакеты: модуль импортируют по имени.
"""
from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from pathlib import Path

from subtask_support import delegate
from test_ephemeral_in_graph import CASE, ENTITY, OWNER
from tool_harness import CHAT, ROOT, OpenRun, Plugin, ctx_of, open_run

from finengine.assistant.loop_desk import LoopDesk, LoopEmitter
from finengine.assistant.subtask_waits import SubtaskWaits
from finengine.assistant.tool_api import ToolApi
from finengine.contracts import GoalIR
from finengine.skills.atoms.graph_atoms import build_registry, default_policy
from finengine.skills.atoms.run_passport import build_run_passport, classification_from_cell
from finengine.skills.case_runtime import CaseRuntime
from finengine.skills.production_runtime import SwitchSink
from finengine.skills.spec.dynamic_run import LoopSession, RunLimits
from finengine.skills.spec.graph_executor import ExecutionBudget, RunInputs
from finengine.skills.spec.loop_state import LoopOrigin
from finengine.skills.spec.target import Target
from finengine.store.cases import SqlCaseStore


def make_passport() -> Any:
    """Паспорт прогона, как у цикла в службе: без него граф не находит уже полученное и не достраивает гейты."""
    goal = GoalIR(raw_query="сложный вопрос", intent_kind="document_qa")
    return build_run_passport(
        "financial report", goal_ir_id=goal.goal_id, classification=classification_from_cell("ifrs_nf_ru"),
    )


def new_runtime(tmp_path: Path, name: str, read_value) -> CaseRuntime:
    """Ещё один рантайм с Case, как фикстура `base`: две системы на одних решениях не делят артефактов."""
    counted, locate = read_value
    runtime = CaseRuntime(
        store=SqlCaseStore(f"sqlite:///{tmp_path / name}.db"),
        registry=build_registry(profile_id="ifrs_nf_ru", locate_executor=locate, read_value_executor=counted),
        policy=default_policy("ifrs_nf_ru"), budget=ExecutionBudget(max_retries=0),
    )
    runtime.ensure_case(owner_id=OWNER, case_id=CASE)
    runtime.register_document(
        owner_id=OWNER, case_id=CASE, logical_name="report.pdf", document_id="doc:v1", document_version="sha256:v1",
    )
    return runtime


class Gateway:
    def root_known(self, run_id: str, task_id: str) -> bool:
        return True


def args_of(raw: str) -> dict[str, Any]:
    """Записанное решение планировщика (JSON ответа модели) → аргументы вызова `aifa_move`."""
    data = json.loads(raw)
    for key in ("action", "answer", "evidence_ids"):
        data.pop(key, None)
    return data


def finish_args(raw: str) -> dict[str, Any]:
    """Записанное `finalize`/`give_up` → аргументы `aifa_finish` (ADR-0042: без текста ответа)."""
    data = json.loads(raw)
    return {
        "action": data["action"], "deliverables": data.get("deliverables", []),
        "evidence_ids": data.get("evidence_ids", []),
    }


@dataclass
class Cycle:
    """Открытый цикл хода и клиент-ouroboros. `events` — события цикла, как их получил бы экран."""

    run: OpenRun
    desk: LoopDesk
    session: LoopSession
    plugin: Plugin
    events: list[dict[str, Any]] = field(default_factory=list)
    calls: list[tuple[str, str]] = field(default_factory=list)

    async def call(
        self, tool: str, args: dict[str, Any] | None = None, *, depth: int = 0, task: str = "sub-task",
    ) -> str:
        ctx = ctx_of(self.run.run_id) if depth == 0 else ctx_of(
            self.run.run_id, task_id=task, root_task_id=ROOT, task_depth=depth
        )
        response = await self.plugin.call(tool, ctx, args)
        self.calls.append((tool, response.text))
        return response.text

    async def move(self, raw: str) -> str:
        return await self.call("aifa_move", args_of(raw))

    async def finish(self, raw: str) -> str:
        return await self.call("aifa_finish", finish_args(raw))

    async def close(self) -> None:
        await self.plugin.close()
        await self.run.end()


async def open_cycle(
    base, *, scenario: Callable[[str], Any] | None = None, author: Any = None,
    limits: RunLimits = RunLimits(), passport: Any = None, doc_ids: tuple[str, ...] = (),
    message: str = "сложный вопрос", entry_stage: str = "planning", document_text: str = "",
    subtasks: SubtaskWaits | None = None,
) -> Cycle:
    """Цикл, открытый так, как его открывает `aifa_goal` при `НУЖЕН_ЦИКЛ`: сессия принята `LoopDesk`."""
    run = open_run(user_id=OWNER, case_id=CASE, doc_ids=doc_ids, message=message)
    goal = GoalIR(raw_query=message, intent_kind="document_qa")
    target = Target(output_type="QuoteEvidence", entity_ref=ENTITY)
    session = LoopSession(
        runtime=base, owner_id=OWNER, case_id=CASE, goal=goal, target=target, inputs=RunInputs(document_text=document_text),
        run_scenario=scenario, limits=limits, run_passport=passport, event_sink=SwitchSink(), entry_stage=entry_stage,  # type: ignore[arg-type]
    )
    if author is not None:
        delegate(session, author)
    session.open(deterministic_error="нет пути")
    desk = LoopDesk()
    desk.adopt(
        run.run_id, OWNER, LoopEmitter.here(run.run_id), session,
        LoopOrigin(owner_id=OWNER, case_id=CASE, document_id="doc:v1", goal=goal, target=target),
    )
    cycle = Cycle(run, desk, session, Plugin(ToolApi(Gateway(), _NoGoal(), desk, subtasks)))
    session.event_sink.target = cycle.events.append  # type: ignore[attr-defined]
    return cycle


class _NoGoal:
    """Цикл открыт заранее (`open_cycle`); `aifa_goal` в этих тестах не вызывается."""

    async def __call__(self, record: Any, emit: Any) -> Any:
        raise AssertionError("aifa_goal не должен вызываться")


__all__ = ["make_passport", "new_runtime", "CHAT", "CASE", "ENTITY", "OWNER", "ROOT", "Cycle", "args_of", "finish_args", "open_cycle"]
