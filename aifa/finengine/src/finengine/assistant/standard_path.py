"""Стандартный путь для `aifa_goal`: цель хода → разбор → канонический конвейер → итог (CONTRACTS.md §3.1).

Цель — исходное сообщение прогона (ADR-0004), документы — те, что ход получил при создании
(`_chat_turn_docs`). События конвейера идут в журнал этого хода, как шли при чате без ouroboros.

Этап 2 (ADR-0029): сбой стандартного пути служба разбирает сама — цель идёт тем же вызовом, но с
`allow_dynamic=True`, то есть в нынешний `DynamicRun`; его итог — тоже `ГОТОВО`. Это временно: с шага 14
сбой вернёт `НУЖЕН_ЦИКЛ`, а цикл поведёт ouroboros.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

from finengine.assistant.events import Emit, Event
from finengine.assistant.goal_result import GoalResult, Verdict
from finengine.assistant.turn_record import TurnRecord
from finengine.ingestion.document_store import get_store
from finengine.llm.budget import BudgetExceeded
from finengine.orchestrator.live_stream import stream_blocking
from finengine.skills.atoms.intent import IngressContext, IntentDecision, parse_message
from finengine.skills.production_runtime import EvidenceGateOutcome, execute_routed_goal
from finengine.skills.result_events import typed_error_event
from finengine.skills.spec.dynamic_run import case_rows
from finengine.store.cases import CasePersistenceError, get_case_store

log = logging.getLogger(__name__)

PARTIAL_STATUSES = frozenset({"partial", "missing", "conflicting", "failed"})


@dataclass(frozen=True)
class HoldStageTwo:
    """Отказ, после которого цель уйдёт на этап 2, пользователю не показывается: он ещё не итог."""

    emit: Emit

    def __call__(self, event: Event) -> None:
        if event.get("type") == "typed_error" and _stage_two_error(event.get("error") or {}):
            return
        self.emit(event)


class StandardPath:
    async def __call__(self, record: TurnRecord, emit: Emit) -> GoalResult:
        decision = await self._parse(record, emit)
        if decision.route != "plan" or decision.goal is None:
            return GoalResult(Verdict.NO_CALCULATION, decision.reason)
        gate_doc = _gate_document(record)
        if not gate_doc:
            return self._without_scope(record, emit)
        text = await asyncio.to_thread(_document_text, gate_doc)
        if not text:
            return GoalResult(
                Verdict.NO_DOCUMENT,
                "Текст документа ещё не готов или не распознан: попроси пользователя подождать или загрузить отчёт заново.",
            )
        outcome = await self._guarded(record, gate_doc, text, decision.goal, emit)
        if outcome is None:
            return GoalResult(Verdict.NO_CALCULATION, "Вид цели не подключён к конвейеру: ответь пользователю сам.")
        rows = await asyncio.to_thread(case_rows, get_case_store(), record.user_id, record.case_id)
        return _result_of(outcome, tuple(rows))

    async def _parse(self, record: TurnRecord, emit: Emit) -> IntentDecision:
        context = await asyncio.to_thread(_ingress_context, record)
        try:
            return await parse_message(record.message, context=context)
        except Exception:
            log.error("разбор цели хода %s не удался", record.run_id[:8], exc_info=True)
            emit(typed_error_event(
                code="intent_parse_failed",
                message="не удалось разобрать запрос; повторите его, пожалуйста",
                retryable=True,
            ))
            raise

    def _without_scope(self, record: TurnRecord, emit: Emit) -> GoalResult:
        """Цель требует расчёта, а документ хода не один и не выбран: как отказ прежнего чата."""
        code = "document_required" if not record.doc_ids else "document_scope_ambiguous"
        message = (
            "для финансового конвейера приложите отчёт"
            if not record.doc_ids
            else "выберите один активный отчёт; многодокументный план пока не собран"
        )
        emit(typed_error_event(code=code, message=message, retryable=True))
        return GoalResult(Verdict.CLARIFY, message)

    async def _guarded(
        self, record: TurnRecord, doc_id: str, text: str, goal: Any, emit: Emit
    ) -> EvidenceGateOutcome | None:
        """Системная ошибка конвейера — в журнал службы и в журнал хода (как отказ прежнего чата), затем дальше."""
        try:
            return await self._execute(record, doc_id, text, goal, emit)
        except Exception:
            log.error("конвейер хода %s упал", record.run_id[:8], exc_info=True)
            emit(typed_error_event(
                code="evidence_gate_failed",
                message=(
                    "канонический финансовый конвейер временно недоступен; "
                    "числовой ответ из текста документа не сформирован"
                ),
                retryable=True,
            ))
            raise

    async def _execute(
        self, record: TurnRecord, doc_id: str, text: str, goal: Any, emit: Emit
    ) -> EvidenceGateOutcome | None:
        try:
            outcome = await self._stream(record, doc_id, text, goal, HoldStageTwo(emit), allow_dynamic=False)
        except (CasePersistenceError, BudgetExceeded):
            raise  # состояние вокруг графа, а не ошибка графа: цикл тут не поможет (Р14.3)
        except Exception as crash:  # noqa: BLE001 — граф собрался и упал: это вход в этап 2, как в чате
            log.warning("стандартный путь хода %s упал, цель уходит на этап 2: %r", record.run_id[:8], crash)
            return await self._stream(record, doc_id, text, goal, emit, allow_dynamic=True)
        if outcome is not None and _stage_two_needed(outcome):
            return await self._stream(record, doc_id, text, goal, emit, allow_dynamic=True)
        return outcome

    async def _stream(
        self, record: TurnRecord, doc_id: str, text: str, goal: Any, emit: Emit, *, allow_dynamic: bool
    ) -> EvidenceGateOutcome | None:
        outcome: EvidenceGateOutcome | None = None
        async for kind, payload in stream_blocking(
            execute_routed_goal,
            owner_id=record.user_id,
            case_id=record.case_id,
            document_id=doc_id,
            document_text=text,
            goal=goal,
            allow_dynamic=allow_dynamic,
            trace_id=record.trace_id,
            cancel_kw="is_cancelled",
        ):
            if kind == "event":
                emit(payload)
            else:
                outcome = payload
        return outcome


def _gate_document(record: TurnRecord) -> str:
    """Документ, по которому идёт конвейер: единственный документ хода или активный из них."""
    if len(record.doc_ids) == 1:
        return record.doc_ids[0]
    return record.active_doc_id if record.active_doc_id in record.doc_ids else ""


def _document_text(doc_id: str) -> str:
    from finengine.store.blobs import document_text  # noqa: PLC0415 — тяжёлый слой хранилища

    return document_text(doc_id) or ""


def _ingress_context(record: TurnRecord) -> IngressContext:
    """Контекст маршрутизатора: документы хода и роль. Реплик диалога нет — история в ouroboros (шаг 11)."""
    documents: list[tuple[str, bool]] = []
    for doc_id in record.doc_ids:
        meta = get_store().get_document(doc_id)
        name = (getattr(meta, "company_name", "") or getattr(meta, "filename", "") or doc_id) if meta else doc_id
        documents.append((str(name), doc_id == record.active_doc_id or len(record.doc_ids) == 1))
    return IngressContext(documents=tuple(documents), ui_role=record.role)


def _stage_two_error(error: Event) -> bool:
    """Узел упал (`retryable`) или план не собрался — стандартный путь не справился; «показателя нет» — не сбой."""
    code = error.get("code")
    return code == "scenario_not_deterministic" or (
        code == "canonical_pipeline_incomplete" and bool(error.get("retryable"))
    )


def _stage_two_needed(outcome: EvidenceGateOutcome) -> bool:
    return _stage_two_error(outcome.manifest.get("error") or {})


def _result_of(outcome: EvidenceGateOutcome, rows: tuple[Event, ...]) -> GoalResult:
    manifest = outcome.manifest
    error = manifest.get("error")
    if error:
        message = str(error.get("message") or "Конвейер не дал результата.")
        verdict = Verdict.CLARIFY if error.get("code") == "goal_needs_clarification" else Verdict.ANSWERED
        return GoalResult(verdict, message, rows)
    dynamic = manifest.get("dynamic_run")
    if dynamic:
        return GoalResult(Verdict.ANSWERED, str(dynamic.get("answer") or ""), rows)
    status = str((manifest.get("result") or {}).get("status") or "supported")
    note = (
        f"Конвейер завершён частично (статус результата: {status}); значения — в таблице."
        if status in PARTIAL_STATUSES
        else "Расчёт выполнен: проверенные результаты — в таблице."
    )
    unperformed = manifest.get("not_performed") or []
    if unperformed:
        note += " Не выполнено: " + "; ".join(unperformed) + "."
    return GoalResult(Verdict.ANSWERED, note, rows)
