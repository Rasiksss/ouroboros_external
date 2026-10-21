"""Автор временных атомов: модель пишет run(inputs) по контракту и чинит его по ошибке.

Автор сам гоняет самопроверку в комнате станции до `ATTEMPTS` раз — на реальных входах из `Case`,
если они есть, иначе на примере автора. Атомам-загрузкам автор не нужен.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path


import finengine.contracts as contracts
from finengine.config import settings
from finengine.llm.budget import BudgetExceeded
from finengine.skills.spec.atom_intake import check_code
from finengine.skills.spec.atom_inputs import without_markdown_fence
from finengine.skills.spec.ephemeral import AtomRequest, EphemeralAtom
from finengine.skills.spec.room_files import FileSpec

_SKILL = Path(__file__).with_name("atom_author.md")
ATTEMPTS = 3
_REAL_INPUT_PREVIEW = 6000
_PREVIEW_ITEMS = 2
_SCHEMA_PREVIEW = 3000
FINDING = "VerificationFinding"
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
        lines.append(f'- "{FINDING}": находки проверок; приходят вместе с показателями, '
                     "если они тебе не нужны — игнорируй.")
    return "\n".join(lines)


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
