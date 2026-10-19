"""Гибридный планировщик: цикл «решение модели → граф → исполнение → итог хода».

Цикл включается, когда детерминированный план не собрался или собранный граф упал при
исполнении с ошибкой (`entry_stage`). Каждый execute-ход
остаётся обычным PlanRun; этот модуль только ведёт состояние разговора моделей,
временные атомы и конечные лимиты.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import re
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, replace
from functools import partial
from pathlib import Path
from time import monotonic
from typing import Literal, Protocol, TypedDict

from pydantic import BaseModel

from finengine.config import settings
from finengine.contracts import (
    DynamicArtifact,
    EphemeralAtomSource,
    FileArtifact,
    GoalClause,
    GoalIR,
    NodeKey,
    RunPassport,
    VerificationFinding,
)
from finengine.llm.budget import BudgetExceeded
from finengine.roles.scope import build_role_plan
from finengine.skills.analysis.external import WEB_SEARCH_OFF_REASON, user_allowed_web_search
from finengine.skills.atoms.web_search import PRODUCER_PREFIX as WEB_SEARCH_PRODUCER
from finengine.skills.atoms.web_search import describe_hits
from finengine.skills.case_runtime import CaseRuntime, PlanInfeasible
from finengine.skills.metric_schema import registry_for_passport
from finengine.skills.result_events import author_attempt_event, dynamic_move_event, dynamic_plan_event
from finengine.skills.spec.atom_author import (
    AtomAuthor,
    StationFault,
    case_inputs,
    describe_inputs,
    end_of_chain,
    without_markdown_fence,
)
from finengine.skills.spec.atom_catalog import NOT_PLANNABLE, catalog_text
from finengine.skills.spec.atom_registry import AtomDef, AtomRegistry, NodeArgs
from finengine.skills.spec.ephemeral import (
    DOWNLOAD_SCHEMA,
    STATION_FAULT_PREFIX,
    AtomRequest,
    EphemeralAtom,
    check_request,
    to_atom_def,
)
from finengine.skills.spec.graph_executor import ExecutionResult, RunInputs
from finengine.skills.spec.graph_planner import AtomStep
from finengine.skills.spec.room_files import FileSpec, spec_problem
from finengine.skills.spec.target import InputSpec, Target
from finengine.store.cases import evidence_id_of

log = logging.getLogger(__name__)

_SKILL = Path(__file__).with_name("dynamic_run_planner.md")
_PLAN_PHASE = Path(__file__).with_name("dynamic_run_plan_phase.md")
# Виды цели, у которых план составляется отдельным вызовом модели до первого хода.
_PLAN_PHASE_GOALS: frozenset[str] = frozenset({"custom_analysis"})
# Предел действий одного хода: сценарии и части цели по ссылке вместе. Каждое действие — полный прогон
# конвейера с моделью, поэтому длительность хода растёт с их числом, а не с видом действия; отдельный
# предел на части позволил бы обойти этот через `goals`.
MAX_MOVE_ACTIONS = 3
MAX_STEPS = 30
# Сколько ходов с кодом атома цикл обязан потратить на обещанный файл, прежде чем `finalize` пройдёт без него.
_FILE_BUILD_ATTEMPTS = 2
_FILE_KIND = re.compile(r"[a-z0-9]{1,8}")
# Статус части разобранной цели в `DynamicRun.goal_status`: последний запуск по ссылке в этом прогоне удался.
GOAL_OBTAINED = "получена"
_SOURCE_OUTPUT_TYPE = "EphemeralAtomSource"
_INFRASTRUCTURE_OUTPUT_TYPES: frozenset[str] = frozenset({
    _SOURCE_OUTPUT_TYPE,
    "CandidateLocator",
    "CompoundResult",
    "DocumentIndex",
    "DocumentText",
    "RunPassport",
    "VerificationFinding",
    "Verified:bounds",
    "Verified:citation",
    "Verified:cross",
    "Verified:formula",
    "Verified:grounding",
    "Verified:identity",
    "Verified:time",
    "Verified:unit",
    "doc.textify:index",
    "doc.textify:pages",
    "document_text",
})
ALLOWED_EXTRA_KEYS: frozenset[str] = frozenset({
    "requested_concepts", "role", "depth", "analysis_type", "file_kind", "source_output_type", "topic",
})
# Служебные поля контрактов: планировщику не нужны, а в 300 символов кратко-описания значение показателя
# без их удаления не помещается.
_SUMMARY_NOISE_KEYS: frozenset[str] = frozenset({
    "schema_version", "output_type", "evidence_id", "produced_by_atom", "produced_by_atom_version",
    "source_ref", "document_version", "supersedes", "created_at", "metric_id", "input_evidence_ids",
    "output_id", "run_id",
})

CompleteFn = Callable[[str, str], Awaitable[str]]

_PLAN_PROPERTIES = {
    "plan": {"type": "array", "items": {"type": "string"}},
    "files": {
        "type": "array",
        "items": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "kind": {"type": "string"},
                "parts": {"type": "integer"},
                "formulas": {"type": "boolean"},
            },
            "required": ["kind", "parts", "formulas"],
        },
    },
}
_PLAN_RESPONSE_FORMAT = {
    "type": "json_schema",
    "json_schema": {
        "name": "dynamic_run_plan",
        "strict": True,
        "schema": {
            "type": "object", "additionalProperties": False,
            "properties": _PLAN_PROPERTIES, "required": ["plan", "files"],
        },
    },
}
_PLANNER_RESPONSE_FORMAT = {
    "type": "json_schema",
    "json_schema": {
        "name": "dynamic_run_decision",
        "strict": True,
        "schema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "objective": {"type": "string"},
                "reason": {"type": "string"},
                "action": {"type": "string", "enum": ["execute", "finalize", "give_up"]},
                **_PLAN_PROPERTIES,
                "scenarios": {"type": "array", "items": {"type": "string"}},
                "goals": {"type": "array", "items": {"type": "string"}},
                "new_atoms": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "name": {"type": "string"},
                            "purpose": {"type": "string"},
                            "requires": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "additionalProperties": False,
                                    "properties": {
                                        "input_type": {"type": "string"},
                                        "concept": {"type": ["string", "null"]},
                                    },
                                    "required": ["input_type", "concept"],
                                },
                            },
                            "output_type": {"type": "string"},
                            "output_schema": {"type": "object"},
                            "url": {"type": "string"},
                        },
                        "required": [
                            "name", "purpose", "requires", "output_type", "output_schema", "url",
                        ],
                    },
                },
                "steps": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "atom": {"type": "string"},
                            "concept": {"type": ["string", "null"]},
                            "period_ref": {"type": ["string", "null"]},
                            "extra": {
                                "type": "object",
                                "additionalProperties": False,
                                "properties": {
                                    key: {"type": "string"} for key in sorted(ALLOWED_EXTRA_KEYS)
                                },
                            },
                        },
                        "required": ["atom", "concept", "period_ref", "extra"],
                    },
                },
                "answer": {"type": "string"},
                "evidence_ids": {"type": "array", "items": {"type": "string"}},
                "deliverables": {"type": "array", "items": {"type": "string"}},
            },
            "required": [
                "objective", "reason", "action", "plan", "files", "scenarios", "goals", "new_atoms", "steps",
                "answer", "evidence_ids", "deliverables",
            ],
        },
    },
}


class _AuthorExtras(TypedDict, total=False):
    """Что `AtomAuthor.write` принимает сверх общего протокола автора."""

    on_attempt: Callable[[int, str, str | None, str], None]
    expected_files: tuple[FileSpec, ...]
    trace_id: str


@dataclass(frozen=True)
class RunLimits:
    max_moves: int = 12
    max_new_atoms: int = 16                 # каждая скачанная страница — отдельный атом (Р19.10)
    idle_moves: int = 3
    max_terminal_evidence: int = 20
    # Общее время цикла: проверяется между ходами, ход в полёте дорабатывает до бюджета своего графа.
    # Первая попытка (часть 14) уже потратила свой бюджет, поэтому ожидание пользователя ограничено суммой.
    max_wall_seconds: float = 1200.0


@dataclass(frozen=True)
class JournalEntry:
    objective: str
    reason: str
    outcome: str


@dataclass(frozen=True)
class Decision:
    objective: str
    reason: str
    action: Literal["execute", "finalize", "give_up"]
    scenarios: tuple[str, ...] = ()
    goals: tuple[str, ...] = ()             # goal_ref частей разобранной цели, исполняемых по ссылке (часть 18)
    new_atoms: tuple[AtomRequest, ...] = ()
    raw_steps: tuple[dict, ...] = ()
    answer: str = ""
    evidence_ids: tuple[str, ...] = ()
    deliverables: tuple[str, ...] = ()      # id `FileArtifact`: файлы-результаты ответа (часть 16)
    plan: tuple[str, ...] = ()              # части работы по порядку; непустой план заменяет прежний (часть 16а)
    files: tuple[FileSpec, ...] = ()        # файлы, которые ответ обещает пользователю


@dataclass(frozen=True)
class ScenarioResult:
    ok: bool
    summary: str


ScenarioRunner = Callable[[str], ScenarioResult]


class GoalRunner(Protocol):
    """Исполнение частей разобранной цели по ссылке: тот же конвейер, что у сообщения чата, без разбора фразы."""

    def refusal(self, clause: GoalClause) -> str | None:
        """Почему часть нельзя исполнить по ссылке (нет детерминированного корня); `None` — можно."""

    def __call__(self, clauses: list[GoalClause]) -> ScenarioResult: ...


@dataclass(frozen=True)
class MoveOutcome:
    headline: str
    details: str
    failed: bool = False
    empty_atoms: tuple[str, ...] = ()
    dead_end_actions: tuple[tuple[str, ...], ...] = ()
    transient: bool = False
    uncreated_atoms: tuple[str, ...] = ()
    steps_rejected: bool = False
    refusal: str = ""                       # код отказа §3.0, если ход не исполнялся из-за правила атома


@dataclass(frozen=True)
class DynamicRunResult:
    status: Literal["finalized", "gave_up", "stopped"]
    answer: str
    evidence: tuple[BaseModel, ...]
    journal: tuple[JournalEntry, ...]
    deliverables: tuple[FileArtifact, ...] = ()

    def manifest(self) -> dict:
        return {
            "status": self.status,
            "answer": self.answer,
            "evidence": [item.model_dump(mode="json") for item in self.evidence],
            "deliverables": [item.model_dump(mode="json") for item in self.deliverables],
            "moves": [asdict(entry) for entry in self.journal],
        }


async def _default_complete(system: str, prompt: str, response_format: dict = _PLANNER_RESPONSE_FORMAT) -> str:
    from finengine.llm import openrouter  # noqa: PLC0415 — единый шлюз модели

    started = monotonic()
    try:
        reply = without_markdown_fence(await asyncio.wait_for(
            openrouter.complete(
                prompt, model=settings.planner_model or None, system=system,
                response_format=response_format, temperature=0.0,
                # Размышления считаются в тот же бюджет вывода: при `high` 6000 токенов могли уйти на них целиком.
                retries=1, max_output=16000, reasoning_effort=settings.planner_reasoning_effort,
            ),
            timeout=settings.planner_model_timeout_seconds,
        ))
        log.info(
            "планировщик цикла: ответил за %.1f с; промпт %d знаков, системный %d, ответ %d знаков, размышления %s",
            monotonic() - started, len(prompt), len(system), len(reply), settings.planner_reasoning_effort,
        )
        return reply
    except TimeoutError:
        # Время ответа растёт с размером состояния: без этих двух чисел таймаут на стенде не разобрать.
        log.warning(
            "планировщик цикла: модель не ответила за %.1f с (предел %s с); промпт %d знаков, системный %d",
            monotonic() - started, settings.planner_model_timeout_seconds, len(prompt), len(system),
        )
        raise


async def complete_plan(system: str, prompt: str) -> str:
    """Вызов модели для фазы планирования: тот же шлюз, ответ — только `{plan, files}`."""
    return await _default_complete(system, prompt, _PLAN_RESPONSE_FORMAT)


class Rejection(str):
    """Причина отказа решению. Служба читает её как строку; `code` — код отказа §3.0 для ouroboros."""

    code: str

    def __new__(cls, reason: str, code: str = "неверные_аргументы") -> Rejection:
        text = super().__new__(cls, reason)
        text.code = code
        return text


class LoopSession:
    """Состояние одного прогона цикла и правила, которые его держат (ADR-0023).

    Ведущий — не здесь: решение приходит либо от модели службы (`DynamicRun`), либо вызовом
    инструмента ouroboros (`assistant/loop_tools.py`). Оба пути зовут одни и те же `check`, `move`,
    `finish`, поэтому правил цикла в одном месте столько же, сколько было.
    """

    def __init__(
        self, *, runtime: CaseRuntime, owner_id, case_id: str, goal: GoalIR, target: Target,
        inputs: RunInputs, run_passport: RunPassport | None = None, root_extra=None,
        event_sink=None, run_scenario: ScenarioRunner | None = None,
        run_goals: GoalRunner | None = None, author: AtomAuthor | None = None,
        limits: RunLimits = RunLimits(), entry_stage: Literal["planning", "execution"] = "planning",
        is_cancelled: Callable[[], bool] | None = None,
    ):
        self.runtime = runtime
        self.owner_id = owner_id
        self.case_id = case_id
        self.goal = goal
        self.target = target
        self.inputs = inputs
        self.run_passport = run_passport
        self.root_extra = root_extra or {}
        self.event_sink = event_sink
        self.author = author or AtomAuthor()
        self.limits = limits
        self.entry_stage = entry_stage
        self.is_cancelled = is_cancelled
        self.run_scenario = run_scenario
        self.run_goals = run_goals
        # goal_ref → «получена» / «отказ по ссылке: …» по последнему запуску части в этом прогоне
        self.goal_status: dict[str, str] = {}
        self.atoms = load_case_atoms(runtime.store, owner_id, case_id)
        self.failures: dict[str, str] = {}
        self.journal: list[JournalEntry] = []
        self.created = 0
        self.turns = 0                       # потрачено ходов: и принятые решения, и отклонённые ответы
        self.idle = 0                        # ходов подряд без новых результатов
        self.rejected = 0                    # ответов подряд, не прошедших проверку
        self.opened_at = monotonic()
        self.closed = False                  # последнее слово сказано: ходить и завершать больше нельзя
        self.feedback = ""                   # «Итог прошлого хода» для следующего решения
        self.evidence_generation = 0
        self.dead_end_actions: dict[tuple[str, ...], int] = {}
        self.plan: tuple[str, ...] = ()
        self.expected_files: tuple[FileSpec, ...] = ()
        self.build_attempts = 0
        self.initial_evidence_ids = self._evidence_ids()
        self.initial_file_ids = self._file_ids()

    def open(self, *, deterministic_error: str) -> None:
        """Цикл открыт: время идёт с этой минуты; причина входа — в «Итоге прошлого хода»."""
        self.opened_at = monotonic()
        if self.entry_stage == "execution":
            # «Нулевой ход»: граф собрался и упал. Итог читается так же, как итог хода цикла; уже
            # полученные им артефакты планировщик видит в таблице состояния.
            self.feedback = (
                "Детерминированный граф собрался, но исполнение упало с ошибкой. Уже полученные "
                f"результаты — в таблице артефактов.\n{deterministic_error}"
            )
            headline = deterministic_error.splitlines()[0] if deterministic_error else "исполнение упало"
            self.journal.append(JournalEntry("стандартный граф", "детерминированный путь не дал ответа", headline))
            # Р14.4: причина входа — первая карточка ленты «Планировщик и автор»; ходы цикла по-прежнему с 1.
            self._emit(dynamic_move_event(
                move=0, status="done", action="entry", objective="Стандартный граф упал при исполнении",
                reason=next((line for line in deterministic_error.splitlines() if line.startswith("✗")), headline),
                details=deterministic_error,
            ))
        else:
            self.feedback = f"Детерминированный планировщик не собрал план: {deterministic_error}"

    def limit(self) -> str | None:
        """Причина, по которой цикл исчерпан и принимает только последнее слово; `None` — ходить можно."""
        if self.rejected >= self.limits.idle_moves:
            return f"{self.rejected} ответа подряд не прошли проверку"
        if self.idle >= self.limits.idle_moves:
            return f"{self.idle} хода подряд без новых результатов"
        if self.turns >= self.limits.max_moves:
            return "исчерпан лимит ходов"
        if monotonic() - self.opened_at > self.limits.max_wall_seconds:
            return "исчерпан бюджет времени цикла"
        return None

    @property
    def next_number(self) -> int:
        return self.turns + 1

    def reject(self, reason: str, *, counted: bool = True, feedback: str | None = None) -> None:
        """Ответ не принят: ход потрачен, причина — в ленту и в «Итог прошлого хода». Молчание модели
        (таймаут, сбой шлюза) в счёт отказов подряд не идёт."""
        self._emit_rejection(self.next_number, reason)
        self.turns += 1
        self.rejected += 1 if counted else 0
        self.feedback = feedback if feedback is not None else f"Твой ответ не принят: {reason}. Пришли ход заново."

    def check(self, decision: Decision, *, last_word: bool = False) -> Decision | Rejection:
        """Правила решения: что можно исполнить или принять. `Rejection` — причина отказа и его код §3.0."""
        verdict = self._verdict(decision, last_word)
        return Rejection(verdict) if type(verdict) is str else verdict

    def _verdict(self, decision: Decision, last_word: bool) -> Decision | str:
        self._accept_plan(decision.plan, decision.files)
        known = [clause.goal_ref for clause in self.goal.clauses()]
        unknown = [ref for ref in decision.goals if ref not in known]
        if unknown:
            return f"goals: нет части {unknown}; в блоке «Разобранная цель» есть {known} — укажи goal_ref оттуда"
        if decision.action in ("finalize", "give_up"):
            decision = self._files_as_deliverables(decision)
        if decision.action == "finalize":
            # После остановки цикла собрать файл уже нельзя: последнее слово проходит без него.
            promised = decision if last_word else self._with_promised_files(decision)
            if isinstance(promised, str):
                return Rejection(promised, "файл_не_собран")
            decision = promised
            if not decision.evidence_ids and not decision.deliverables:
                return (
                    "evidence_ids пуст: перечисли id артефактов (первый столбец таблицы «Полученные "
                    "артефакты»), на которые опирается ответ; готовый файл-результат укажи в deliverables; "
                    "если опереться не на что — give_up с причиной"
                )
            missing = [evidence_id for evidence_id in decision.evidence_ids if not self._evidence(evidence_id)]
            if missing:
                return (
                    f"finalize ссылается на несуществующие артефакты: {missing}; id артефакта стоит в первом "
                    "столбце таблицы «Полученные артефакты», строки с «—» в первом столбце не цитируются — "
                    "перечисли id показателей, на которые опирается ответ"
                )
            missing_files = [file_id for file_id in decision.deliverables if self._deliverable(file_id) is None]
            if missing_files:
                return (
                    f"finalize ссылается на несуществующие файлы: {missing_files}; id файла — первый столбец "
                    "строки FileArtifact в таблице «Полученные артефакты»"
                )
            failed = self._failed_verifications(decision.evidence_ids)
            if failed:
                details = "; ".join(
                    f"{finding.check_kind}: {finding.detail or finding.status}" for finding in failed
                )
                return f"finalize ссылается на проваленную проверку {details}"
        repeat = self._repeated_dead_end(decision)
        if repeat:
            return Rejection(repeat, "повтор_тупика")
        return decision

    def move(self, decision: Decision) -> MoveOutcome:
        """Исполнить принятое решение `execute`: события хода, журнал, счётчики цикла."""
        number = self.next_number
        self.rejected = 0
        self._emit(self._move_event(number, "running", decision))
        before = self._artifact_count()
        outcome = self._move(decision)
        self._emit(self._move_event(
            number, "done", decision, outcome=outcome.headline, details=outcome.details,
        ))
        if self._runs_code_atom(decision):
            self.build_attempts += 1
        factual_outcome = _outcome_text(outcome)
        self.journal.append(JournalEntry(decision.objective, decision.reason, factual_outcome))
        self.feedback = factual_outcome
        after = self._artifact_count()
        if after != before:
            self.evidence_generation += 1
        for signature in outcome.dead_end_actions:
            self.dead_end_actions[signature] = self.evidence_generation
        self.idle = self.idle + 1 if after == before else 0
        self.turns += 1
        return outcome

    def stop_feedback(self, reason: str) -> str:
        return (
            f"Цикл остановлен системой: {reason}. Исполнять новые действия нельзя. "
            "Верни finalize по имеющимся артефактам либо give_up с конкретной причиной."
        )

    def finish(self, decision: Decision, *, number: int | None = None) -> DynamicRunResult:
        """Последнее решение цикла (`finalize` или `give_up`)."""
        self.rejected = 0
        result = self._finish(number or self.next_number, decision)
        self.closed = True
        return result

    def stopped(self, reason: str) -> DynamicRunResult:
        """Последнее слово не принято: сводка по частичным результатам, как у цикла, остановленного системой."""
        self._emit_rejection(self.next_number, reason)
        evidence_ids = self._partial_evidence_ids()
        answer = self._stopped_summary(evidence_ids)
        self.closed = True
        return self._result("stopped", answer, evidence_ids, self._partial_deliverable_ids())

    def _accept_plan(self, plan: tuple[str, ...], files: tuple[FileSpec, ...]) -> None:
        # План — заявление планировщика о намерении: он принимается и тогда, когда сам ход будет отклонён.
        if files:
            self.expected_files = tuple({spec.kind: spec for spec in (*self.expected_files, *files)}.values())
        if plan:
            self.plan = plan
            self._emit(dynamic_plan_event(plan=plan, files=[spec.text() for spec in self.expected_files]))

    def _stop_if_cancelled(self) -> None:
        """Пользователь остановил ход: рабочий поток доживает сам, но новые ходы модели тратить не на кого."""
        if self.is_cancelled is not None and self.is_cancelled():
            raise asyncio.CancelledError

    def _finish(self, number: int, decision: Decision) -> DynamicRunResult:
        self._emit(self._move_event(number, "done", decision, details=decision.answer or self._without_answer(decision)))
        status = "finalized" if decision.action == "finalize" else "gave_up"
        evidence_ids = decision.evidence_ids
        deliverable_ids = decision.deliverables
        if status == "gave_up":
            evidence_ids = self._without_refuted(tuple(dict.fromkeys((*evidence_ids, *self._partial_evidence_ids()))))
            # Найденное не выбрасывается: файл, собранный в этом прогоне, доходит до пользователя и при отказе.
            deliverable_ids = tuple(dict.fromkeys((*deliverable_ids, *self._partial_deliverable_ids())))
        return self._result(status, decision.answer, evidence_ids, deliverable_ids)

    def _without_answer(self, decision: Decision) -> str:
        """Итог финального хода, когда ответ пишет ouroboros (ADR-0044 п. 2): файлы или «ответ — в чате»."""
        files = [file.filename for file_id in decision.deliverables if (file := self._deliverable(file_id))]
        return f"файлы: {', '.join(files)}" if files else "ответ — в чате"

    def _emit_rejection(self, number: int, details: str) -> None:
        self._emit(dynamic_move_event(move=number, status="rejected", action="rejected", details=details))

    def _partial_evidence_ids(self) -> tuple[str, ...]:
        """Целевые результаты прогона без значений, проваливших проверку: сводка не обходит гейт finalize."""
        obtained = tuple(dict.fromkeys(
            evidence_id
            for row in self.runtime.store.list_artifacts(self.owner_id, self.case_id)
            if row["output_type"] not in _INFRASTRUCTURE_OUTPUT_TYPES
            for payload in row.get("payloads") or ()
            if (evidence_id := evidence_id_of(payload))
            if evidence_id not in self.initial_evidence_ids
        ))
        return self._without_refuted(obtained)[:self.limits.max_terminal_evidence]

    def _partial_deliverable_ids(self) -> tuple[str, ...]:
        """Файлы этого прогона (до его начала в Case их не было): обещанные планом, а без них — все собранные."""
        promised = tuple(file.artifact_id for file in self._promised_files())
        return promised or tuple(sorted(self._file_ids() - self.initial_file_ids))

    def _promised_files(self) -> list[FileArtifact]:
        found = (self.promised_file(spec)[0] for spec in self.expected_files)
        return [file for file in found if file is not None]

    def _without_refuted(self, evidence_ids: tuple[str, ...]) -> tuple[str, ...]:
        refuted = {finding.source_ref for finding in self._failed_verifications(evidence_ids)}
        return tuple(evidence_id for evidence_id in evidence_ids if evidence_id not in refuted)

    def _stopped_summary(self, evidence_ids: tuple[str, ...]) -> str:
        # Причина остановки — для планировщика (`_stop`), пользователю она ни о чём не говорит.
        files = self._promised_files()
        lines = [f"Файл собран: {file.filename} ({file.summary or file.file_kind})." for file in files] or [
            "Не удалось получить все запрошенные данные."
        ]
        evidence = [item for evidence_id in evidence_ids for item in self._evidence(evidence_id)]
        if evidence:
            lines.append("Полученные результаты:")
            lines.extend(f"- {line}" for line in dict.fromkeys(_describe_evidence(item) for item in evidence))
        else:
            lines.append("Целевых результатов получить не удалось.")
        return "\n".join(lines)

    def _emit(self, event: dict) -> None:
        if self.event_sink is not None:
            self.event_sink(event)

    def _move_event(self, number: int, status: str, decision: Decision, *, outcome: str = "", details: str = "") -> dict:
        clauses = {clause.goal_ref: clause for clause in self.goal.clauses()}
        return dynamic_move_event(
            move=number, status=status, action=decision.action, objective=decision.objective,
            reason=decision.reason, scenarios=decision.scenarios, outcome=outcome, details=details,
            goals=[
                {"goal_ref": ref, "description": describe_clause(clauses[ref])}
                for ref in decision.goals if ref in clauses
            ],
            new_atoms=[
                {
                    "name": request.name, "kind": "download" if request.is_download else "code",
                    "purpose": request.purpose, "output_type": request.output_type,
                    "url": request.url or None,
                }
                for request in decision.new_atoms
            ],
            steps=[
                {"atom": step.get("atom"), "concept": step.get("concept"), "period_ref": step.get("period_ref")}
                for step in decision.raw_steps
            ],
        )

    def _report_author_attempt(self, atom: str, number: int, status: str, error: str | None, code: str) -> None:
        self._emit(author_attempt_event(
            move=self.next_number, atom=atom, attempt=number, status=status, error=error, code=code,
        ))

    def _move(self, decision: Decision) -> MoveOutcome:
        known = self._progress_ids()
        lines = []
        failed = False
        dead_end_actions: list[tuple[str, ...]] = []
        if decision.goals:
            self._stop_if_cancelled()
            before_goals = self._progress_ids()
            result = self._run_goal_refs(decision.goals)
            lines.append(f"части цели {', '.join(decision.goals)}: {result.summary}")
            failed = failed or not result.ok
            if self._progress_ids() == before_goals:
                dead_end_actions.extend(_goal_signature(ref) for ref in decision.goals)
        for text in decision.scenarios:
            self._stop_if_cancelled()
            before_scenario = self._progress_ids()
            try:
                result = self.run_scenario(text)
                lines.append(f"сценарий «{text}»: {result.summary}")
                failed = failed or not result.ok
                if self._progress_ids() == before_scenario:
                    dead_end_actions.append(_scenario_signature(text))
            except BudgetExceeded:
                raise
            except Exception as exc:  # noqa: BLE001 — отказ сценария возвращается планировщику
                lines.append(f"сценарий «{text}»: ✗ {type(exc).__name__}: {exc}")
                failed = True
        empty_atoms: tuple[str, ...] = ()
        transient = False
        refusal = ""
        if decision.new_atoms or decision.raw_steps:
            before_steps = self._progress_ids()
            steps = self._run_steps(decision)
            lines.append(
                _outcome_text(steps)
                if steps.failed and steps.headline != "ход завершён с ошибками"
                else steps.details
            )
            failed = failed or steps.failed
            empty_atoms = steps.empty_atoms
            transient = steps.transient
            # Ход целиком отклонён правилом атома, только если кроме шагов в нём ничего не исполнялось.
            refusal = steps.refusal if not decision.goals and not decision.scenarios else ""
            if self._progress_ids() == before_steps and not transient and not steps.steps_rejected:
                # шаг атома, которого не создали, не исполнялся — повтором его правка не считается;
                # набор, отклонённый проверкой шагов, не исполнялся целиком
                ran = tuple(
                    item for item in decision.raw_steps
                    if str(item.get("atom") or "") not in steps.uncreated_atoms
                )
                dead_end_actions.extend(_step_signatures(ran))
        produced = len(self._progress_ids() - known)
        if failed:
            headline = "ход завершён с ошибками"
        elif produced:
            headline = f"✓ {produced} новых результатов"
        elif empty_atoms:
            headline = f"результатов нет: {', '.join(empty_atoms)} вернули пусто"
        else:
            headline = "новых результатов нет, всё уже было получено"
        return MoveOutcome(
            headline, "\n".join(lines), failed=failed, empty_atoms=empty_atoms,
            dead_end_actions=tuple(dead_end_actions), transient=transient, refusal=refusal,
        )

    def _run_goal_refs(self, refs: tuple[str, ...]) -> ScenarioResult:
        """Части разобранной цели по ссылке — тем же конвейером, что сообщение чата (Р18.3). Отказ с текстом,
        из которого следует действие, возвращается планировщику, цикл внутри цикла не создаётся (Р18.4).
        Статус «отказ по ссылке» — только у частей, которые конвейер действительно пробовал исполнить."""
        chosen = [clause for clause in self.goal.clauses() if clause.goal_ref in refs]    # порядок разбора, не ответа
        for clause in chosen:
            reason = self.run_goals.refusal(clause)
            if reason is not None:
                return ScenarioResult(False, (
                    f"✗ часть {clause.goal_ref} ({clause.intent_kind}) по ссылке не исполняется: {reason}. "
                    "Получи её сам: шагами каталога или новым атомом из уже полученных результатов; части, "
                    "от которых она зависит, получи по ссылке."
                ))
        for clause in chosen:
            missing = [dep for dep in clause.depends_on if dep not in refs and self.goal_status.get(dep) != GOAL_OBTAINED]
            if missing:
                return ScenarioResult(False, (
                    f"✗ часть {clause.goal_ref} зависит от части {missing[0]}, которая ещё не получена: назови "
                    f"{missing[0]} в goals этого же хода либо получи её отдельным ходом по ссылке."
                ))
        result = self.run_goals(chosen)
        if result.ok:
            self.goal_status.update({clause.goal_ref: GOAL_OBTAINED for clause in chosen})
            return result
        return self._goal_refused(chosen, ScenarioResult(False, (
            f"{result.summary}\nПо ссылке часть не получена; ту же ссылку не повторяй. Показатель, который "
            "есть в «Реестре показателей», получи сценарием в `scenarios` (рассчитываемый — «Рассчитай …», "
            "извлекаемый — «Извлеки …»), не шагами locate и read.value: то, чего не нашёл конвейер, они не найдут. "
            "Показателя нет в реестре — новый атом либо give_up."
        )))

    def _goal_refused(self, chosen: list[GoalClause], result: ScenarioResult) -> ScenarioResult:
        reason = result.summary.splitlines()[0][:160]
        self.goal_status.update({clause.goal_ref: f"отказ по ссылке: {reason}" for clause in chosen})
        return result

    def _run_steps(self, decision: Decision) -> MoveOutcome:
        atom_problems: dict[str, str] = {}
        for request in decision.new_atoms:
            problem = self._ensure_atom(request)
            if problem:
                atom_problems[request.name] = problem
        raw_steps = tuple(
            item for item in decision.raw_steps
            if str(item.get("atom") or "") not in atom_problems
        )
        problems = "\n".join(f"{name}: {problem}" for name, problem in atom_problems.items())
        if not raw_steps:
            return MoveOutcome(
                f"атомы не созданы: {', '.join(atom_problems)}", problems, failed=True,
                transient=any("комната станции недоступна" in problem for problem in atom_problems.values()),
                uncreated_atoms=tuple(atom_problems),
                refusal=next((code for code in map(_refusal_code, atom_problems.values()) if code), ""),
            )
        known = self._progress_ids()
        runtime = self.runtime.with_atoms([self._atom_def(atom) for atom in self.atoms.values()])
        try:
            steps = parse_steps(raw_steps, runtime.registry)
            planned = runtime.plan_steps(
                owner_id=self.owner_id, case_id=self.case_id, goal_ir=self.goal, target=self.target,
                steps=steps, run_passport=self.run_passport, root_extra=self.root_extra,
            )
            result = runtime.execute(
                owner_id=self.owner_id, case_id=self.case_id, plan_run_id=planned.record.plan_run_id,
                inputs=self.inputs, event_sink=partial(_progress_only, self.event_sink),
            )
        except InvalidSteps as exc:
            return MoveOutcome(
                "шаги отклонены", str(exc), failed=True, uncreated_atoms=tuple(atom_problems), steps_rejected=True,
            )
        except PlanInfeasible as exc:
            return MoveOutcome("план не собрался", str(exc), failed=True, uncreated_atoms=tuple(atom_problems))
        except TimeoutError as exc:
            return MoveOutcome("ход прерван по времени", str(exc), failed=True, transient=True)
        self._remember_failures(result)
        outcome = describe_execution(result, known)
        outcome = replace(outcome, details=outcome.details + _web_off_note(outcome.empty_atoms, runtime.registry))
        transient = _has_station_fault(result)
        if atom_problems:
            return MoveOutcome(
                f"{outcome.headline}; {len(atom_problems)} атомов не создано",
                f"{outcome.details}\n{problems}",
                failed=True,
                empty_atoms=outcome.empty_atoms,
                transient=transient or any(
                    "комната станции недоступна" in problem for problem in atom_problems.values()
                ),
                uncreated_atoms=tuple(atom_problems),
            )
        return MoveOutcome(
            outcome.headline, outcome.details, failed=outcome.failed,
            empty_atoms=outcome.empty_atoms, transient=transient,
        )

    def _atom_def(self, atom: EphemeralAtom) -> AtomDef:
        """Запись реестра для графа. Вход «любой показатель» (тип без `concept`) раскрывается в показатели,
        которые уже есть в Case: иначе граф достраивает чтение документа без показателя, и оно падает."""
        definition = to_atom_def(atom)
        concepts: dict[str, list[str]] = {}
        for row in self.runtime.store.list_artifacts(self.owner_id, self.case_id):
            if row["concept"] and row["concept"] not in concepts.setdefault(row["output_type"], []):
                concepts[row["output_type"]].append(row["concept"])
        requires = tuple(dict.fromkeys(
            expanded
            for spec in atom.request.requires
            for expanded in (
                [InputSpec(spec.input_type, concept) for concept in concepts[spec.input_type]]
                if spec.concept is None and concepts.get(spec.input_type) else [spec]
            )
        ))
        return replace(definition, requires=requires)

    def _ensure_atom(self, request: AtomRequest) -> str | None:
        known = self.atoms.get(request.name)
        if known and known.request == request and request.name not in self.failures:
            return None
        if known is None and self.created >= self.limits.max_new_atoms:
            return "исчерпан лимит новых атомов — используй существующие"
        problems = check_request(request, self.runtime.registry)
        if problems:
            return "контракт отклонён: " + "; ".join(problems)
        if request.is_download:
            atom, error = EphemeralAtom(request=request), None
        else:
            real = case_inputs(
                self.runtime.store, self.owner_id, self.case_id, request, atoms=self.atoms,
            )
            if real is None:
                needs = ", ".join(f"{spec.input_type}:{spec.concept or '*'}" for spec in request.requires)
                return (
                    f"входов атома {request.name} в Case ещё нет (нужны: {needs}) — получи их "
                    "сценарием или шагом, а атом с кодом опиши следующим ходом"
                )
            observer: _AuthorExtras = (
                {
                    "on_attempt": partial(self._report_author_attempt, request.name),
                    "expected_files": self.expected_files,
                    "trace_id": self.inputs.extra.get("trace_id", ""),
                }
                if isinstance(self.author, AtomAuthor) else {}
            )
            atom, error = self.author.write(
                request, inputs_doc=describe_inputs(request, self.atoms), real_inputs=real,
                previous_code=known.code if known else None, error=self.failures.get(request.name),
                **observer,
            )
        if isinstance(error, StationFault):
            return f"комната станции недоступна, код не проверить: {error}"
        if atom is None:
            return f"автор не написал рабочий код за {self.author.attempts} попытки: {error}"
        self.failures.pop(request.name, None)
        if known is None:
            self.created += 1
        self.atoms[request.name] = atom
        save_source(self.runtime.store, self.owner_id, self.case_id, atom)
        return None

    def _files_as_deliverables(self, decision: Decision) -> Decision:
        """Модель может назвать id готового файла в evidence_ids: файл — результат ответа, а не доказательство."""
        file_ids = self._file_ids()
        cited = tuple(item for item in decision.evidence_ids if item in file_ids)
        if not cited:
            return decision
        return replace(
            decision,
            evidence_ids=tuple(item for item in decision.evidence_ids if item not in file_ids),
            deliverables=tuple(dict.fromkeys((*decision.deliverables, *cited))),
        )

    def _with_promised_files(self, decision: Decision) -> Decision | str:
        """Гейт файла: ответ с обещанным в плане файлом. Собранный файл уходит в ответ сам; несобранный —
        отказ с тем, что делать, пока цикл не потратил на него `_FILE_BUILD_ATTEMPTS` ходов с кодом атома."""
        attached = list(decision.deliverables)
        for spec in self.expected_files:
            file, problem = self.promised_file(spec)
            if file is not None:
                if not any(self._kind_of(file_id) == spec.kind for file_id in attached):
                    attached.append(file.artifact_id)
            elif self.build_attempts < _FILE_BUILD_ATTEMPTS:
                if problem:
                    return f"{problem}; исправь атом — пришли его описание с тем же name и запусти шагом"
                return (
                    f"по плану ответ отдаёт файл {spec.text()}, а в этом прогоне он не собран: опиши атом с кодом "
                    "в new_atoms (входы — уже полученные показатели, структура файла — в purpose) и вызови его "
                    "шагом этого же хода; out.file для этого не годится — он выгружает форму одним листом без формул"
                )
        return replace(decision, deliverables=tuple(attached))

    def promised_file(self, spec: FileSpec) -> tuple[FileArtifact | None, str | None]:
        """(файл этого прогона, отвечающий плану, None) либо (None, чем беднее плана последний собранный)."""
        built = [
            file for file in self._file_artifacts()
            if file.artifact_id not in self.initial_file_ids and file.file_kind == spec.kind and file.size_bytes > 0
        ]
        problem = None
        for file in reversed(built):
            found = spec_problem(file.filename, file.summary or "", spec)
            if found is None:
                return file, None
            problem = problem or found
        return None, problem

    def _kind_of(self, file_id: str) -> str | None:
        file = self._deliverable(file_id)
        return file.file_kind if file is not None else None

    def _runs_code_atom(self, decision: Decision) -> bool:
        code_atoms = {request.name for request in decision.new_atoms if not request.is_download}
        code_atoms |= {name for name, atom in self.atoms.items() if not atom.request.is_download}
        return any(str(step.get("atom") or "") in code_atoms for step in decision.raw_steps)

    def _repeated_dead_end(self, decision: Decision) -> str | None:
        repeated = [
            signature for signature in _decision_signatures(decision)
            if self.dead_end_actions.get(signature) == self.evidence_generation
            and not self._rewrites_ephemeral(decision, signature)
        ]
        if not repeated:
            return None
        actions = ", ".join(_signature_text(signature) for signature in repeated)
        return (
            f"ход точно повторяет тупиковое действие без новых результатов: {actions}. "
            "Смени путь, используй новые evidence либо заверши задачу."
        )

    def _rewrites_ephemeral(self, decision: Decision, signature: tuple[str, ...]) -> bool:
        if signature[0] != "step" or not signature[1].startswith("eph."):
            return False
        request = next((item for item in decision.new_atoms if item.name == signature[1]), None)
        known = self.atoms.get(signature[1])
        return request is not None and known is not None and (
            request != known.request or signature[1] in self.failures
        )

    def _result(self, status, answer, evidence_ids, deliverable_ids=()) -> DynamicRunResult:
        evidence = []
        seen: set[str] = set()
        for evidence_id in evidence_ids:
            for item in self._evidence(evidence_id):
                item_id = evidence_id_of(item)
                if item_id and item_id in seen:
                    continue
                if item_id:
                    seen.add(item_id)
                evidence.append(item)
        deliverables = tuple(
            file for file_id in dict.fromkeys(deliverable_ids) if (file := self._deliverable(file_id)) is not None
        )
        return DynamicRunResult(status, answer, tuple(evidence), tuple(self.journal), deliverables)

    def _evidence(self, evidence_id: str) -> list:
        # `find_evidence` разрешает и служебные id результатов (`result_id_of`: документ, паспорт,
        # локатор) — они нужны NodeRun, но доказательством ответа служат только доменные evidence.
        found = self.runtime.store.find_evidence(self.owner_id, self.case_id, evidence_id)
        return [item for item in found if evidence_id_of(item)]

    def _failed_verifications(self, evidence_ids: tuple[str, ...]) -> tuple[VerificationFinding, ...]:
        selected = set(evidence_ids)
        findings: dict[str, VerificationFinding] = {}
        for artifact in self.runtime.store.list_artifacts(self.owner_id, self.case_id):
            for item in artifact.get("payloads") or ():
                if (
                    isinstance(item, VerificationFinding) and item.status == "fail"
                    and (item.evidence_id in selected or item.source_ref in selected)
                ):
                    findings[item.evidence_id] = item
        return tuple(findings.values())

    def _remember_failures(self, result: ExecutionResult) -> None:
        for key, node_run in result.node_runs.items():
            if (
                key.atom_name.startswith("eph.") and node_run.status == "failed" and node_run.error
                and not node_run.error.startswith(STATION_FAULT_PREFIX)
            ):
                atom = self.atoms.get(key.atom_name)
                if atom is not None and not atom.request.is_download:
                    self.failures[key.atom_name] = node_run.error[-800:]

    def _artifact_count(self) -> int:
        return len(self._progress_ids())

    def _progress_ids(self) -> frozenset[str]:
        """Что считается продвижением цикла: доменные evidence и собранные файлы (у файла `evidence_id` нет)."""
        return self._evidence_ids() | self._file_ids()

    def _file_artifacts(self) -> list[FileArtifact]:
        rows = self.runtime.store.list_artifacts(self.owner_id, self.case_id)
        return [
            payload for row in rows if row["output_type"] == "FileArtifact"
            for payload in row.get("payloads") or () if isinstance(payload, FileArtifact)
        ]

    def _file_ids(self) -> frozenset[str]:
        return frozenset(file.artifact_id for file in self._file_artifacts())

    def _deliverable(self, file_id: str) -> FileArtifact | None:
        """Файл, на который можно сослаться в ответе: есть в Case этого прогона и не пуст."""
        return next(
            (file for file in self._file_artifacts() if file.artifact_id == file_id and file.size_bytes > 0), None,
        )

    def _evidence_ids(self) -> frozenset[str]:
        rows = self.runtime.store.list_artifacts(self.owner_id, self.case_id)
        return frozenset(
            evidence_id for row in rows if row["output_type"] not in _INFRASTRUCTURE_OUTPUT_TYPES
            for payload in row.get("payloads") or ()
            if (evidence_id := evidence_id_of(payload))
        )


class DynamicRun(LoopSession):
    """Ведущий цикла — модель службы (`loop_driver=engine`): ответ модели → `check` → `move`/`finish`."""

    def __init__(self, *, complete_fn: CompleteFn | None = None, plan_fn: CompleteFn | None = None, **session):
        super().__init__(**session)
        self.complete_fn = complete_fn or _default_complete
        self.plan_fn = plan_fn

    def run(self, *, deterministic_error: str) -> DynamicRunResult:
        """Выполнить синхронный цикл; из async-кода вызывать через `asyncio.to_thread`."""
        self.open(deterministic_error=deterministic_error)
        self._draft_plan(self.feedback)
        while True:
            self._stop_if_cancelled()
            reason = self.limit()
            if reason is not None:
                return self._stop(reason)
            decision = self._ask_planner(self.feedback)
            if isinstance(decision, str):
                self.reject(decision, counted=not isinstance(decision, PlannerSilent))
            elif decision.action in ("finalize", "give_up"):
                return self.finish(decision)
            else:
                self.move(decision)

    def _draft_plan(self, feedback: str) -> None:
        """Фаза планирования: модель видит всё состояние и возвращает только план — он уходит на экран до
        первого хода. Не получилось — цикл идёт как раньше: план придёт первым ходом."""
        if self.plan_fn is None or not any(
            clause.intent_kind in _PLAN_PHASE_GOALS for clause in self.goal.clauses()
        ):
            return
        system = _SKILL.read_text(encoding="utf-8") + "\n\n" + _PLAN_PHASE.read_text(encoding="utf-8")
        try:
            data = json.loads(asyncio.run(self.plan_fn(system, render_state(self, feedback))))
            plan = _string_tuple(data.get("plan", []), "plan")
            files = _file_specs(data.get("files", []))
        except BudgetExceeded:
            raise
        except Exception:  # noqa: BLE001 — без плана цикл работает: сбой фазы не останавливает прогон
            return
        self._accept_plan(plan, files)

    def _stop(self, reason: str) -> DynamicRunResult:
        decision = self._ask_planner(self.stop_feedback(reason), last_word=True)
        if isinstance(decision, Decision) and decision.action in ("finalize", "give_up"):
            return self.finish(decision)
        return self.stopped(
            decision if isinstance(decision, str) else "после остановки цикла исполнять действия нельзя"
        )

    def _ask_planner(self, feedback: str, *, last_word: bool = False) -> Decision | str:
        prompt = render_state(self, feedback)
        try:
            raw = asyncio.run(self.complete_fn(_SKILL.read_text(encoding="utf-8"), prompt))
        except BudgetExceeded:
            raise
        except Exception as exc:  # noqa: BLE001 — сбой шлюза становится обратной связью хода
            return PlannerSilent(f"модель-планировщик не ответила: {type(exc).__name__}: {exc}")
        try:
            decision = parse_decision(
                raw, scenarios_enabled=self.run_scenario is not None, goals_enabled=self.run_goals is not None,
            )
        except ValueError as exc:
            return str(exc)
        return self.check(decision, last_word=last_word)


class InvalidSteps(ValueError):
    """Шаги ответа модели не прошли детерминированную проверку."""


class PlannerSilent(str):
    """Модель-планировщик не ответила (таймаут, сбой шлюза): обратная связь хода, но не отклонённый ответ."""


def parse_decision(raw: str, *, scenarios_enabled: bool, goals_enabled: bool = False) -> Decision:
    try:
        data = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"ответ не является JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("ответ — не JSON-объект")
    return decision_from_data(data, scenarios_enabled=scenarios_enabled, goals_enabled=goals_enabled)


def decision_from_data(
    data: dict, *, scenarios_enabled: bool, goals_enabled: bool = False, answer_in_chat: bool = False,
) -> Decision:
    """Решение из разобранного объекта — путь модели службы (`parse_decision`) и путь аргументов инструмента.

    `answer_in_chat` — ответ пользователю пишет ouroboros, а не решение (ADR-0044 п. 2): `finalize` не
    требует `answer`, `give_up` не подменяет его причиной."""
    objective = str(data.get("objective") or "").strip()
    reason = str(data.get("reason") or "").strip()
    action = data.get("action")
    if not objective:
        raise ValueError("поле objective пусто")
    if not reason:
        raise ValueError("поле reason пусто")
    if action not in ("execute", "finalize", "give_up"):
        raise ValueError(f"неизвестное action={action!r}")
    scenarios = _string_tuple(data.get("scenarios", []), "scenarios")
    goals = tuple(dict.fromkeys(_string_tuple(data.get("goals", []), "goals")))
    if len(scenarios) + len(goals) > MAX_MOVE_ACTIONS:
        raise ValueError("за ход разрешено не больше трёх действий: сценарии и части цели (goals) вместе")
    if scenarios and not scenarios_enabled:
        raise ValueError("сценарии недоступны в этом запуске")
    if goals and not goals_enabled:
        raise ValueError("части цели недоступны в этом запуске: получай их сценариями или шагами")
    raw_steps = data.get("steps", [])
    if not isinstance(raw_steps, list) or any(not isinstance(item, dict) for item in raw_steps):
        raise ValueError("steps должен быть списком объектов")
    raw_atoms = data.get("new_atoms", [])
    if not isinstance(raw_atoms, list):
        raise ValueError("new_atoms должен быть списком")
    new_atoms = tuple(_parse_atom_request(item, index) for index, item in enumerate(raw_atoms))
    answer = str(data.get("answer") or "").strip()
    evidence_ids = _string_tuple(data.get("evidence_ids", []), "evidence_ids")
    deliverables = _string_tuple(data.get("deliverables", []), "deliverables")
    plan = _string_tuple(data.get("plan", []), "plan")
    files = _file_specs(data.get("files", []))
    # Атом описывают, чтобы запустить: шаг для объявленного, но не вызванного атома достраивается, а не
    # возвращается модели отказом (на dev 06–07.10.2026 этот отказ повторялся в каждом втором прогоне).
    used = {str(item.get("atom") or "") for item in raw_steps}
    raw_steps = [*raw_steps, *({"atom": request.name} for request in new_atoms if request.name not in used)]
    if action == "execute" and not scenarios and not goals and not raw_steps:
        raise ValueError("для execute нужен хотя бы один сценарий, часть цели (goals) или шаг")
    if action in ("finalize", "give_up") and (scenarios or goals or new_atoms or raw_steps):
        raise ValueError(f"для {action} поля scenarios, goals, new_atoms и steps должны быть пустыми")
    if action == "finalize" and not answer and not answer_in_chat:
        raise ValueError("для finalize поле answer обязательно")
    if action == "give_up" and not answer and not answer_in_chat:
        answer = reason
    return Decision(
        objective=objective, reason=reason, action=action, scenarios=scenarios, goals=goals,
        new_atoms=new_atoms, raw_steps=tuple(raw_steps), answer=answer, evidence_ids=evidence_ids,
        deliverables=deliverables, plan=plan, files=files,
    )


def _file_specs(value) -> tuple[FileSpec, ...]:
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise ValueError("files должен быть списком объектов {kind, parts, formulas}")
    specs = []
    for item in value:
        kind = str(item.get("kind") or "").strip().lower().lstrip(".")
        parts = item.get("parts") or 0
        if not _FILE_KIND.fullmatch(kind) or not isinstance(parts, int) or parts < 0:
            raise ValueError(
                f"files: kind={item.get('kind')!r}, parts={item.get('parts')!r} — kind это расширение файла "
                "(xlsx, pptx, docx), parts — целое число листов или слайдов (0 — не задано)"
            )
        specs.append(FileSpec(kind=kind, parts=parts, formulas=bool(item.get("formulas"))))
    return tuple(specs)


def _string_tuple(value, field: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) or not item.strip() for item in value):
        raise ValueError(f"{field} должен быть списком непустых строк")
    return tuple(item.strip() for item in value)


def _normalized(value) -> str:
    return " ".join(str(value or "").casefold().split())


def _scenario_signature(text: str) -> tuple[str, ...]:
    return ("scenario", _normalized(text))


def _goal_signature(ref: str) -> tuple[str, ...]:
    return ("goal", ref)


def _step_signature(item: dict) -> tuple[str, ...]:
    return (
        "step", str(item.get("atom") or "").strip(), _normalized(item.get("concept")),
        _normalized(item.get("period_ref")),
        json.dumps(item.get("extra") or {}, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
    )


def _step_signatures(items: tuple[dict, ...]) -> tuple[tuple[str, ...], ...]:
    return tuple(_step_signature(item) for item in items)


def _decision_signatures(decision: Decision) -> tuple[tuple[str, ...], ...]:
    goals = tuple(_goal_signature(ref) for ref in decision.goals)
    scenarios = tuple(_scenario_signature(text) for text in decision.scenarios)
    return goals + scenarios + _step_signatures(decision.raw_steps)


def _signature_text(signature: tuple[str, ...]) -> str:
    if signature[0] == "scenario":
        return f"сценарий «{signature[1]}»"
    if signature[0] == "goal":
        return f"часть {signature[1]}"
    concept = f" {signature[2]}" if signature[2] else ""
    period = f" за {signature[3]}" if signature[3] else ""
    return f"{signature[1]}{concept}{period}"


def _input_concept(value) -> str | None:
    """Строгая схема ответа требует ключ `concept` всегда: «без концепта» модель пишет `null`, `""` или `"*"`."""
    text = str(value).strip() if value is not None else ""
    return None if text in ("", "*") else text


def _parse_atom_request(item, index: int) -> AtomRequest:
    if not isinstance(item, dict):
        raise ValueError(f"new_atoms[{index}] — не объект")
    requires_raw = item.get("requires", [])
    if not isinstance(requires_raw, list):
        raise ValueError(f"new_atoms[{index}].requires — не список")
    try:
        requires = tuple(
            InputSpec(
                input_type=str(spec["input_type"]),
                concept=_input_concept(spec.get("concept")),
            )
            for spec in requires_raw
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"new_atoms[{index}].requires невалиден: {exc}") from exc
    url = str(item.get("url") or "").strip()
    schema = DOWNLOAD_SCHEMA if url else _usable_schema(item.get("output_schema"))
    return AtomRequest(
        name=str(item.get("name") or "").strip(), purpose=str(item.get("purpose") or "").strip(),
        output_type=str(item.get("output_type") or "").strip(), output_schema=schema,
        requires=requires, url=url,
    )


def _usable_schema(schema) -> dict:
    """Схема результата атома; отсутствующая или невалидная заменяется на «любой объект» — это не повод
    отклонять весь ход: результат всё равно объект, а у файла-результата схема ничего не решает."""
    from jsonschema.exceptions import SchemaError  # noqa: PLC0415 — не зависимость комнаты станции
    from jsonschema.validators import validator_for  # noqa: PLC0415

    if not isinstance(schema, dict):
        return {"type": "object"}
    try:
        validator_for(schema).check_schema(schema)
    except SchemaError:
        return {"type": "object"}
    return schema


def parse_steps(raw_steps, registry: AtomRegistry) -> list[AtomStep]:
    if not isinstance(raw_steps, (list, tuple)) or not raw_steps:
        raise InvalidSteps("поле steps отсутствует или пусто")
    if len(raw_steps) > MAX_STEPS:
        raise InvalidSteps(f"слишком много шагов: {len(raw_steps)} > {MAX_STEPS}")
    return [_parse_step(index, item, registry) for index, item in enumerate(raw_steps)]


def _parse_step(index: int, item: dict, registry: AtomRegistry) -> AtomStep:
    if not isinstance(item, dict):
        raise InvalidSteps(f"шаг {index} — не объект")
    atom_name = str(item.get("atom") or "").strip()
    atom = registry.get(atom_name)
    if atom is None and atom_name.startswith("eph."):
        raise InvalidSteps(
            f"шаг {index}: атом {atom_name!r} не создан — опиши его в new_atoms этого же хода "
            "(name, purpose, output_type и url либо requires) и оставь шаг с ним"
        )
    if atom is None:
        raise InvalidSteps(f"шаг {index}: неизвестный атом {atom_name!r} — используй только атомы каталога")
    if atom_name in NOT_PLANNABLE:
        raise InvalidSteps(
            f"шаг {index}: атом {atom_name!r} нельзя планировать вручную — гейты и инфраструктуру "
            "планировщик достраивает сам"
        )
    concept = item.get("concept")
    period = item.get("period_ref")
    if concept is not None and not isinstance(concept, str):
        raise InvalidSteps(f"шаг {index}: concept должен быть строкой или null")
    if period is not None and not isinstance(period, str):
        raise InvalidSteps(f"шаг {index}: period_ref должен быть строкой или null")
    extra = item.get("extra") or {}
    if not isinstance(extra, dict) or any(not isinstance(value, str) for value in extra.values()):
        raise InvalidSteps(f"шаг {index}: extra должен быть объектом со строковыми значениями")
    forbidden = sorted(set(extra) - ALLOWED_EXTRA_KEYS)
    if forbidden:
        raise InvalidSteps(
            f"шаг {index}: extra содержит недопустимые ключи {forbidden}; "
            f"разрешены {sorted(ALLOWED_EXTRA_KEYS)}"
        )
    output_types = list(dict.fromkeys(spec.output_type for spec in atom.produces))
    produces = item.get("produces")
    if produces is None:
        output_type = output_types[0]
    elif produces in output_types:
        output_type = produces
    else:
        raise InvalidSteps(f"шаг {index}: produces={produces!r} — атом {atom_name} даёт {output_types}")
    problem = atom.step_check(NodeArgs(concept=concept, period_ref=period, extra=extra)) if atom.step_check else None
    if problem:
        raise InvalidSteps(f"шаг {index}: {problem}")
    return AtomStep(
        atom=atom_name, output_type=output_type, concept=concept or None,
        period_ref=period or None, extra=dict(extra),
    )


def case_rows(store, owner_id, case_id: str) -> list[dict]:
    """Артефакты Case без исходников временных атомов — то, из чего строится таблица результатов."""
    return [row for row in store.list_artifacts(owner_id, case_id) if row["output_type"] != _SOURCE_OUTPUT_TYPE]


def artifacts_table(rows: list[dict]) -> str:
    """Строки «Полученные артефакты» для планировщика и для ouroboros (`ouro_view`): одна таблица на обоих."""
    return "\n".join(_state_table(rows)) or "—"


ARTIFACTS_TITLE = "Полученные артефакты"
ARTIFACTS_HEADER = "id | тип | концепт | период | значение"


def render_state(run: LoopSession, feedback: str) -> str:
    return "\n\n".join(f"## {title}\n{body}" for title, body in state_sections(run, feedback))


def state_sections(run: LoopSession, feedback: str) -> list[tuple[str, str]]:
    """Разделы состояния цикла (6.2 плана 34): их читает и модель службы, и ouroboros (`ouro_view`)."""
    goal = run.goal
    profile = "не зафиксирован"
    if run.run_passport is not None:
        classification = run.run_passport.classification
        profile = (
            f"стандарт={classification.standard}; тип компании={classification.company_type}; "
            f"язык={classification.language}"
        )
    rows = case_rows(run.runtime.store, run.owner_id, run.case_id)
    produced_types = {row["output_type"] for row in rows}
    atoms = "\n".join(
        _describe_atom(atom, ran=atom.request.output_type in produced_types) for atom in run.atoms.values()
    ) or "—"
    artifacts = artifacts_table(rows)
    journal = "\n".join(
        f"{index}. {entry.objective} — {entry.outcome}"
        for index, entry in enumerate(run.journal, 1)
    ) or "—"
    return [
        ("Вопрос пользователя", goal.raw_query),
        ("Разобранная цель", _describe_goal(run)),
        ("План", _describe_plan(run)),
        ("Профиль отчёта", profile),
        ("Реестр показателей", _describe_metric_registry(run)),
        ("Каталог атомов", catalog_text(run.runtime.registry)),
        ("Созданные атомы этого разговора", atoms),
        (ARTIFACTS_TITLE, f"{ARTIFACTS_HEADER}\n{artifacts}"),
        ("Журнал ходов", journal),
        ("Итог прошлого хода", feedback),
        ("Осталось", (
            f"ходов: {run.limits.max_moves - run.turns} из {run.limits.max_moves}; "
            f"новых атомов: {run.limits.max_new_atoms - run.created} из {run.limits.max_new_atoms}"
        )),
    ]


def _describe_metric_registry(run: LoopSession) -> str:
    """Показатели, которые AIFA получает сама для отчёта этого профиля: по реестру, зафиксированному в паспорте."""
    if run.run_passport is None:
        return "профиль отчёта не зафиксирован — реестр недоступен"
    registry = registry_for_passport(run.run_passport)
    classification = run.run_passport.classification
    lines = ["Извлекаемые (читаются из отчёта, если строка в нём есть):"]
    lines += [f"- {metric['enum']} — {metric['name_ru']}" for metric in registry["extractable"]]
    lines.append("Рассчитываемые (формула реестра над извлекаемыми):")
    lines += [f"- {enum} — {name} = {formula}" for enum, name, formula, _ in registry["calculated"]]
    role_plan = build_role_plan(run.goal.role, classification.company_type, classification.profile_id)
    if role_plan.output_enums:
        lines.append(f"Форма роли «{role_plan.role_label}»: {', '.join(sorted(role_plan.output_enums))}")
    return "\n".join(lines)


def _describe_plan(run: LoopSession) -> str:
    if not run.plan:
        return "плана нет — пришли `plan` и `files` этим ходом"
    lines = [f"{index}. {part}" for index, part in enumerate(run.plan, 1)]
    for spec in run.expected_files:
        file, problem = run.promised_file(spec)
        status = f"собран: {file.filename}" if file is not None else (problem or "не собран")
        lines.append(f"файл по плану: {spec.text()} — {status}")
    if run.expected_files and all(run.promised_file(spec)[0] is not None for spec in run.expected_files):
        lines.append("Все файлы по плану собраны: следующим ходом — finalize с коротким ответом, без новых расчётов.")
    return "\n".join(lines)


def _state_table(rows: list[dict]) -> list[str]:
    """Таблица артефактов для планировщика: то, на что может опереться ответ, без служебных записей.

    У каждого показателя в Case несколько записей (по одной на стадию гейтов) — остаётся конец цепочки;
    из находок проверок — только проваленные; локаторы, текст документа и паспорт не показываются.
    Полная таблица на реальном отчёте занимала 107 тыс. знаков из 114 тыс. состояния (dev 06.10.2026)."""
    answerable = [row for row in rows if row["output_type"] not in _INFRASTRUCTURE_OUTPUT_TYPES]
    lines = [_describe_artifact_evidence(row, value) for value, row in end_of_chain(answerable, {})]
    lines += [
        _describe_artifact_evidence(row, payload)
        for row in rows if row["output_type"] == "VerificationFinding"
        for payload in row["payloads"] if getattr(payload, "status", None) == "fail"
    ]
    return lines


# Как называет часть цели экран: вид без служебного имени. Вид вне словаря показывается как есть.
_GOAL_VERBS = {
    "extract_registered_metric": "Извлечь", "extract_ad_hoc_metric": "Извлечь",
    "calculate_registered_metric": "Рассчитать", "calculate_ad_hoc_metric": "Рассчитать",
    "full_analysis": "Полный анализ", "form": "Форма роли", "monitor_changes": "Изменения по периодам",
    "explain_change": "Объяснить изменение",
}


def describe_clause(clause: GoalClause) -> str:
    """Читаемое описание части цели для экрана: «Рассчитать EBITDA (все периоды)»."""
    subject = ", ".join(target.concept for target in clause.targets) or (clause.unresolved[0] if clause.unresolved else "")
    verb = _GOAL_VERBS.get(clause.intent_kind, clause.intent_kind)
    return f"{verb} {subject} ({clause.period_ref or 'все периоды'})".replace("  ", " ")


def _how_to_get(run: LoopSession, clause: GoalClause) -> str | None:
    """Способ получения части и статус в этом прогоне; `None` — по ссылке её получить нельзя."""
    if run.run_goals is None or run.run_goals.refusal(clause) is not None:
        return None
    status = run.goal_status.get(clause.goal_ref)
    shown = "получена в этом прогоне" if status == GOAL_OBTAINED else status or "не получена в этом прогоне"
    return f'по ссылке: goals ["{clause.goal_ref}"] — {shown}'


def _describe_goal(run: LoopSession) -> str:
    """Разобранная цель для планировщика. У составной — все части со связями и способом получения: без них он
    не знает, что часть можно получить по ссылке, а что — только своими силами."""
    goal = run.goal
    clauses = goal.clauses()
    if len(clauses) == 1:
        targets = [target.concept for target in goal.targets]
        line = (
            f"goal_type={goal.intent_kind}; показатели={targets}; сущность={goal.entity_ref}; "
            f"период={goal.period_ref or 'все периоды'}; роль={goal.role}"
        )
        by_reference = _how_to_get(run, clauses[0])
        return f"{line}; {clauses[0].goal_ref}: {by_reference}" if by_reference else line
    parts = []
    for clause in clauses:
        part = (
            f"- {clause.goal_ref}: goal_type={clause.intent_kind}; "
            f"показатели={[target.concept for target in clause.targets]}; "
            f"период={clause.period_ref or 'все периоды'}; depends_on={clause.depends_on}"
        )
        if clause.unresolved:
            part += f"; текст части: {'; '.join(clause.unresolved)}"
        part += f"; получать: {_how_to_get(run, clause) or 'силами планировщика (сценарий, шаги или новый атом)'}"
        parts.append(part)
    return "\n".join([f"составная цель, частей: {len(clauses)}; сущность={goal.entity_ref}; роль={goal.role}", *parts])


def _describe_atom(atom: EphemeralAtom, ran: bool) -> str:
    request = atom.request
    source = f"загрузка {request.url}" if request.is_download else "код"
    requires = ", ".join(f"{spec.input_type}:{spec.concept or '*'}" for spec in request.requires) or "без входов"
    line = f"- {request.name}@{atom.version} ({source}): {request.purpose}; требует {requires} → {request.output_type}"
    if not ran:
        line += f" — НЕ ЗАПУЩЕН: артефактов {request.output_type} нет, данных ещё нет; нужен шаг с этим атомом"
    return line


def _describe_artifact_evidence(row: dict, payload) -> str:
    if isinstance(payload, FileArtifact):
        return f"{payload.artifact_id} | FileArtifact | — | — | файл {payload.filename}: {payload.summary or payload.file_kind}"
    evidence_id = evidence_id_of(payload)
    concept = getattr(payload, "concept", None) or row.get("concept") or "—"
    period = getattr(payload, "period_ref", None) or row.get("period_ref") or "—"
    # Результат без `evidence_id` (форма роли, отчёт) в `evidence_ids` не указывается: id у строки нет.
    value = _evidence_value(payload) if evidence_id else f"не цитируется: {_evidence_value(payload)}"
    return f"{evidence_id or '—'} | {_evidence_type(payload)} | {concept} | {period} | {value}"


def _evidence_value(payload) -> str:
    for field in ("scalar_fact", "derived_fact"):
        fact = getattr(payload, field, None)
        if fact is not None:
            unit = f" {fact.unit}" if getattr(fact, "unit", None) else ""
            return f"{fact.value}{unit}"
    if isinstance(payload, DynamicArtifact) and payload.producer.startswith(WEB_SEARCH_PRODUCER):
        # Находки — списком: из обрезанного JSON адрес для загрузки не выбрать (часть 19). Признак —
        # производитель, а не тип: `dyn:*`-тип вправе занять и созданный атом, формы находок у него нет.
        return describe_hits(payload.payload, settings.web_search_state_chars)
    if isinstance(payload, DynamicArtifact):
        return json.dumps(payload.payload, ensure_ascii=False, default=str)[:300]
    quote = getattr(payload, "quote_text", None)
    if quote:
        return str(quote)[:300]
    return _summarize(payload)[:300]


def _describe_evidence(payload) -> str:
    """Строка итога для пользователя: без имени типа, число читаемо (в планировщик уходит `_evidence_value`)."""
    label = getattr(payload, "concept", None) or _evidence_type(payload)
    period = getattr(payload, "period_ref", None)
    return f"{label}{f', {period}' if period else ''}: {_shown_value(payload)}"


def _shown_value(payload) -> str:
    for field in ("scalar_fact", "derived_fact"):
        fact = getattr(payload, field, None)
        if fact is not None and isinstance(fact.value, (int, float)):
            number = _readable_number(fact.value)
            return f"{number} {fact.unit}" if getattr(fact, "unit", None) else number
    return _evidence_value(payload)


def _readable_number(value: float) -> str:
    """Как `formatDisplayNumber` на экране: до 3 знаков, неразрывные пробелы в разрядах, запятая;
    малое ненулевое значение не превращается в ноль."""
    value += 0.0  # -0.0 → 0.0
    digits = 3 if abs(value) >= 0.0005 or value == 0 else min(2 - math.floor(math.log10(abs(value))), 15)
    text = f"{value:,.{digits}f}".rstrip("0").rstrip(".")
    return text.replace(",", " ").replace(".", ",")


def _summarize(payload) -> str:
    if isinstance(payload, DynamicArtifact):
        data = {"payload": payload.payload, "origin": payload.origin, "producer": payload.producer}
    elif isinstance(payload, BaseModel):
        data = _without_noise(payload.model_dump(mode="json"))
    else:
        return str(payload)
    return json.dumps(data, ensure_ascii=False, default=str)


def artifact_content(payload) -> str:
    """Полное содержимое артефакта для чтения ouroboros: значения, единицы, периоды, цитаты, источник."""
    if isinstance(payload, BaseModel):
        data = _without_none(payload.model_dump(mode="json"))
    else:
        data = str(payload)
    return json.dumps(data, ensure_ascii=False, default=str)


def _without_none(value):
    if isinstance(value, dict):
        return {key: _without_none(item) for key, item in value.items() if item is not None}
    if isinstance(value, list):
        return [_without_none(item) for item in value]
    return value


def _without_noise(value):
    if isinstance(value, dict):
        return {
            key: _without_noise(item) for key, item in value.items()
            if key not in _SUMMARY_NOISE_KEYS and item is not None
        }
    if isinstance(value, list):
        return [_without_noise(item) for item in value]
    return value


# Правило атома, из-за которого ход не исполнялся, — по тексту отказа; коды — закрытый список §3.0.
_REFUSAL_MARKS = (
    ("входов атома", "нет_входов"), ("лимит новых атомов", "лимит_атомов"),
    ("только dyn:*", "не_dyn_тип"), ("без x-unit", "нет_единицы"), ("контракт отклонён", "неверные_аргументы"),
)


def _refusal_code(problem: str) -> str:
    return next((code for mark, code in _REFUSAL_MARKS if mark in problem), "")


def _outcome_text(outcome: MoveOutcome) -> str:
    return f"{outcome.headline}\n{outcome.details}" if outcome.details else outcome.headline


def _progress_only(event_sink, event: dict) -> None:
    if event_sink is not None and event.get("type") in ("plan_graph", "node_run"):
        event_sink(event)


def _has_station_fault(result: ExecutionResult) -> bool:
    return any(
        node_run.error and node_run.error.startswith(STATION_FAULT_PREFIX)
        for node_run in result.node_runs.values()
    )


def describe_execution(result: ExecutionResult, known: frozenset[str] = frozenset()) -> MoveOutcome:
    """`known` — id артефактов, бывших до хода: узлы с ними переиспользованы, а не получены заново."""
    lines = []
    produced = 0
    empty_atoms: list[str] = []
    for key, node_run in result.node_runs.items():
        evidence = result.evidence_by_key.get(key, ())
        target_evidence = [item for item in evidence if _evidence_type(item) not in _INFRASTRUCTURE_OUTPUT_TYPES]
        fresh = [item for item in target_evidence if _progress_id(item) not in known]
        produced += len(fresh)
        if node_run.status == "succeeded" and not evidence:
            marker = "∅"
            empty_atoms.append(key.atom_name)
        elif node_run.status == "succeeded":
            marker = "✓"
        elif node_run.status in ("blocked", "skipped"):
            marker = "⊘"
        else:
            marker = "✗"
        output_types = list(dict.fromkeys(_evidence_type(item) for item in evidence))
        concept = next((getattr(item, "concept", None) for item in evidence if getattr(item, "concept", None)), None)
        line = f"{marker} {key.atom_name} {concept or ''} → {', '.join(output_types) or 'нет результата'} ({len(evidence)})"
        download = next((item for item in evidence if hasattr(item, "payload") and isinstance(item.payload, dict)
                         and {"content_type", "text"} <= set(item.payload)), None)
        if target_evidence and not fresh:
            line += "; уже было"
        if download is not None:
            line += f"; content_type={download.payload['content_type']}; длина текста={len(download.payload['text'])}"
        if node_run.error:
            line += ": " + node_run.error[-800:]
        ids = [evidence_id_of(item) for item in evidence if evidence_id_of(item)]
        if ids:
            line += "; evidence_ids=" + ", ".join(ids)
        files = [item for item in evidence if isinstance(item, FileArtifact)]
        if files:
            line += "; файлы: " + ", ".join(f"{file.artifact_id} ({file.filename}: {file.summary})" for file in files)
        lines.append(line)
    if not result.succeeded:
        headline = "ход завершён с ошибками"
    elif produced:
        headline = f"✓ {produced} новых результатов"
    elif empty_atoms:
        headline = f"результатов нет: {', '.join(empty_atoms)} вернули пусто"
    else:
        headline = "новых результатов нет, всё уже было получено"
    return MoveOutcome(
        headline, "\n".join(lines), failed=not result.succeeded,
        empty_atoms=tuple(empty_atoms),
    )


def _web_off_note(empty_atoms: tuple[str, ...], registry: AtomRegistry) -> str:
    """Причина пустого результата сетевого атома при выключенном тумблере: без неё планировщик видит
    «вернули пусто» и тратит ходы, а пользователь читает «не удалось найти» (dev 08.10.2026)."""
    if user_allowed_web_search():
        return ""
    networked = [name for name in empty_atoms if registry.get(name).execution_kind == "io"]
    return f"\n{', '.join(networked)}: {WEB_SEARCH_OFF_REASON}" if networked else ""


def _evidence_type(item) -> str:
    return getattr(item, "output_type", None) or type(item).__name__


def _progress_id(item) -> str | None:
    """Идентичность результата для «новое / уже было»: evidence_id, а у файла (его нет) — artifact_id."""
    return evidence_id_of(item) or (item.artifact_id if isinstance(item, FileArtifact) else None)


def load_case_atoms(store, owner_id, case_id: str) -> dict[str, EphemeralAtom]:
    atoms: dict[str, EphemeralAtom] = {}
    for row in store.list_artifacts(owner_id, case_id):
        if row["output_type"] != _SOURCE_OUTPUT_TYPE:
            continue
        for source in row["payloads"]:
            request = _request_from_dict(source.request)
            atoms[source.name] = EphemeralAtom(request=request, code=source.code)
    return atoms


def _request_from_dict(data: dict) -> AtomRequest:
    return AtomRequest(
        name=data["name"], purpose=data["purpose"], output_type=data["output_type"],
        output_schema=data["output_schema"],
        requires=tuple(InputSpec(**item) for item in data.get("requires", [])),
        url=data.get("url", ""),
    )


def save_source(store, owner_id, case_id: str, atom: EphemeralAtom) -> None:
    request = asdict(atom.request)
    source = EphemeralAtomSource(
        evidence_id=atom.version, name=atom.request.name, request=request, code=atom.code,
    )
    store.put_artifact(
        owner_id, case_id,
        node_key=NodeKey(
            atom_name=atom.request.name, atom_version=atom.version, args_fingerprint="source",
        ),
        output_type=_SOURCE_OUTPUT_TYPE, payloads=(source,), concept=None, entity_ref=None,
        period_ref=None, basis=None, document_version=None, policy_version=None,
    )


__all__ = [
    "Decision", "DynamicRun", "DynamicRunResult", "InvalidSteps", "JournalEntry", "LoopSession", "MoveOutcome",
    "Rejection",
    "GOAL_OBTAINED", "GoalRunner", "RunLimits", "ScenarioResult", "ScenarioRunner", "artifacts_table",
    "case_rows", "complete_plan", "artifact_content", "state_sections", "ARTIFACTS_TITLE", "ARTIFACTS_HEADER", "describe_clause", "describe_execution", "load_case_atoms",
    "decision_from_data", "parse_decision", "parse_steps", "render_state", "save_source",
]
