"""Автор временных атомов: модель пишет run(inputs) по контракту и чинит его по ошибке.

Автор сам гоняет самопроверку в комнате станции до `ATTEMPTS` раз — на реальных входах из `Case`,
если они есть, иначе на примере автора. Атомам-загрузкам автор не нужен.
"""
from __future__ import annotations

import asyncio
import json
import re
from functools import partial
from pathlib import Path

from pydantic import BaseModel

import finengine.contracts as contracts
from finengine.config import settings
from finengine.llm.budget import BudgetExceeded
from finengine.skills.spec.atom_intake import check_code
from finengine.skills.spec.ephemeral import AtomRequest, EphemeralAtom, group_inputs
from finengine.skills.spec.room_files import FileSpec
from finengine.skills.spec.target import InputSpec
from finengine.store.cases import evidence_id_of

_SKILL = Path(__file__).with_name("atom_author.md")
ATTEMPTS = 3
_REAL_INPUT_PREVIEW = 6000
_PREVIEW_ITEMS = 2
_SCHEMA_PREVIEW = 3000
_FINDING = "VerificationFinding"
_CHECKS_BY_TYPE = {
    "ExtractedMetric": "Показатели приходят после проверок norm.unit, norm.time и verify.bounds.",
    "CalculatedMetric": "Показатели приходят после проверок verify.identity, verify.cross, norm.time и verify.bounds.",
}
_NO_CHECKS = "Проверки norm.* и verify.bounds к этому типу не применяются — не считай данные проверенными."


class StationFault(str):
    """Причина отказа `write()`: сбой станции, а не кода. Для вызывающего это обычная строка,
    но `isinstance(reason, StationFault)` отличает «станция не работает» от «автор не справился»:
    во втором случае атом можно описать иначе, в первом — повтор бессмыслен, пока станцию не починят."""


class AtomAuthor:
    def __init__(self, complete_fn=None, attempts: int = ATTEMPTS):
        self.complete_fn = complete_fn or _default_complete
        self.attempts = attempts

    def write(
        self, request: AtomRequest, *, inputs_doc: str, real_inputs: dict | None,
        previous_code: str | None = None, error: str | None = None, on_attempt=None,
        expected_files: tuple[FileSpec, ...] = (), trace_id: str = "",
    ) -> tuple[EphemeralAtom | None, str | None]:
        """(атом, None) — код прошёл самопроверку; (None, причина) — не справился за N попыток.

        Причина — `StationFault`, если станция не исполнила код: ошибку автору тогда не шлём,
        попытки не тратим и цикл прерываем сразу.

        `on_attempt(номер, статус, ошибка, код)` — необязательный наблюдатель для ленты UI: вызывается
        после каждой попытки, на результат не влияет.

        `expected_files` — файлы из плана цикла: файл такого формата, собранный этим кодом, должен быть не
        беднее обещанного (листы, формулы), иначе это ошибка автора, а не лишний ход планировщика."""
        system = _SKILL.read_text(encoding="utf-8")
        code = previous_code
        reason = error
        for number in range(1, self.attempts + 1):
            prompt = render_author_prompt(request, inputs_doc, real_inputs, code, error, expected_files)
            try:
                raw = asyncio.run(self.complete_fn(system, prompt))
            except BudgetExceeded:
                raise
            except Exception as exc:  # noqa: BLE001 — сбой шлюза модели (сеть, лимит, 5xx): попытка, не падение
                reason = f"модель не ответила: {type(exc).__name__}: {exc}"   # автору не показываем: это не его код
                _report_attempt(on_attempt, number, "no_response", reason, code)
                continue
            try:
                reply = json.loads(raw)
                code, example = str(reply["code"]), reply.get("example_inputs") or {}
                if not isinstance(example, dict):
                    raise TypeError("example_inputs не объект")
            except (ValueError, KeyError, TypeError) as exc:
                error = reason = f"ответ не в формате {{code, example_inputs}}: {exc!r}"
                _report_attempt(on_attempt, number, "bad_format", error, code)
                continue
            verdict = check_code(request, code, real_inputs or example, expected_files=expected_files, trace_id=trace_id)
            if verdict.station_fault:
                _report_attempt(on_attempt, number, "station_fault", verdict.error, code)
                return None, StationFault(verdict.error)
            error = reason = verdict.error
            _report_attempt(on_attempt, number, "ok" if error is None else "error", error, code)
            if error is None:
                return EphemeralAtom(request=request, code=code), None
        return None, reason


def _report_attempt(on_attempt, number: int, status: str, error: str | None, code: str | None) -> None:
    if on_attempt is not None:
        on_attempt(number, status, error, code or "")


def render_author_prompt(
    request: AtomRequest, inputs_doc: str, real_inputs: dict | None, code: str | None, error: str | None,
    expected_files: tuple[FileSpec, ...] = (),
) -> str:
    parts = [
        f"## Атом\nname: {request.name}\npurpose: {request.purpose}",
        f"## Входы — inputs\n{inputs_doc}",
        "## Результат — output_schema\n" + json.dumps(request.output_schema, ensure_ascii=False, indent=1),
    ]
    if expected_files:
        wishes = "\n".join(f"- {spec.text()}" for spec in expected_files)
        parts.append(f"## Требования к файлу\nЕсли этот атом создаёт файл такого формата, в нём должно быть:\n{wishes}")
    if real_inputs:
        parts.append(f"## Реальный вход (сокращён)\n{_input_preview(real_inputs)}")
    if code:
        parts.append(f"## Твой прошлый код\n```python\n{code}\n```")
    if error:
        parts.append(f"## Ошибка\n{error}\nИсправь причину и пришли код целиком.")
    return "\n\n".join(parts)


def _input_preview(real_inputs: dict) -> str:
    """Первые объекты КАЖДОГО типа: обрезка общего JSON с начала прятала от автора все типы, кроме первого
    (dev 06.10.2026: 83 тыс. знаков входа, ни одного `CalculatedMetric` в первых 6000)."""
    share = _REAL_INPUT_PREVIEW // len(real_inputs)
    counts = ", ".join(f"{label}: {len(items)}" for label, items in real_inputs.items())
    samples = "\n".join(
        f'"{label}": {json.dumps(items[:_PREVIEW_ITEMS], ensure_ascii=False)[:share]}'
        for label, items in real_inputs.items()
    )
    return f"всего объектов — {counts}; ниже первые {_PREVIEW_ITEMS} каждого типа\n{samples}"


def describe_inputs(request: AtomRequest, atoms: dict[str, EphemeralAtom]) -> str:
    """Формы входов для автора: класс контракта — его JSON Schema; dyn:* — схема производителя.

    Так же описано, что код получает в графе: числа приходят за гейтами, а рядом с ними — группа
    `VerificationFinding` (находки проверок). Типам без гейтов норм/границ проверенность не обещается."""
    schemas = {atom.request.output_type: atom.request.output_schema for atom in atoms.values()}
    types = list(dict.fromkeys(spec.input_type for spec in request.requires))
    lines = [
        _describe_input(
            input_type, schemas,
            tuple(dict.fromkeys(spec.concept for spec in request.requires
                                if spec.input_type == input_type and spec.concept is not None)),
        )
        for input_type in types
    ]
    if any(not input_type.startswith("dyn:") for input_type in types):
        lines.append(f'- "{_FINDING}": находки проверок; приходят вместе с показателями, '
                     "если они тебе не нужны — игнорируй.")
    return "\n".join(lines)


def case_inputs(
    store, owner_id, case_id: str, request: AtomRequest, atoms: dict[str, EphemeralAtom] | None = None,
) -> dict | None:
    """Реальные входы атома из Case в том виде, в каком код получит их в графе (`group_inputs` от
    `ctx.dependencies()`); None, если какого-то входа ещё нет.

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
        [row for row, flag in zip(rows, is_input) if not flag and row["output_type"] == _FINDING], producers)
    related = [(value, row) for value, row in findings
               if set(row["dependency_evidence_ids"] or ()) & set(position)]
    related.sort(key=partial(_first_dependency, position=position))
    return group_inputs(tuple(value for value, _ in wanted + related))


def _requires_rank(item: tuple[BaseModel, dict], requires: tuple[InputSpec, ...]) -> int:
    row = item[1]
    return next(index for index, spec in enumerate(requires)
                if row["output_type"] == spec.input_type and spec.concept in (None, row["concept"]))


def _first_dependency(item: tuple[BaseModel, dict], position: dict) -> int:
    return min(position.get(evidence_id, len(position)) for evidence_id in item[1]["dependency_evidence_ids"] or ())


def _is_input(row: dict, requires: tuple[InputSpec, ...]) -> bool:
    return any(row["output_type"] == spec.input_type and spec.concept in (None, row["concept"])
               for spec in requires)


def _has_input(items: list[tuple[BaseModel, dict]], spec: InputSpec) -> bool:
    return any(row["output_type"] == spec.input_type and spec.concept in (None, row["concept"])
               for _, row in items)


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


def _describe_input(input_type: str, schemas: dict[str, dict], concepts: tuple[str, ...] = ()) -> str:
    concept_text = ""
    if concepts:
        concept_text = " (concept: " + ", ".join(json.dumps(item, ensure_ascii=False) for item in concepts) + ")"
    if input_type.startswith("dyn:"):
        if input_type not in schemas:
            return f'- "{input_type}"{concept_text}: объекты DynamicArtifact; форма payload не описана'
        schema = json.dumps(schemas[input_type], ensure_ascii=False)
        return f'- "{input_type}"{concept_text}: объекты DynamicArtifact; данные — в поле payload по схеме {schema}'
    schema = json.dumps(getattr(contracts, input_type).model_json_schema(), ensure_ascii=False)
    checks = _CHECKS_BY_TYPE.get(input_type, _NO_CHECKS)
    return f'- "{input_type}"{concept_text}: объекты по схеме {schema[:_SCHEMA_PREVIEW]}\n  {checks}'


# `zai-org/GLM-5.2` на Cloud.ru отдаёт верный JSON в ограде ```json: `response_format` ему не указ.
_MARKDOWN_FENCE = re.compile(r"\s*```(?:json)?\s*\n(.*?)\s*```\s*", re.DOTALL)


def without_markdown_fence(reply: str) -> str:
    fenced = _MARKDOWN_FENCE.fullmatch(reply)
    return fenced.group(1) if fenced else reply


async def _default_complete(system: str, prompt: str) -> str:
    # Ленивый импорт: единый шлюз модели без тяжёлого импорта на уровне модуля.
    # (код станции не должен тянуть клиент модели, страж test_station_has_what_the_code_imports).
    from finengine.llm import openrouter  # noqa: PLC0415

    return without_markdown_fence(await asyncio.wait_for(
        openrouter.complete(
            prompt, model=settings.atom_author_model or None, system=system,
            response_format={"type": "json_object"}, temperature=0.0,
            retries=1, max_output=6000, reasoning_effort="low",
        ),
        timeout=settings.atom_author_model_timeout_seconds,
    ))
