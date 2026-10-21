"""Single production ingress for Slice 13.5 financial execution.

This module owns the only allowed transition from request/document inputs to
``CaseRuntime``.  It freezes classification and the effective registry once,
then passes the same ``RunPassport`` object to every planned root.  It contains
no legacy handler, result-shape adapter, traffic flag or fallback.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
from dataclasses import dataclass, replace
from typing import Callable, Mapping, Sequence

from finengine.config import settings
from finengine.contracts import GoalClause, GoalIR, RunPassport
from finengine.llm.budget import BudgetExceeded
from finengine.skills.atoms.frame import classify_frame
from finengine.skills.atoms.graph_atoms import build_registry, default_policy
from finengine.skills.atoms.intent import (
    IngressContext,
    IntentParseError,
    RootTarget,
    parse_message,
    root_from_goal,
)
from finengine.skills.atoms.run_passport import build_run_passport, classification_from_cell
from finengine.skills.case_runtime import CaseRuntime, PlanInfeasible, PlannedCaseRun
from finengine.skills.metric_schema import compiled_registry_for_classification
from finengine.skills.periods import normalize_period_label, period_kind_for_enum, split_period_labels
from finengine.skills.result_events import file_event, typed_error_event, typed_result_event, typed_results
from finengine.skills.spec.dynamic_run import (
    DynamicRun,
    DynamicRunResult,
    LoopSession,
    ScenarioResult,
    _progress_only,
    complete_plan,
    describe_execution,
)
from finengine.skills.spec.graph_executor import ExecutionBudget, ExecutionResult, RunInputs
from finengine.skills.spec.loop_state import LoopOrigin, restore_state
from finengine.skills.spec.target import Target
from finengine.store.cases import CasePersistenceError, SqlCaseStore, get_case_store


logger = logging.getLogger(__name__)

EventSink = Callable[[dict], None]


@dataclass(frozen=True)
class ProductionContext:
    """Frozen execution context created exactly once at ingress."""

    runtime: CaseRuntime
    passport: RunPassport
    owner_id: object
    case_id: str
    document_id: str
    document_version: str
    trace_id: str = ""  # трасса хода ouroboros: уходит в комнату станции заголовком (ADR-0007)


@dataclass(frozen=True)
class ProductionRootResult:
    root: RootTarget
    planned: PlannedCaseRun
    execution: ExecutionResult


@dataclass(frozen=True)
class ProductionOutcome:
    context: ProductionContext
    roots: tuple[ProductionRootResult, ...] = ()
    refusal_event: dict | None = None
    dynamic: DynamicRunResult | None = None
    loop: LoopSession | None = None   # цикл открыт и передан ведущему снаружи (`open_loop`), а не пройден здесь
    # Исключение из `CaseRuntime.execute` (таймаут, бюджет вызовов, сбой исполнителя): граф собрался и
    # упал. Выставляется только при `allow_dynamic` — иначе исключение поднимается как раньше.
    crashed: Exception | None = None

    @property
    def succeeded(self) -> bool:
        return self.refusal_event is None and bool(self.roots) and all(
            item.execution.succeeded for item in self.roots
        )


def _methodology_version(value: str | None) -> str:
    return (value or os.environ.get("FINENGINE_METHODOLOGY_VERSION") or "unversioned").strip()


def _model_version(value: str | None) -> str:
    return (value or getattr(settings, "openrouter_model", "") or "unversioned").strip()


def prepare_production_context(
    *,
    owner_id: object,
    case_id: str,
    document_id: str,
    document_text: str,
    goal_ir: GoalIR,
    cell: str = "",
    company_type_override: str | None = None,
    methodology_version: str | None = None,
    model_version: str | None = None,
    prompt_version: str | None = None,
    sandbox_image_version: str | None = None,
    store: SqlCaseStore | None = None,
    registry_options: Mapping[str, object] | None = None,
    budget: ExecutionBudget | None = None,
    trace_id: str = "",
) -> ProductionContext:
    """Freeze passport/registry and construct the sole production runtime.

    The compiled snapshot is materialized once and supplied both to the
    passport and ``build_registry``.  Downstream atoms therefore cannot observe
    a later registry-layer change during this run.
    """

    classification = (
        classification_from_cell(cell, company_type_override=company_type_override)
        if cell
        else classify_frame(document_text, company_type_override=company_type_override)
    )
    compiled = compiled_registry_for_classification(classification)
    passport = build_run_passport(
        document_text,
        goal_ir_id=goal_ir.goal_id,
        classification=classification,
        registry_version=compiled.version,
        filename=document_id,
        methodology_version=_methodology_version(methodology_version),
        model_version=_model_version(model_version),
        prompt_version=(prompt_version or os.environ.get("FINENGINE_PROMPT_VERSION") or "unversioned"),
        sandbox_image_version=(
            sandbox_image_version
            or os.environ.get("FINENGINE_SANDBOX_IMAGE_VERSION")
            or "unversioned"
        ),
    )
    options = dict(registry_options or {})
    registry = build_registry(
        profile_id=classification.profile_id,
        registry_data=compiled.to_effective_data(),
        **options,
    )
    runtime = CaseRuntime(
        store=store or get_case_store(),
        registry=registry,
        policy=default_policy(classification.profile_id),
        budget=budget,
    )
    runtime.ensure_case(owner_id=owner_id, case_id=case_id)
    document_version = "sha256:" + hashlib.sha256(document_text.encode("utf-8")).hexdigest()
    runtime.register_document(
        owner_id=owner_id,
        case_id=case_id,
        logical_name=document_id,
        document_id=document_id,
        document_version=document_version,
    )
    return ProductionContext(
        runtime=runtime,
        passport=passport,
        owner_id=owner_id,
        case_id=case_id,
        document_id=document_id,
        document_version=document_version,
        trace_id=trace_id,
    )


def _execute_graph(
    context: ProductionContext, plan_run_id: str, inputs: RunInputs, event_sink: EventSink | None,
    allow_dynamic: bool,
) -> tuple[ExecutionResult | None, Exception | None]:
    """Исполнить собранный граф. Падение — второй элемент пары, а не исключение (только при `allow_dynamic`).

    Не ошибка графа, а состояние вокруг него — не повод для цикла (Р14.3): `CasePersistenceError`
    (документ не активен, ревизия Case устарела), `BudgetExceeded` (бюджет модели исчерпан). Остановка —
    `BaseException` — сюда не попадает вовсе."""
    try:
        execution = context.runtime.execute(
            owner_id=context.owner_id, case_id=context.case_id, plan_run_id=plan_run_id,
            inputs=inputs, event_sink=event_sink,
        )
    except (CasePersistenceError, BudgetExceeded):
        raise
    except Exception as exc:  # noqa: BLE001 — граница исполнения: падение графа уходит на решение вызывающему
        if not allow_dynamic:
            raise
        return None, exc
    return execution, None


def execute_production_goal(
    context: ProductionContext,
    goal_ir: GoalIR,
    *,
    document_text: str,
    root_extra: Mapping[str, str] | None = None,
    event_sink: EventSink | None = None,
    roots: Sequence[RootTarget] | None = None,
    allow_dynamic: bool = False,
    dynamic_reason: str | None = None,
    dynamic_cause: BaseException | None = None,
    dynamic_stage: str = "planning",
    is_cancelled: Callable[[], bool] | None = None,
    open_loop: LoopOpener | None = None,
) -> ProductionOutcome:
    """Resolve and execute every semantic root without legacy fallback.

    ``roots``, when given, overrides ``root_from_goal(goal_ir)``.  Срез 29's
    evidence gate (``execute_routed_goal`` below) uses this to route
    several explicitly named registered metrics into one scoped ``FormOutput``
    root instead of one root per target — ``root_from_goal`` itself is not
    touched, it stays exactly the one-root-per-target ``TargetFactory`` it
    already was (that function is part of the atomic-planner boundary this
    Срез does not change).

    ``allow_dynamic``: если детерминированный планировщик не собрал исполнимый
    план (``PlanInfeasible``), запрос получает цикл ``DynamicRun``.
    ``dynamic_reason`` — любая причина сбоя детерминированного планирования, найденная до этого
    вызова (вид цели без корня, корень не построился, общий граф не собрался): детерминированный
    путь пропускается, причина идёт агенту как ошибка. ``dynamic_cause`` — исключение, из которого
    она выросла: если оно есть, в лог входа в цикл попадает его трассировка. ``dynamic_stage`` —
    где сбой: ``planning`` (граф не собрался) или ``execution`` (собрался и упал, часть 14).

    При ``allow_dynamic`` исключение из исполнения графа не поднимается, а возвращается как
    ``ProductionOutcome.crashed`` — вход в цикл решает вызывающий (`_execution_failure`). Состояние
    ``Case`` (``CasePersistenceError``), бюджет модели и остановка (``BaseException``) поднимаются всегда.
    """

    if roots is None:
        try:
            roots = root_from_goal(goal_ir)
        except NotImplementedError as exc:
            refusal = typed_error_event(
                code="unsupported_goal_type",
                message=str(exc),
                retryable=False,
            )
            if event_sink is not None:
                event_sink(refusal)
            return ProductionOutcome(context=context, refusal_event=refusal)

    if not roots:
        refusal = typed_error_event(
            code="unsupported_goal_type",
            message=f"GoalType {goal_ir.intent_kind!r} has no resolved semantic Target",
            retryable=False,
        )
        if event_sink is not None:
            event_sink(refusal)
        return ProductionOutcome(context=context, refusal_event=refusal)

    # A compound request is one persisted PlanRun and one executable DAG.  It
    # is deliberately composed before execution: independent scenarios become
    # sibling branches, while declared depends_on relations become root edges.
    if len(roots) > 1:
        requests = []
        prepared_roots: list[RootTarget] = []
        for root in roots:
            if root.ad_hoc_spec is not None:
                context.runtime.store.put_ad_hoc_spec(context.owner_id, context.case_id, root.ad_hoc_spec)
            merged_extra = {**root.root_extra, **dict(root_extra or {})}
            passport_entity = getattr(context.passport, "entity", None)
            target = replace(
                root.root,
                entity_ref=(
                    root.root.entity_ref
                    or getattr(passport_entity, "entity_id", None)
                    or getattr(passport_entity, "canonical_name", None)
                    or context.document_id
                ),
                document_version=context.document_version,
            )
            prepared = root._replace(root=target, root_extra=merged_extra)
            prepared_roots.append(prepared)
            requests.append((
                target, merged_extra, root.goal_ref, root.goal_type or goal_ir.intent_kind,
                root.depends_on,
            ))
        planned = context.runtime.plan_many(
            owner_id=context.owner_id, case_id=context.case_id, goal_ir=goal_ir,
            requests=requests, run_passport=context.passport,
        )
        execution, crash = _execute_graph(
            context, planned.record.plan_run_id,
            _run_inputs(context, document_text=document_text, role=goal_ir.role or "", extra=root_extra),
            event_sink, allow_dynamic,
        )
        if crash is not None:
            return ProductionOutcome(context=context, crashed=crash)
        return ProductionOutcome(
            context=context,
            roots=(ProductionRootResult(root=prepared_roots[0], planned=planned, execution=execution),),
        )

    completed: list[ProductionRootResult] = []
    for root in roots:
        if root.ad_hoc_spec is not None:
            # Persist the semantic definition before planning.  The graph gets
            # only its immutable id/data through root_extra; the canonical
            # registry is deliberately untouched.
            context.runtime.store.put_ad_hoc_spec(
                context.owner_id, context.case_id, root.ad_hoc_spec,
            )
        merged_extra = dict(root.root_extra)
        merged_extra.update(root_extra or {})
        target = root.root
        # A document-scoped chat request commonly omits the company name
        # because the active PDF is already selected.  The passport has
        # classified that exact document before planning, so its frozen
        # entity is the deterministic identity for node fingerprints; it
        # is not an entity guessed from the user's wording.
        passport_entity = getattr(context.passport, "entity", None)
        resolved_entity_ref = (
            target.entity_ref
            or getattr(passport_entity, "entity_id", None)
            or getattr(passport_entity, "canonical_name", None)
            or context.document_id
        )
        target = replace(
            target,
            entity_ref=resolved_entity_ref,
            document_version=context.document_version,
        )
        try:
            if dynamic_reason is not None:
                raise PlanInfeasible(dynamic_reason) from dynamic_cause
            planned = context.runtime.plan(
                owner_id=context.owner_id,
                case_id=context.case_id,
                goal_ir=goal_ir,
                target=target,
                run_passport=context.passport,
                root_extra=merged_extra,
            )
        except PlanInfeasible as exc:
            if not allow_dynamic:
                raise
            # Трассировка — только когда за сбоем стоит исключение кода: штатный сбой (модель не составила
            # формулу, вид цели без корня) её не шумит, а поломка не прячется за ответом цикла.
            logger.warning(
                "deterministic %s failed, starting dynamic run case=%s goal_type=%s: %s",
                dynamic_stage, context.case_id, goal_ir.intent_kind, exc,
                exc_info=exc if exc.__cause__ is not None else None,
            )
            if open_loop is not None:
                return ProductionOutcome(context=context, loop=_hand_over_loop(
                    open_loop, context, goal_ir, target, merged_extra, document_text, event_sink,
                    dynamic_stage, str(exc),
                ))
            try:
                dynamic = DynamicRun(
                    **_loop_parts(context, goal_ir, target, merged_extra, document_text, event_sink),
                    entry_stage=dynamic_stage,
                    is_cancelled=is_cancelled,
                    plan_fn=complete_plan,
                ).run(deterministic_error=str(exc))
            except BudgetExceeded:
                raise
            except Exception as failure:  # noqa: BLE001 — граница цикла: причина доходит до пользователя
                logger.error("dynamic run failed case=%s", context.case_id, exc_info=True)
                refusal = typed_error_event(
                    code="dynamic_run_failed",
                    message=f"{type(failure).__name__}: {failure}"[:_MANIFEST_TEXT_LIMIT],
                    retryable=True,
                )
                if event_sink is not None:
                    event_sink(refusal)
                return ProductionOutcome(context=context, refusal_event=refusal)
            return ProductionOutcome(context=context, dynamic=dynamic)
        execution, crash = _execute_graph(
            context, planned.record.plan_run_id,
            _run_inputs(context, document_text=document_text, role=goal_ir.role or "", extra=merged_extra),
            event_sink, allow_dynamic,
        )
        if crash is not None:
            return ProductionOutcome(context=context, crashed=crash)
        completed.append(ProductionRootResult(root=root, planned=planned, execution=execution))
    return ProductionOutcome(context=context, roots=tuple(completed))


# ---------------------------------------------------------------------------
# Срез 31 (docs/build-plan/31-vozvrat-k-zamyslu-vladeltsa-ingress-v2.md):
# Ingress v2 — исполнение цели, которую модель-маршрутизатор передала планировщику.
#
# ГДЕ. `FinengineAgent.query_stream` вызывает `parse_message` (`skills/atoms/
# intent.py`) на КАЖДОМ сообщении. Только если модель выбрала `route="plan"`
# (запрос — один из GoalType) и у хода есть ровно один адресуемый документ, агент
# передаёт полученный `GoalIR` сюда. Решение «звать ли канонический конвейер»
# здесь НЕ принимается — его принимает модель. Регэкспов, порогов «≥5
# кандидатов», повторных разборов и period prepass, которые раньше подменяли это
# решение (Срезы 29/29a/30), больше нет.
#
# ПОЧЕМУ ФУНКЦИЯ СИНХРОННАЯ. Каждый модельный атом (`read.value`, `locate`,
# `out.narrative`, ...) сам открывает короткоживущий event loop, потому что
# исполнитель графа (`GraphExecutor.run`) — синхронный код; прямой вызов из
# корутины `query_stream` уронил бы `asyncio.run() cannot be called from a
# running event loop`. Поэтому агент запускает её через `stream_blocking`
# (`orchestrator/live_stream.py`): события `event_sink` доходят до клиента по
# мере появления, а не после завершения.
#
# ПЕРИОД. Не спрашивается. Пустой `goal.period_ref` = «все доступные периоды»:
# корень строится без периода, экстрактор возвращает всё, что нашёл, и ВСЕ
# значения публикуются. Явно названный пользователем период остаётся фильтром, а
# несовпавший результат — типизированным отказом `canonical_period_mismatch`.
#
# ЧТО, ЕСЛИ ПЛАНИРОВЩИК ЦЕЛЬ НЕ РАСКРЫЛ. Возврат `None` — вид цели ещё не
# подключён к детерминированному графу (`NotImplementedError` из `root_from_goal`).
# Любой другой отказ — типизированный `typed_error`; диспетчер после него не
# получает ни prompt, ни сырой документ (числовой обход закрыт).
# `monitor_changes` (Срез 32) — изменения между периодами ОДНОГО отчёта: проверок
# Case/снимков здесь нет, планировщик сам достраивает недостающее и берёт из Case
# уже сохранённые показатели (`AvailableIndex`), поэтому «пустой чат» и «середина
# диалога» — один и тот же граф с разной долей переиспользованных узлов.
_GATED_KINDS = (
    "extract_registered_metric", "extract_ad_hoc_metric",
    "calculate_registered_metric", "calculate_ad_hoc_metric",
    "full_analysis", "form", "monitor_changes", "explain_change",
)
# Аналитические цели по ролевому набору показателей: одна методика в паспорте,
# один бюджет и один состав `root_extra` (`_full_analysis_root_extra`).
_ANALYSIS_KINDS = ("full_analysis", "monitor_changes")
# Срез 30: граф FULL_ANALYSIS на реальном отчёте — 627 узлов; на свободной
# системе он исполнился за 279 с, под нагрузкой — дольше бюджета GraphExecutor
# по умолчанию (300 с, рассчитан на один показатель). Прежний «полный путь» по
# документации занимал 6–25 минут; для одного показателя бюджет не меняется.
_FULL_ANALYSIS_BUDGET = ExecutionBudget(max_wall_seconds=1800.0)
# A compound metric graph can spend one upstream timeout (the live extraction
# model currently retries at roughly 360 s) before any downstream read nodes
# become runnable.  Keep the 300 s guard for a single metric, but let an
# explicitly compound request survive one such transient stall.
_COMPOUND_METRIC_BUDGET = ExecutionBudget(max_wall_seconds=600.0)


def _with_wall_floor(budget: ExecutionBudget | None) -> ExecutionBudget | None:
    """Бюджет вида цели, поднятый до `settings.graph_min_wall_seconds`; `None` — умолчание графа."""
    current = budget or ExecutionBudget()
    if current.max_wall_seconds >= settings.graph_min_wall_seconds:
        return budget
    return replace(current, max_wall_seconds=settings.graph_min_wall_seconds)
_FORM_OUTPUT_TYPE = "FormOutput"
# Код итоговой ошибки `CaseRuntime.execute`, когда корень графа не получил результата.
_GRAPH_FAILED_CODE = "graph_execution_failed"


@dataclass(frozen=True)
class EvidenceGateOutcome:
    """Один канонический результат, который диспетчер обязан процитировать —
    он не создаёт второй числовой путь и не оставляет модели решения,
    которое модель могла обойти.

    `node_run`/`plan_graph`/`typed_error` события, произведённые самим
    исполнением, сюда НЕ дублируются — вызывающий код получает их через
    `event_sink` по мере появления; этот dataclass несёт только итоговый
    манифест для tool_output."""

    tool_name: str
    manifest: dict


def _refusal_outcome(
    event_sink: EventSink | None, *, case_id: str, document_id: str,
    code: str, message: str, retryable: bool,
) -> EvidenceGateOutcome:
    """Типизированный отказ: событие клиенту + манифест для истории диалога."""
    refusal = typed_error_event(code=code, message=message, retryable=retryable)
    if event_sink is not None:
        event_sink(refusal)
    return EvidenceGateOutcome(
        tool_name="financial_pipeline",
        manifest={"schema_version": "1.0", "case_id": case_id, "document_id": document_id, **refusal},
    )


_METRIC_KINDS = (
    "extract_registered_metric", "extract_ad_hoc_metric",
    "calculate_registered_metric", "calculate_ad_hoc_metric",
)


def _failed_nodes(outcome: "ProductionOutcome", limit: int = 2) -> list[str]:
    """`имя_атома: причина` первых упавших узлов исполнения (для честного отказа).

    Признак — `ExecutionResult.errors`: упавшие названы первыми; заблокированные без упавших (сбой песочницы)
    тоже ошибка, а не «показателя нет» (часть 14)."""
    errors = [run for item in outcome.roots for run in item.execution.errors]
    shown = [run for run in errors if run.status == "failed"] or errors
    return [f"{run.node_key.atom_name}: {run.error or 'без описания ошибки'}" for run in shown][:limit]


def _root_matches(outcome: "ProductionOutcome", roots: Sequence[RootTarget]) -> tuple[list, list[str]]:
    """Значения корней исполненного графа и типы корней. Тип корня — из СОХРАНЁННОЙ цели плана: для
    динамического плана его выбрал агент, для обычного он совпадает с `roots[0].root.output_type`."""
    compound_scenarios = (
        outcome.roots[0].planned.record.graph.scenarios if outcome.roots else []
    )
    root_output_types = (
        [root.root.output_type for root in roots]
        if compound_scenarios else
        [outcome.roots[0].planned.record.target.output_type if outcome.roots else roots[0].root.output_type]
    )
    values = [
        value
        for item in outcome.roots
        for key in ([scenario.root for scenario in item.planned.record.graph.scenarios] or [item.planned.record.graph.root])
        for value in item.execution.evidence_by_key.get(key, ())
    ]
    return [v for v in values if any(_is_value_of_root_type(v, kind) for kind in root_output_types)], root_output_types


def _execution_failure(
    outcome: "ProductionOutcome", roots: Sequence[RootTarget],
) -> tuple[str, Exception | None] | None:
    """Граф собрался и упал с ОШИБКОЙ → `(причина, исключение)`; иначе `None` (Р14.1–Р14.2).

    Граница — типизированная, текст сообщения не читается:
    * исключение из исполнения (`outcome.crashed`) — ошибка;
    * у корня нет результата И есть узел в терминальном сбое (`ExecutionResult.errors`) — ошибка;
    * узлы отработали без ошибок, корень пуст («показателя нет», «периода нет») — результат, не вход в цикл."""
    if outcome.crashed is not None:
        return f"исполнение графа прервано: {type(outcome.crashed).__name__}: {outcome.crashed}", outcome.crashed
    if outcome.dynamic is not None or outcome.loop is not None or outcome.refusal_event is not None or not outcome.roots:
        return None
    matches, _ = _root_matches(outcome, roots)
    failed = [item.execution for item in outcome.roots if item.execution.errors]
    if (matches and outcome.succeeded) or not failed:
        return None
    described = [describe_execution(execution) for execution in failed]
    return "\n".join(f"{item.headline}\n{item.details}" for item in described), None


def _incomplete_refusal(
    goal: GoalIR, roots: Sequence[RootTarget], outcome: "ProductionOutcome", root_output_type: str,
) -> dict:
    """Типизированный отказ, когда конвейер отработал, но результата корня нет.

    Живая проверка (M3, dev): показателя в отчёте просто нет, а пользователь видел
    `canonical_pipeline_incomplete` — из него ничего не понять. Теперь: узел упал —
    называется узел и причина; узлы отработали, но пусто — говорится, ЧТО искали
    (имя и синонимы, придуманные моделью, либо код показателя реестра)."""
    failed = _failed_nodes(outcome)
    if failed:
        return typed_error_event(
            code="canonical_pipeline_incomplete",
            message=f"конвейер не завершён для {goal.intent_kind!r}: " + "; ".join(failed),
            retryable=True,
        )
    if goal.intent_kind in _METRIC_KINDS:
        spec = roots[0].ad_hoc_spec if roots else None
        if spec is not None and getattr(spec, "components", None):
            names = ", ".join(c.name_ru or c.term for c in spec.components)
            message = (
                f"Не удалось рассчитать «{spec.term}» ({spec.expression}): не найдены входные показатели "
                f"среди: {names}. Формула и допущения — в описании запроса."
            )
        elif spec is not None:
            aliases = [*spec.aliases_ru, *spec.aliases_en]
            searched = f" Искал также по: {', '.join(aliases)}." if aliases else ""
            message = f"Показатель «{spec.name_ru or spec.term}» не найден в отчёте.{searched}"
        else:
            codes = ", ".join(t.concept for t in goal.targets) or "запрошенный показатель"
            message = f"Показатель {codes} не найден в отчёте."
        return typed_error_event(code="metric_not_found", message=message, retryable=False)
    if goal.intent_kind == "monitor_changes":
        return typed_error_event(
            code="changes_not_found",
            message="в отчёте не найдено ни одного показателя роли для сравнения периодов",
            retryable=False,
        )
    return typed_error_event(
        code="canonical_pipeline_incomplete",
        message=f"canonical {root_output_type} не получен для {goal.intent_kind!r}",
        retryable=False,
    )


def _is_value_of_root_type(value: object, root_output_type: str) -> bool:
    """Значение корня: типизированные результаты несут поле `output_type`, прочие
    доказательства (цитата, структура, ...) — только имя класса; составной тип
    `ReferenceValue:plan` сравнивается по базовому имени."""
    if getattr(value, "output_type", None) == root_output_type:
        return True
    return type(value).__name__ == root_output_type.split(":")[0]


def _value_period_matches(label: str, value: object, kinds: Mapping[str, str]) -> bool:
    """Относится ли значение к одной названной метке периода (`1H 2025` = `6M 2025`)."""
    concept = str(getattr(value, "concept", "") or "").strip().upper()
    kind = kinds.get(concept) or period_kind_for_enum(concept)
    actual_period = str(getattr(value, "period_ref", "") or "").strip()
    return normalize_period_label(actual_period, value_kind=kind) == normalize_period_label(label, value_kind=kind)


def _select_published(
    matches: Sequence,
    requested_period: str,
    *,
    value_kinds: Mapping[str, str] | None = None,
) -> tuple[list, list[str]]:
    """Какие значения корня публикуются и какие периоды среди них найдены.

    Период не назван — публикуются ВСЕ значения (все доступные даты), периоды
    не перечисляются. Назван — только совпавшие по нормализованной подписи
    (`1H 2025` = `6M 2025`); `found_periods` нужен для честного отказа, когда
    ни одно значение не относится к запрошенному периоду."""
    if not requested_period:
        return list(matches), []
    kinds = {str(key).strip().upper(): value for key, value in (value_kinds or {}).items()}
    requested_labels = split_period_labels(requested_period)

    published = []
    for value in matches:
        if not str(getattr(value, "period_ref", "") or "").strip():
            # Container outputs (for example a scoped FormOutput inside a
            # compound request) own their period axis in nested rows.  They
            # must survive the scalar period gate; otherwise one neighbouring
            # scalar match makes ``published`` non-empty and silently drops
            # the whole table at the fallback below.
            published.append(value)
            continue
        if any(_value_period_matches(label, value, kinds) for label in requested_labels):
            published.append(value)
    found = sorted({
        str(getattr(v, "period_ref", "") or "").strip() for v in matches
        if str(getattr(v, "period_ref", "") or "").strip()
    })
    return published, found


def _unanswered_periods(
    published: Sequence, requested_period: str, *, value_kinds: Mapping[str, str] | None = None,
) -> list[str]:
    """Названные пользователем периоды, по которым среди опубликованных значений ничего нет.

    Запрос нескольких периодов отвечен частично, когда часть периодов в отчёте есть,
    а часть нет: без этой строки в `not_performed` пользователь видит карточки и не
    замечает, что один из периодов потерян."""
    dated = [v for v in published if str(getattr(v, "period_ref", "") or "").strip()]
    if not dated:
        return []
    kinds = {str(key).strip().upper(): value for key, value in (value_kinds or {}).items()}
    return [
        label for label in split_period_labels(requested_period)
        if not any(_value_period_matches(label, value, kinds) for value in dated)
    ]


def _ambiguity_message(goal: GoalIR) -> str:
    """Что именно неоднозначно — из `ambiguities` самой модели, не общая фраза. У составной цели —
    из тех частей, где модель назвала неоднозначность; их `unresolved` — тот же остаток разбора."""
    clauses = [clause for clause in goal.clauses() if clause.ambiguities] or goal.clauses()[:1]
    ambiguities = [note for clause in clauses for note in clause.ambiguities]
    unresolved = [item for clause in clauses for item in clause.unresolved]
    bits: list[str] = []
    if ambiguities:
        bits.append("неоднозначные термины: " + "; ".join(
            f"{a.term} ({', '.join(a.candidates)})" if a.candidates else a.term
            for a in ambiguities
        ))
    if unresolved:
        bits.append("не удалось сопоставить: " + ", ".join(unresolved))
    detail = "; ".join(bits)
    return "нужно уточнение" + (f": {detail}" if detail else " — данных для запроса недостаточно")


def _scoped_form_root(goal: GoalIR, roots: Sequence[RootTarget]) -> RootTarget:
    """Срез 29, Этап 3 (владелец подтвердил: scoped FILL_FORM сейчас, не
    multi-root parallel Target). Несколько явно названных зарегистрированных
    показателей за один ход («доходы и расходы») превращаются в ОДИН
    `FormOutput` со scope, ограниченным именно этими концептами —
    `out_form.py::build_form_output`/`_out_form_scope` уже умеют это через
    `root_extra["requested_concepts"]` (тот же путь, что штатный запрос
    формы), поэтому здесь не заводится новый механизм среза, только
    переиспользуется существующий."""
    concepts = sorted({r.root.concept_id for r in roots if r.root.concept_id})
    return RootTarget(
        root=Target(output_type=_FORM_OUTPUT_TYPE, entity_ref=goal.entity_ref, period_ref=goal.period_ref),
        root_extra={"role": goal.role or "", "requested_concepts": ",".join(concepts)},
    )


_DYNAMIC_ROOT_TYPE = "DynamicPlan"
_MANIFEST_TEXT_LIMIT = 2000


def _dynamic_marker_root(goal: GoalIR) -> RootTarget:
    """Корень-заглушка для цели без детерминированного производителя.

    Реальные цели ходов выберет ``DynamicRun``; здесь остаётся только
    идентичность запроса (сущность, период, роль).
    """
    return RootTarget(
        root=Target(output_type=_DYNAMIC_ROOT_TYPE, entity_ref=goal.entity_ref, period_ref=goal.period_ref),
        root_extra={"role": goal.role or ""},
    )


def _full_analysis_root_extra(company_type: str = "non_financial", role: str = "") -> dict[str, str]:
    """Корневой `extra` полного анализа. `role` — роль пользователя: от неё
    зависит состав показателей, сегментов и консолидированной информации
    (`graph_atoms._analyze_full_bundle_scope`)."""
    from finengine.skills.analysis import methodology  # noqa: PLC0415

    effective_methodology = methodology.base()
    extra = {
        "depth": "full",
        "analysis_type": "general",
        "methodology_text": methodology.framework_block(effective_methodology, company_type, "full", ()),
    }
    if role:
        extra["role"] = role
    return extra


def _full_analysis_methodology_version() -> str:
    from finengine.skills.analysis import methodology  # noqa: PLC0415
    from finengine.skills.versioning import methodology_fingerprint  # noqa: PLC0415

    return str(methodology_fingerprint(methodology.base()).get("effective_sha256") or "unversioned")


def _clipped_text(value):
    """Длинные строки доказательств урезаны: манифест уходит в историю диалога, а скачанный
    атомом файл или цитата лежат в `Case` целиком."""
    if isinstance(value, str) and len(value) > _MANIFEST_TEXT_LIMIT:
        return f"{value[:_MANIFEST_TEXT_LIMIT]}…[урезано: всего {len(value)} символов]"
    if isinstance(value, dict):
        return {key: _clipped_text(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_clipped_text(item) for item in value]
    return value


def _run_inputs(
    context: ProductionContext, *, document_text: str, role: str, extra: Mapping[str, str] | None,
) -> RunInputs:
    """Входы прогона. `owner_id` и `project_id` — для файлов: без них `out.file` и файлы атомов уходят
    к владельцу `system`, и пользователь скачать их не может. Проект чата — это `case_id`."""
    return RunInputs(
        document_text=document_text, role=role,
        extra={
            **dict(extra or {}), "owner_id": str(context.owner_id), "project_id": context.case_id,
            **({"trace_id": context.trace_id} if context.trace_id else {}),
        },
    )


LoopOpener = Callable[[LoopSession, LoopOrigin], None]


class SwitchSink:
    """Куда цикл пишет события сейчас: пока идёт стандартный путь — в его поток, после передачи ведущему
    снаружи — туда, куда укажет он (поток стандартного пути к тому времени закрыт)."""

    def __init__(self, target: EventSink | None = None) -> None:
        self.target = target

    def __call__(self, event: dict) -> None:
        if self.target is not None:
            self.target(event)


def _loop_parts(
    context: ProductionContext, goal_ir: GoalIR, target: Target, root_extra: Mapping[str, str],
    document_text: str, event_sink: EventSink | None,
) -> dict:
    """Общая часть сессии цикла для обоих ведущих: службы (`DynamicRun`) и ouroboros (`LoopSession`)."""
    role = goal_ir.role or ""
    return {
        "runtime": context.runtime, "owner_id": context.owner_id, "case_id": context.case_id,
        "goal": goal_ir, "target": target, "run_passport": context.passport, "root_extra": root_extra,
        "inputs": _run_inputs(context, document_text=document_text, role=role, extra=root_extra),
        "event_sink": event_sink,
        "run_scenario": ScenarioChannel(context=context, document_text=document_text, role=role, event_sink=event_sink),
        "run_goals": GoalChannel(context=context, document_text=document_text, role=role, event_sink=event_sink),
    }


def _hand_over_loop(
    open_loop: LoopOpener, context: ProductionContext, goal_ir: GoalIR, target: Target,
    root_extra: Mapping[str, str], document_text: str, event_sink: EventSink | None,
    stage: str, reason: str,
) -> LoopSession:
    """Цикл открыт, но ведёт его не служба: сессия передаётся ведущему вместе с рецептом сборки."""
    session = LoopSession(
        **_loop_parts(context, goal_ir, target, root_extra, document_text, SwitchSink(event_sink)),
        entry_stage=stage,  # type: ignore[arg-type]
    )
    session.open(deterministic_error=reason)
    budget = context.runtime.executor.budget
    open_loop(session, LoopOrigin(
        owner_id=context.owner_id, case_id=context.case_id, document_id=context.document_id,  # type: ignore[arg-type]
        goal=goal_ir, target=target, root_extra=dict(root_extra), trace_id=context.trace_id, entry_stage=stage,
        methodology_version=context.passport.methodology_version,
        budget={"max_wall_seconds": budget.max_wall_seconds, "max_atom_calls": budget.max_atom_calls,
                "max_retries": budget.max_retries},
    ))
    return session


def open_agent_loop(
    *, open_loop: LoopOpener, owner_id: object, case_id: str, document_id: str, document_text: str,
    message: str, role: str | None, event_sink: EventSink | None, trace_id: str = "",
) -> LoopSession:
    """Цикл, который открыл агент (`aifa_open`, ADR-0060 п. 1): сообщение не разбирается, конвейер не запускается.
    Паспорт фиксирует профиль отчёта и реестр; корень шагов несёт только сущность и версию документа."""
    goal = GoalIR(raw_query=message, intent_kind="custom_analysis", role=role)
    context = prepare_production_context(
        owner_id=owner_id, case_id=case_id, document_id=document_id, document_text=document_text,
        goal_ir=goal, budget=_with_wall_floor(None), trace_id=trace_id,
    )
    entity = getattr(context.passport, "entity", None)
    target = Target(
        output_type="ReportOutput", document_version=context.document_version,
        entity_ref=getattr(entity, "entity_id", None) or getattr(entity, "canonical_name", None) or document_id,
    )
    session = LoopSession(
        **_loop_parts(context, goal, target, {}, document_text, SwitchSink(event_sink)), entry_stage="agent",
    )
    session.open(deterministic_error="")
    budget = context.runtime.executor.budget
    open_loop(session, LoopOrigin(
        owner_id=context.owner_id, case_id=context.case_id, document_id=context.document_id,  # type: ignore[arg-type]
        goal=goal, target=target, trace_id=context.trace_id, entry_stage="agent",
        methodology_version=context.passport.methodology_version,
        budget={"max_wall_seconds": budget.max_wall_seconds, "max_atom_calls": budget.max_atom_calls,
                "max_retries": budget.max_retries},
    ))
    return session


def restore_loop_session(
    origin: LoopOrigin, state: dict, *, document_text: str, event_sink: EventSink | None,
    is_cancelled: Callable[[], bool] | None = None,
) -> LoopSession:
    """Сессия цикла после перезапуска процесса: рантайм и каналы — заново, накопленное — из сохранённого."""
    context = prepare_production_context(
        owner_id=origin.owner_id, case_id=origin.case_id, document_id=origin.document_id,
        document_text=document_text, goal_ir=origin.goal, methodology_version=origin.methodology_version,
        budget=ExecutionBudget(**origin.budget) if origin.budget else None, trace_id=origin.trace_id,
    )
    session = LoopSession(
        **_loop_parts(context, origin.goal, origin.target, origin.root_extra, document_text, SwitchSink(event_sink)),
        entry_stage=origin.entry_stage,  # type: ignore[arg-type]
        is_cancelled=is_cancelled,
    )
    restore_state(session, state)
    return session


def publish_result(result: DynamicRunResult, event_sink: EventSink | None, *, case_id: str, document_id: str) -> None:
    """Итог цикла на экран: карточки результатов и файлы — как у цикла, который вела служба."""
    _dynamic_outcome(result, event_sink, case_id=case_id, document_id=document_id)


def _dynamic_outcome(
    result: DynamicRunResult,
    event_sink: EventSink | None,
    *,
    case_id: str,
    document_id: str,
) -> EvidenceGateOutcome:
    """Преобразовать конечное состояние цикла в тот же контракт чата."""
    dynamic_run = result.manifest()
    dynamic_run["evidence"] = _clipped_text(dynamic_run["evidence"])
    dynamic_run["moves"] = _clipped_text(dynamic_run["moves"])
    manifest = {
        "schema_version": "1.0",
        "case_id": case_id,
        "document_id": document_id,
        "dynamic_run": dynamic_run,
    }
    if event_sink is not None:
        for value in typed_results(result.evidence):
            event_sink(typed_result_event(value))
        for file in result.deliverables:           # файл-результат: карточка и кнопка скачивания
            event_sink(typed_result_event(file))
            event_sink(file_event(file))
    return EvidenceGateOutcome(tool_name="financial_pipeline", manifest=manifest)


_AD_HOC_KINDS = ("extract_ad_hoc_metric", "calculate_ad_hoc_metric")
_CONCEPT_CODE = re.compile(r"[A-Z][A-Z0-9_]+")


def unperformed_requests(goal: GoalIR) -> list[str]:
    """Действенный остаток запроса, который детерминированный путь не выполнял.

    Предохранитель ловит только то, что модель положила в `unresolved`. Если разбор
    потерял просьбу целиком (`unresolved` пуст), предохранитель бессилен — это закрывает
    только правильный вид цели (`custom_analysis`). Не остаток: фраза показателя
    `*_ad_hoc_metric` (`unresolved[0]`, из неё строится корень) и `role:<имя>`
    (невалидная роль — другой механизм)."""
    lines: list[str] = []
    for clause in goal.clauses():
        skip = 1 if clause.intent_kind in _AD_HOC_KINDS else 0
        for item in clause.unresolved[skip:]:
            if item.startswith("role:"):
                continue
            lines.append(f"показатель не распознан: {item}" if _CONCEPT_CODE.fullmatch(item) else item)
    return lines


def scenario_summary(goal: GoalIR, outcome: EvidenceGateOutcome | None) -> ScenarioResult:
    """Короткий итог вложенного детерминированного сценария для планировщика."""
    if outcome is None:
        return ScenarioResult(False, "✗ сценарий не исполним детерминированно")
    error = outcome.manifest.get("error")
    if error:
        code = error.get("code") or "scenario_failed"
        message = error.get("message") or "без описания причины"
        return ScenarioResult(False, f"✗ {code}: {message}")
    result = outcome.manifest.get("result") or {}
    output_type = result.get("output_type") or "результат"
    goal_type = outcome.manifest.get("goal_type") or goal.intent_kind
    return ScenarioResult(True, f"✓ {goal_type}: {output_type}")


@dataclass(frozen=True)
class _NestedChannel:
    """Цель внутри цикла идёт тем же конвейером, что сообщение чата, но без входа в цикл (Р13.3)."""

    context: ProductionContext
    document_text: str
    role: str
    event_sink: EventSink | None

    def _execute(self, goal: GoalIR) -> ScenarioResult:
        outcome = execute_routed_goal(
            owner_id=self.context.owner_id,
            case_id=self.context.case_id,
            document_id=self.context.document_id,
            document_text=self.document_text,
            goal=goal,
            store=self.context.runtime.store,
            event_sink=self._progress_only,
            allow_dynamic=False,
        )
        return scenario_summary(goal, outcome)

    def _progress_only(self, event: dict) -> None:
        _progress_only(self.event_sink, event)


@dataclass(frozen=True)
class ScenarioChannel(_NestedChannel):
    """Сценарий цикла проходит тот же разбор и конвейер, что сообщение чата."""

    def __call__(self, request: str) -> ScenarioResult:
        try:
            decision = asyncio.run(parse_message(
                request, context=IngressContext(ui_role=self.role), allow_answer=False,
            ))
        except IntentParseError as exc:
            return ScenarioResult(False, f"✗ запрос не разобран: {exc}")
        return self._execute(decision.goal)


def goal_from_clauses(clauses: Sequence[GoalClause]) -> GoalIR:
    """Цель для конвейера из частей уже разобранной цели: поля частей переносятся как есть, модель-классификатор
    не вызывается. Из `depends_on` остаются связи внутри набора — опора, полученная раньше, уже лежит в Case, а
    ссылка на часть вне графа сделала бы его несобираемым."""
    refs = {clause.goal_ref for clause in clauses}
    local = [
        clause.model_copy(update={"depends_on": [ref for ref in clause.depends_on if ref in refs]})
        for clause in clauses
    ]
    primary, *rest = local
    return GoalIR(**dict(primary), additional_goals=rest)


@dataclass(frozen=True)
class GoalChannel(_NestedChannel):
    """Части разобранной цели исполняются по ссылке `goal_ref`: тот же `execute_routed_goal` с `allow_dynamic=False`
    и тот же Case, что у сообщения чата, но цель строится из `GoalClause` без разбора фразы (Р18.3)."""

    def refusal(self, clause: GoalClause) -> str | None:
        """Почему часть нельзя исполнить по ссылке: у неё нет детерминированного корня (Р18.4)."""
        if clause.intent_kind not in _GATED_KINDS:
            return f"у вида {clause.intent_kind} нет детерминированного корня"
        try:
            root_from_goal(clause)
        except (NotImplementedError, ValueError) as exc:
            return str(exc)
        return None

    def __call__(self, clauses: Sequence[GoalClause]) -> ScenarioResult:
        return self._execute(goal_from_clauses(clauses))


def execute_routed_goal(
    *,
    open_loop: LoopOpener | None = None,
    owner_id: object,
    case_id: str,
    document_id: str,
    document_text: str,
    goal: GoalIR,
    store: SqlCaseStore | None = None,
    event_sink: EventSink | None = None,
    diagnostics: dict | None = None,
    allow_dynamic: bool = True,
    is_cancelled: Callable[[], bool] | None = None,
    trace_id: str = "",
) -> EvidenceGateOutcome | None:
    """Синхронный вход канонического конвейера для цели, выбранной моделью-
    маршрутизатором. Вызывать ТОЛЬКО через `stream_blocking`/`asyncio.to_thread`,
    никогда напрямую из корутины (см. архитектурную запись выше).

    `None` — вид цели ещё не подключён к детерминированному графу; ход
    продолжается свободным диспетчером. Любой другой исход — `EvidenceGateOutcome`
    (успех либо типизированный отказ); неожиданные исключения поднимаются как
    есть — их логирует и превращает в `evidence_gate_failed` вызывающий код."""
    if diagnostics is not None:
        # Контекст для ERROR-лога вызывающего кода: какой GoalType к моменту сбоя.
        diagnostics["intent_kind"] = goal.intent_kind

    def _refuse(code: str, message: str, *, retryable: bool = True) -> EvidenceGateOutcome:
        return _refusal_outcome(
            event_sink, case_id=case_id, document_id=document_id,
            code=code, message=message, retryable=retryable,
        )

    if any(clause.ambiguities for clause in goal.clauses()):
        return _refuse("goal_needs_clarification", _ambiguity_message(goal))

    # Срез 31, П8; часть 13 доводки: любой сбой детерминированного ПЛАНИРОВАНИЯ — вид цели без
    # корня, корень части не построился, общий граф не собрался (ниже, у `plan_many`) — это один
    # `DynamicRun` на всё сообщение, для одиночной и составной цели одинаково. Детерминированный
    # путь пропускается, причина идёт агенту как ошибка планировщика. Единственное исключение —
    # неоднозначность: она остаётся вопросом пользователю (`goal_needs_clarification`).
    dynamic_reason: str | None = None
    roots: list[RootTarget] = []
    clauses = goal.clauses()
    if len(clauses) > 1:
        unsupported = [clause.intent_kind for clause in clauses if clause.intent_kind not in _GATED_KINDS]
        if unsupported:
            dynamic_reason = (
                "составной запрос содержит GoalType без производителя корня в детерминированном графе: "
                + ", ".join(unsupported)
            )
        else:
            try:
                for clause in clauses:
                    clause_roots = root_from_goal(clause)
                    if len(clause_roots) > 1:
                        scoped = _scoped_form_root(clause, clause_roots)._replace(
                            goal_ref=clause.goal_ref, goal_type=clause.intent_kind,
                            depends_on=tuple(clause.depends_on),
                        )
                        clause_roots = [scoped]
                    roots.extend(clause_roots)
            except (NotImplementedError, ValueError) as exc:
                dynamic_reason = str(exc)
            if not roots and dynamic_reason is None:
                return _refuse("goal_needs_clarification", _ambiguity_message(goal))
    elif goal.intent_kind not in _GATED_KINDS:
        dynamic_reason = f"GoalType {goal.intent_kind!r} не имеет производителя корня в детерминированном графе"
    else:
        try:
            roots = list(root_from_goal(goal))
        except (NotImplementedError, ValueError) as exc:
            # Например, calculate_ad_hoc без доверенной формулы: модель не смогла
            # составить проверяемое выражение и описание компонентов.
            dynamic_reason = str(exc)
        if not roots and dynamic_reason is None:
            return _refuse("goal_needs_clarification", _ambiguity_message(goal))
    if dynamic_reason is not None and not allow_dynamic:
        return _refuse("scenario_not_deterministic", dynamic_reason, retryable=False)
    if dynamic_reason is not None:
        roots = [_dynamic_marker_root(goal)]
    elif len(roots) > 1 and len(clauses) == 1:
        # Several metric roots of ONE clause are one scoped table. Roots of
        # DIFFERENT clauses must remain separate: ``execute_production_goal``
        # composes them with ``plan_many`` and preserves scenario metadata.
        roots = [_scoped_form_root(goal, roots)]

    # Цикл получает ту же обвязку, что одиночная `custom_analysis`: методика анализа, форма и
    # бюджет «составных показателей» относятся к детерминированному графу и не зависят от вида
    # основной части цели, ушедшей в цикл.
    deterministic = dynamic_reason is None
    root_extra: dict[str, str] = {}
    methodology_version = None
    if deterministic and goal.intent_kind in _ANALYSIS_KINDS:
        methodology_version = _full_analysis_methodology_version()
        root_extra.update(_full_analysis_root_extra(role=goal.role or ""))
    elif deterministic and goal.intent_kind == "form":
        root_extra["role"] = goal.role or ""
        root_extra["period_scope"] = "selected" if goal.period_ref else "all_available"

    budget = None
    if deterministic and goal.intent_kind in _ANALYSIS_KINDS:
        budget = _FULL_ANALYSIS_BUDGET
    elif deterministic and (len(goal.targets) > 1 or len(clauses) > 1):
        budget = _COMPOUND_METRIC_BUDGET
    budget = _with_wall_floor(budget)
    context = prepare_production_context(
        owner_id=owner_id, case_id=case_id, document_id=document_id, document_text=document_text,
        goal_ir=goal, methodology_version=methodology_version, store=store,
        budget=budget, trace_id=trace_id,
    )
    if deterministic and goal.intent_kind in _ANALYSIS_KINDS:
        # Профиль зафиксирован только теперь — пересобрать методический блок
        # под настоящий company_type (та же двухшаговая логика, что уже
        # проверена в `metric_run.py`).
        root_extra.update(_full_analysis_root_extra(
            context.passport.classification.company_type, role=goal.role or "",
        ))

    # Ход и топология (`plan_graph`, `node_run`) идут клиенту сразу, а итоговый
    # typed_result удерживается до проверки периода: несовпавший результат не
    # должен мелькнуть в UI успешной карточкой, прежде чем gate превратит его в
    # типизированный отказ.
    # Часть 14: ошибка самого исполнения (`graph_execution_failed`) удерживается так же, как итоговый
    # `typed_result`: если задачу подхватит цикл, пользователь её не видит (Р14.4); иначе она уходит
    # клиенту перед отказом, в прежнем порядке.
    held_errors: list[dict] = []

    def _checked_event_sink(event: dict) -> None:
        if event.get("type") == "typed_result":
            return
        if event.get("type") == "typed_error" and (event.get("error") or {}).get("code") == _GRAPH_FAILED_CODE:
            held_errors.append(event)
            return
        if event_sink is not None:
            event_sink(event)

    # Единственное место входа в цикл после подготовки контекста — и для части 13 (общий граф не
    # собрался, путь 3), и для части 14 (граф собрался и упал). Рантайм уже собран — цикл идёт с ним.
    cycle_entry: tuple[str, BaseException | None, str] | None = None
    try:
        outcome = execute_production_goal(
            context, goal, document_text=document_text, root_extra=root_extra,
            event_sink=_checked_event_sink, roots=roots,
            allow_dynamic=allow_dynamic, dynamic_reason=dynamic_reason, is_cancelled=is_cancelled,
            open_loop=open_loop,
        )
    except PlanInfeasible as exc:
        if not allow_dynamic:
            return _refuse("scenario_not_deterministic", str(exc), retryable=False)
        cycle_entry = (str(exc), exc.__cause__, "planning")
    else:
        failure = _execution_failure(outcome, roots) if allow_dynamic else None
        if failure is not None:
            cycle_entry = (failure[0], failure[1], "execution")
    if cycle_entry is not None and is_cancelled is not None and is_cancelled():
        # Р14.3: пользователь остановил ход. Рабочий поток дорабатывает сам, но цикл — это ходы модели
        # ради ответа, который никто не прочтёт; остановка приходит `BaseException`, как и в других местах.
        raise asyncio.CancelledError
    if cycle_entry is None and event_sink is not None:
        for held in held_errors:
            event_sink(held)
    if cycle_entry is not None:
        reason, cause, stage = cycle_entry
        roots = [_dynamic_marker_root(goal)]
        outcome = execute_production_goal(
            context, goal, document_text=document_text, event_sink=_checked_event_sink,
            roots=roots, allow_dynamic=True, dynamic_reason=reason, dynamic_cause=cause,
            dynamic_stage=stage, is_cancelled=is_cancelled, open_loop=open_loop,
        )

    if outcome.loop is not None:
        return EvidenceGateOutcome(tool_name="financial_pipeline", manifest={
            "schema_version": "1.0", "case_id": case_id, "document_id": document_id, "loop_opened": True,
        })
    if outcome.dynamic is not None:
        return _dynamic_outcome(
            outcome.dynamic, event_sink, case_id=case_id, document_id=document_id,
        )

    matches, root_output_types = _root_matches(outcome, roots)
    root_output_type = root_output_types[-1]
    if outcome.refusal_event is not None or not matches or not outcome.succeeded:
        refusal = outcome.refusal_event or _incomplete_refusal(goal, roots, outcome, root_output_type)
        if event_sink is not None and outcome.refusal_event is None:
            event_sink(refusal)
        return EvidenceGateOutcome(
            tool_name="financial_pipeline",
            manifest={"schema_version": "1.0", "case_id": case_id, "document_id": document_id, **refusal},
        )

    requested_period = str(goal.period_ref or "").strip()
    ad_hoc_value_kinds = {
        str(root.root.concept_id or "").strip().upper(): root.ad_hoc_spec.value_kind
        for root in roots
        if root.root.concept_id and hasattr(root.ad_hoc_spec, "value_kind")
    }
    published, found_periods = _select_published(
        matches, requested_period, value_kinds=ad_hoc_value_kinds,
    )
    if requested_period and not published and found_periods:
        return _refuse(
            "canonical_period_mismatch",
            f"в отчёте нет данных за запрошенный период {requested_period!r}; "
            f"найдены периоды: {', '.join(found_periods)}",
        )
    # Значения без собственного периода (форма, отчёт) публикуются как есть.
    published = published or matches

    if event_sink is not None:
        # Карточки — только для типизированных результатов UI (`typed_results`);
        # остальное (цитата, структура, ...) доступно диспетчеру через манифест.
        for value in typed_results(published):
            event_sink(typed_result_event(value))
    terminal = published[-1]
    manifest = {
        "schema_version": "1.0",
        "case_id": case_id,
        "document_id": document_id,
        "document_version": context.document_version,
        "goal_type": goal.intent_kind,
        "goal_types": [clause.intent_kind for clause in goal.clauses()],
        "period_scope": "selected" if requested_period else "all_available",
        "result": terminal.model_dump(mode="json"),
        "not_performed": [
            *unperformed_requests(goal),
            *(
                f"нет данных за период {label} (найдены: {', '.join(found_periods)})"
                for label in _unanswered_periods(published, requested_period, value_kinds=ad_hoc_value_kinds)
            ),
        ],
    }
    if len(published) > 1:
        manifest["results"] = [value.model_dump(mode="json") for value in published]
    ad_hoc_spec = roots[0].ad_hoc_spec
    if ad_hoc_spec is not None:
        # Придуманные моделью имя/синонимы/определение либо формула и допущения
        # — чтобы диспетчер честно сказал пользователю, ЧТО именно искали/считали.
        manifest["ad_hoc_definition"] = ad_hoc_spec.model_dump(mode="json")
    return EvidenceGateOutcome(tool_name="financial_pipeline", manifest=manifest)


__all__ = [
    "EvidenceGateOutcome",
    "GoalChannel",
    "ProductionContext",
    "ProductionOutcome",
    "ProductionRootResult",
    "ScenarioChannel",
    "execute_production_goal",
    "goal_from_clauses",
    "prepare_production_context",
    "execute_routed_goal",
]
