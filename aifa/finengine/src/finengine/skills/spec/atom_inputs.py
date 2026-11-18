"""Входы временного атома из `Case` в том виде, в каком код получает их в графе, и конец цепочки гейтов.

Общее для проверки кода атома (`LoopSession._real_inputs`, `atom_intake`) и таблицы артефактов цикла.
"""
from __future__ import annotations

import re
from functools import partial

from pydantic import BaseModel

from finengine.skills.spec.ephemeral import AtomRequest, EphemeralAtom, group_inputs
from finengine.skills.spec.target import InputSpec
from finengine.store.cases import evidence_id_of

FINDING = "VerificationFinding"


def case_inputs(
    store, owner_id, case_id: str, request: AtomRequest, atoms: dict[str, EphemeralAtom] | None = None,
) -> dict | None:
    """Реальные входы атома из Case в том виде, в каком код получит их в графе (`group_inputs` от
    `ctx.dependencies()`); None, если какого-то входа ещё нет."""
    values = case_input_values(store, owner_id, case_id, request, atoms)
    return None if values is None else group_inputs(values)


def case_input_values(
    store, owner_id, case_id: str, request: AtomRequest, atoms: dict[str, EphemeralAtom] | None = None,
) -> tuple[BaseModel, ...] | None:
    """Значения входов атома из Case в порядке `requires`, затем находки проверок; None, если входа ещё нет.

    В Case каждая стадия цепочки гейтов оставляет свою запись (read.value → norm.unit → norm.time →
    verify.bounds), так что один показатель лежит там несколько раз. Граф отдаёт коду конец цепочки,
    поэтому берутся значения, которые не заменила следующая стадия того же типа, и находки проверок,
    выведенные из них. `atoms` — текущие временные атомы: результат старой версии производителя
    `dyn:*` автору не показывается."""
    rows = store.list_artifacts(owner_id, case_id)
    producers = {atom.request.output_type: atom.producer for atom in (atoms or {}).values()}
    is_input = [_is_input(row, request.requires) for row in rows]
    wanted = end_of_chain([row for row, flag in zip(rows, is_input) if flag], producers)
    if not all(_has_input(wanted, spec) for spec in request.requires):
        return None
    # Граф отдаёт входы в порядке requires атома, и код вправе брать `inputs[тип][0]`: порядок тот же.
    wanted.sort(key=partial(_requires_rank, requires=request.requires))
    position = {evidence_id_of(value): index for index, (value, _) in enumerate(wanted)}
    findings = end_of_chain(
        [row for row, flag in zip(rows, is_input) if not flag and row["output_type"] == FINDING], producers)
    related = [(value, row) for value, row in findings
               if set(row["dependency_evidence_ids"] or ()) & set(position)]
    related.sort(key=partial(_first_dependency, position=position))
    return tuple(value for value, _ in wanted + related)


def inputs_in_case(store, owner_id, case_id: str, request: AtomRequest) -> str:
    """Что из нужных атому типов в Case есть — для отказа `нет_входов`: автор видит, чем поправить `requires`."""
    rows = store.list_artifacts(owner_id, case_id)
    lines = []
    for input_type in dict.fromkeys(spec.input_type for spec in request.requires):
        concepts = sorted({row["concept"] or "" for row in rows if row["output_type"] == input_type})
        if not concepts:
            lines.append(f"{input_type} — нет")
        elif concepts == [""]:
            lines.append(f"{input_type} — есть, без концепта (concept не указывай)")
        else:
            lines.append(f"{input_type} — концепты: {', '.join(concept for concept in concepts if concept)}")
    return "; ".join(lines)


def _matches(row: dict, spec: InputSpec) -> bool:
    """У записи без концепта (форма, отчёт, `dyn:*`) концепт запроса не проверяется (ADR-0060 п. 3)."""
    return row["output_type"] == spec.input_type and (
        spec.concept is None or not row["concept"] or row["concept"] == spec.concept
    )


def _requires_rank(item: tuple[BaseModel, dict], requires: tuple[InputSpec, ...]) -> int:
    return next(index for index, spec in enumerate(requires) if _matches(item[1], spec))


def _first_dependency(item: tuple[BaseModel, dict], position: dict) -> int:
    return min(position.get(evidence_id, len(position)) for evidence_id in item[1]["dependency_evidence_ids"] or ())


def _is_input(row: dict, requires: tuple[InputSpec, ...]) -> bool:
    return any(_matches(row, spec) for spec in requires)


def _has_input(items: list[tuple[BaseModel, dict]], spec: InputSpec) -> bool:
    return any(_matches(row, spec) for _, row in items)


def end_of_chain(rows: list[dict], producers: dict[str, str]) -> list[tuple[BaseModel, dict]]:
    """Значения рядов без заменённых следующей стадией, без дублей и без старых версий dyn:*."""
    replaced: dict[str, set[str]] = {}
    for row in rows:
        own = set(row["evidence_ids"] or ())
        replaced.setdefault(row["output_type"], set()).update(set(row["dependency_evidence_ids"] or ()) - own)
    items, seen = [], set()
    for row in rows:
        for value in row["payloads"]:
            evidence_id = evidence_id_of(value)
            if evidence_id and (evidence_id in replaced[row["output_type"]] or evidence_id in seen):
                continue
            if row["output_type"] in producers and getattr(value, "producer", None) != producers[row["output_type"]]:
                continue
            seen.add(evidence_id)
            items.append((value, row))
    return items


# `zai-org/GLM-5.2` на Cloud.ru отдаёт верный JSON в ограде ```json: `response_format` ему не указ.
_MARKDOWN_FENCE = re.compile(r"\s*```(?:json)?\s*\n(.*?)\s*```\s*", re.DOTALL)


def without_markdown_fence(reply: str) -> str:
    fenced = _MARKDOWN_FENCE.fullmatch(reply)
    return fenced.group(1) if fenced else reply
