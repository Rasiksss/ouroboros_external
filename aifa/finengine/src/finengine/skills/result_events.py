"""Bridge GraphExecutor records to the durable A11 journal vocabulary."""
from __future__ import annotations

from collections.abc import Iterable

from pydantic import BaseModel

from finengine.contracts import (
    CalculatedMetric,
    ExtractedMetric,
    FileArtifact,
    FormOutput,
    MonitorResult,
    NarrativeOutput,
    NodeRun,
    PeriodChangeReport,
    PlanGraph,
    ReportOutput,
)

TypedResultModel = (
    ReportOutput | FormOutput | NarrativeOutput | FileArtifact
    | ExtractedMetric | CalculatedMetric | MonitorResult | PeriodChangeReport
)


def node_run_event(node_run: NodeRun, *, plan_graph_hash: str) -> dict:
    """Progress is a projection of the real NodeRun state machine."""
    return {
        "type": "node_run",
        "plan_graph_hash": plan_graph_hash,
        "node_run": node_run.model_dump(mode="json"),
    }


def plan_graph_event(graph: PlanGraph) -> dict:
    """Срез 29, Этап 7 — ОДНОРАЗОВОЕ (за исполнение) событие топологии.

    Эмитится ровно один раз, СРАЗУ после того как граф зафиксирован
    (`CaseRuntime.execute`, до первого `node_run`) — топология не меняется
    после планирования, меняются только статусы узлов, которые уже несёт
    `node_run_event`. Владелец видит, ЧТО построилось, до того как что-либо
    начало исполняться — план 29 §5, Этап 7.

    `node_key` каждого узла сериализован тем же способом, что уже несёт
    `NodeRun.node_key` в `node_run_event` (`atom_name`/`atom_version`/
    `args_fingerprint`) — фронт связывает узел топологии с его же
    последующими обновлениями статуса по этому триплету, не по отдельному
    новому идентификатору.
    """
    return {
        "type": "plan_graph",
        "graph_hash": graph.graph_hash,
        # Срез 31: UI различает план, собранный детерминированным планировщиком, и
        # план агента-планировщика (`dynamic-…`).
        "planner_version": graph.planner_version,
        "root": graph.root.model_dump(mode="json"),
        "nodes": [
            {
                "node_key": node.node_key.model_dump(mode="json"),
                "produces": list(node.produces),
                "requires": [key.model_dump(mode="json") for key in node.requires],
                "sandbox_profile": node.sandbox_profile,
                "status": "pending",
                "scenario_refs": list(node.scenario_refs),
            }
            for node in graph.nodes
        ],
        "scenarios": [scenario.model_dump(mode="json") for scenario in graph.scenarios],
    }


_TRACE_DETAILS_LIMIT = 2000
_TRACE_CODE_LIMIT = 8000


def dynamic_move_event(
    *, move: int, status: str, action: str, objective: str = "", reason: str = "",
    scenarios=(), goals=(), new_atoms=(), steps=(), outcome: str = "", details: str = "",
) -> dict:
    """Ход гибридного планировщика для ленты UI. `status`: running (решение принято, исполняется),
    done (итог известен), rejected (ответ planner не принят). Один `move` приходит несколько раз —
    UI обновляет карточку по номеру хода. `goals` — части разобранной цели, исполняемые по ссылке:
    `{goal_ref, description}`; в записях до части 18 поля нет."""
    return {
        "type": "dynamic_move", "move": move, "status": status, "action": action,
        "objective": objective, "reason": reason, "scenarios": list(scenarios),
        "goals": list(goals), "new_atoms": list(new_atoms), "steps": list(steps),
        "outcome": outcome, "details": details[:_TRACE_DETAILS_LIMIT],
    }


def dynamic_plan_event(*, plan, files=()) -> dict:
    """План гибридного планировщика целиком: части работы по порядку и обещанные файлы. Приходит до
    первого хода (фаза планирования) и снова, если планировщик переписал план, — UI заменяет прежний."""
    return {"type": "dynamic_plan", "plan": list(plan), "files": list(files)}


def author_attempt_event(
    *, move: int, atom: str, attempt: int, status: str, error: str | None = None, code: str = "",
) -> dict:
    """Попытка автора написать код атома. `status`: ok, error (код не прошёл самопроверку),
    bad_format (ответ не {code, example_inputs}), no_response (шлюз модели), station_fault."""
    return {
        "type": "author_attempt", "move": move, "atom": atom, "attempt": attempt, "status": status,
        "error": error, "code": code[:_TRACE_CODE_LIMIT],
    }


def typed_result_event(result: TypedResultModel) -> dict:
    status = getattr(result, "status", "complete")
    return {
        "type": "typed_result",
        "status": "partial" if status in {"partial", "blocked"} else "complete",
        "result": result.model_dump(mode="json"),
    }


def file_event(file: FileArtifact) -> dict:
    """Кнопка скачивания в чате. `file_id` — имя объекта без расширения: так его принимает
    `/projects/{project_id}/sources/{file_id}/file` (ключ `<префикс>/<file_id>.<ext>`)."""
    name = file.object_store_location.rsplit("/", 1)[1]
    return {
        "type": "file", "file_id": name.rsplit(".", 1)[0], "filename": file.filename, "kind": file.file_kind,
    }


def typed_error_event(
    *,
    code: str,
    message: str,
    retryable: bool = False,
    node_key: str | None = None,
    partial_results: Iterable[TypedResultModel] = (),
) -> dict:
    return {
        "type": "typed_error",
        "error": {
            "code": code,
            "message": message,
            "retryable": retryable,
            "node_key": node_key,
            "details": {},
        },
        "partial_results": [value.model_dump(mode="json") for value in partial_results],
    }


def typed_results(values: Iterable[BaseModel]) -> tuple[TypedResultModel, ...]:
    return tuple(
        value for value in values
        if isinstance(value, (
            ReportOutput, FormOutput, NarrativeOutput, FileArtifact,
            ExtractedMetric, CalculatedMetric, MonitorResult, PeriodChangeReport,
        ))
    )


__all__ = ["node_run_event", "typed_result_event", "typed_error_event", "typed_results", "file_event"]
