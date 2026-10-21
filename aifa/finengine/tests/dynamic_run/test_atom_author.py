"""Автор временных атомов (гибридный планировщик, часть 3).

Модель подменена `complete_fn` с заранее записанными ответами: он же сохраняет полученные
промпты, чтобы тесты проверяли, ЧТО увидел автор. Код исполняет `local_room`. Тесты на форму
входов из `Case` идут через настоящий граф (подставные `locate`/`read.value`, как в
`test_ephemeral_in_graph`), чтобы сравнивать с тем, что код получает в графе на самом деле.
"""
from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path

import httpx
import pytest

from dynamic_support import MARGIN_CODE, margin_request
from finengine.skills.document_acquisition.downloader import download_public_file
from finengine.llm.budget import BudgetExceeded
from finengine.skills.spec import atom_author, ephemeral, sealed_room
from finengine.skills.spec.atom_author import (
    _REAL_INPUT_PREVIEW,
    AtomAuthor,
    StationFault,
    describe_inputs,
    render_author_prompt,
)
from finengine.skills.spec.atom_inputs import case_inputs
from finengine.skills.spec.ephemeral import DOWNLOAD_SCHEMA, AtomRequest, EphemeralAtom, group_inputs, to_atom_def
from finengine.skills.spec.target import InputSpec
from test_ephemeral_atoms import _metric
from test_ephemeral_in_graph import CASE, OWNER, _atom, _execute, _plan, _stored
from tests.dynamic_run.conftest import local_run_code

EXAMPLE = {"ExtractedMetric": [
    {"concept": "EBITDA", "scalar_fact": {"value": 500.0}},
    {"concept": "REVENUE", "scalar_fact": {"value": 2000.0}},
]}


def _reply(code: str = MARGIN_CODE, example: dict | None = None) -> str:
    return json.dumps({"code": code, "example_inputs": EXAMPLE if example is None else example})


@dataclass
class Script:
    """`complete_fn` с записанными ответами; исключение в списке — сбой шлюза на этом вызове."""
    replies: list
    prompts: list[str] = field(default_factory=list)
    systems: list[str] = field(default_factory=list)

    async def __call__(self, system: str, prompt: str) -> str:
        self.systems.append(system)
        self.prompts.append(prompt)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


def _write(script: Script, *, real_inputs: dict | None = None, request: AtomRequest | None = None,
           attempts: int = 3, **kwargs):
    request = request or margin_request()
    return AtomAuthor(script, attempts=attempts).write(
        request, inputs_doc=describe_inputs(request, {}), real_inputs=real_inputs, **kwargs)


BAD_CODE = "def run(inputs):\n    return {'margin_pct': 1 / 0}\n"
BAD_SCHEMA_CODE = "def run(inputs):\n    return {'margin_pct': 'много'}\n"


# ---------------------------------------------------------------------------
# цикл write()
def test_success_on_the_first_attempt(local_room) -> None:
    script = Script([_reply()])

    atom, reason = _write(script)

    assert reason is None and isinstance(atom, EphemeralAtom)
    assert atom.code == MARGIN_CODE and atom.request == margin_request()
    assert len(script.prompts) == 1
    assert script.systems[0] == atom_author._SKILL.read_text(encoding="utf-8")
    assert "## Ошибка" not in script.prompts[0] and "## Твой прошлый код" not in script.prompts[0]


def test_a_failed_self_check_sends_the_error_and_the_previous_code_and_the_second_attempt_wins(local_room) -> None:
    script = Script([_reply(BAD_CODE), _reply()])

    atom, reason = _write(script)

    assert reason is None and atom.code == MARGIN_CODE
    first, second = script.prompts
    assert "## Ошибка" not in first
    assert "## Ошибка" in second and "ZeroDivisionError" in second
    assert "## Твой прошлый код" in second and "1 / 0" in second


FOREIGN_FILE_CODE = (
    "def run(inputs):\n    import os\n    os.makedirs('out', exist_ok=True)\n"
    "    open('out/итоги.xlsx', 'w').write('это не книга')\n    return {'margin_pct': 1.0}\n"
)
GOOD_FILE_CODE = (
    "def run(inputs):\n    import os\n    from openpyxl import Workbook\n    os.makedirs('out', exist_ok=True)\n"
    "    wb = Workbook()\n    wb.active['A1'] = '=1+1'\n    wb.save('out/итоги.xlsx')\n    return {'margin_pct': 1.0}\n"
)


def test_a_file_that_is_not_what_its_extension_says_is_sent_back_to_the_author(local_room) -> None:
    """Часть 16: автор узнаёт о повреждённом файле на самопроверке, а не после выкладки узла."""
    script = Script([_reply(FOREIGN_FILE_CODE), _reply(GOOD_FILE_CODE)])

    atom, reason = _write(script)

    assert reason is None and atom.code == GOOD_FILE_CODE
    assert "итоги.xlsx" in script.prompts[1] and "не похоже на xlsx" in script.prompts[1]


def test_a_reply_that_is_not_json_counts_as_an_attempt(local_room) -> None:
    script = Script(["Вот ваш код: def run...", _reply()])

    atom, reason = _write(script)

    assert reason is None and atom.code == MARGIN_CODE
    assert "ответ не в формате" in script.prompts[1]


@pytest.mark.parametrize("reply", ['{"example_inputs": {}}', "[1, 2]", "null", '{"code": "x", "example_inputs": [1]}'])
def test_a_reply_of_the_wrong_shape_counts_as_an_attempt(local_room, reply: str) -> None:
    script = Script([reply, _reply()])

    atom, _ = _write(script)

    assert atom is not None and len(script.prompts) == 2


def test_when_every_attempt_fails_the_reason_is_the_last_error_and_there_is_no_atom(local_room) -> None:
    script = Script([_reply(BAD_CODE), _reply(BAD_CODE), _reply(BAD_CODE), _reply()])

    atom, reason = _write(script)

    assert atom is None and "ZeroDivisionError" in reason
    assert not isinstance(reason, StationFault), "отказ автора — обычная строка"
    assert len(script.prompts) == 3 and len(script.replies) == 1, "ровно три попытки, четвёртого вызова нет"


def test_the_number_of_attempts_is_a_parameter(local_room) -> None:
    script = Script([_reply(BAD_CODE)] * 5)

    atom, _ = _write(script, attempts=2)

    assert atom is None and len(script.prompts) == 2


def test_a_schema_violation_is_sent_to_the_author_as_an_error(local_room) -> None:
    script = Script([_reply(BAD_SCHEMA_CODE), _reply()])

    atom, _ = _write(script)

    assert atom is not None
    assert "output_schema" in script.prompts[1] and "margin_pct" in script.prompts[1]


def test_a_fix_request_starts_from_the_given_code_and_error(local_room) -> None:
    script = Script([_reply()])

    atom, _ = _write(script, previous_code=BAD_CODE, error="ValueError: не могу разобрать период '6M 2026'")

    assert atom is not None
    assert "не могу разобрать период" in script.prompts[0] and "1 / 0" in script.prompts[0]


# ---------------------------------------------------------------------------
# на каких входах идёт самопроверка
def test_the_self_check_runs_on_the_real_inputs_not_on_the_example(local_room) -> None:
    real = {"ExtractedMetric": [{"concept": "EBITDA", "scalar_fact": {"value": 5.0}},
                                {"concept": "REVENUE", "scalar_fact": {"value": 0.0}}]}
    script = Script([_reply(), _reply()])      # пример автора годен, а реальный вход делит на ноль

    atom, reason = _write(script, real_inputs=real, attempts=2)

    assert atom is None and "ZeroDivisionError" in reason, "пример не должен подменять реальные данные"


def test_without_real_inputs_the_self_check_runs_on_the_authors_example(local_room) -> None:
    atom, reason = _write(Script([_reply()]), real_inputs=None)

    assert reason is None and atom is not None


def test_a_big_real_input_does_not_bloat_the_prompt_but_reaches_the_room_whole(monkeypatch) -> None:
    seen: list[dict] = []

    def spy(code: str, inputs: dict, **kwargs):
        seen.append(inputs)
        return local_run_code(code, inputs, **kwargs)

    monkeypatch.setattr(sealed_room, "run_code", spy)
    text = "DATE,VALUE\n" + "2026-01-02,78.1\n" * 70_000                   # ~1 МБ
    real = {"dyn:brent_csv": [{"payload": {"url": "u", "content_type": "text/csv", "text": text}}]}
    request = AtomRequest(
        name="eph.rows", purpose="число строк", output_type="dyn:rows_count",
        output_schema={"type": "object", "required": ["rows"],
                       "properties": {"rows": {"type": "integer", "x-unit": "count"}}},
        requires=(InputSpec("dyn:brent_csv"),))
    code = "def run(inputs):\n    return {'rows': inputs['dyn:brent_csv'][0]['payload']['text'].count('\\n')}\n"
    script = Script([_reply(code)])

    atom, reason = _write(script, real_inputs=real, request=request)

    assert reason is None and atom is not None
    assert len(script.prompts[0]) < _REAL_INPUT_PREVIEW + 6000, "в промпте — только сокращённый вход"
    assert text not in script.prompts[0]
    assert seen[0]["dyn:brent_csv"][0]["payload"]["text"] == text, "в комнату — полный вход"


def test_a_huge_wrong_result_does_not_bloat_the_next_prompt(local_room) -> None:
    """Сообщение jsonschema содержит само значение: большой результат не по схеме раздул бы промпт починки."""
    huge_wrong = "def run(inputs):\n    return {'margin_pct': 'x' * 900_000}\n"
    script = Script([_reply(huge_wrong), _reply()])

    atom, _ = _write(script)

    assert atom is not None
    assert "## Ошибка" in script.prompts[1] and "output_schema" in script.prompts[1]
    assert len(script.prompts[1]) < _REAL_INPUT_PREVIEW + 6000, "в промпте починки — сокращённая ошибка"


def test_render_author_prompt_has_the_sections_of_the_plan() -> None:
    request = margin_request()

    plain = render_author_prompt(request, "ВХОДЫ", None, None, None)
    fixing = render_author_prompt(request, "ВХОДЫ", {"ExtractedMetric": []}, "КОД", "ОШИБКА")

    for section in ("## Атом", "name: eph.ebitda_margin", "purpose: маржа EBITDA", "## Входы — inputs",
                    "ВХОДЫ", "## Результат — output_schema", '"margin_pct"'):
        assert section in plain
    assert "## Реальный вход" not in plain and "## Ошибка" not in plain
    for section in ("## Реальный вход (сокращён)", "## Твой прошлый код", "КОД", "## Ошибка", "ОШИБКА",
                    "Исправь причину и пришли код целиком."):
        assert section in fixing


# ---------------------------------------------------------------------------
# сбой станции и сбой шлюза модели
def test_a_station_fault_stops_the_loop_and_the_model_never_sees_it(monkeypatch) -> None:
    fault = sealed_room.CodeRun(None, "комната станции не настроена: FINENGINE_WORKSTATION_URL пуст",
                                station_fault=True)
    monkeypatch.setattr(sealed_room, "run_code", lambda code, inputs, **kwargs: fault)
    script = Script([_reply(), _reply()])

    atom, reason = _write(script)

    assert atom is None and isinstance(reason, StationFault)
    assert str(reason) == fault.error, "причина станции — как есть"
    assert len(script.prompts) == 1, "автору чинить нечего: попытки не тратятся"


def test_a_station_fault_after_a_code_error_does_not_send_it_either(monkeypatch) -> None:
    runs = iter([sealed_room.CodeRun(None, "ZeroDivisionError: division by zero"),
                 sealed_room.CodeRun(None, "станция не исполнила код: timeout", station_fault=True)])
    monkeypatch.setattr(sealed_room, "run_code", lambda code, inputs, **kwargs: next(runs))
    script = Script([_reply(BAD_CODE), _reply(), _reply()])

    atom, reason = _write(script)

    assert atom is None and isinstance(reason, StationFault) and "timeout" in reason
    assert len(script.prompts) == 2
    assert "timeout" not in script.prompts[1] and "станци" not in script.prompts[1].split("## Ошибка")[-1]


def test_a_gateway_exception_is_an_attempt_not_a_crash(local_room) -> None:
    script = Script([RuntimeError("503 Service Unavailable"), _reply()])

    atom, reason = _write(script)

    assert reason is None and atom is not None
    assert "503" not in script.prompts[1] and "## Ошибка" not in script.prompts[1], (
        "сбой шлюза — не ошибка кода: модели его не показываем"
    )


def test_when_the_gateway_keeps_failing_the_reason_names_it(local_room) -> None:
    script = Script([TimeoutError("лимит запросов"), httpx.ConnectError("нет сети"), RuntimeError("503")])

    atom, reason = _write(script)

    assert atom is None and not isinstance(reason, StationFault)
    assert "503" in reason and "RuntimeError" in reason
    assert len(script.prompts) == 3


def test_budget_exceeded_from_the_gateway_is_not_swallowed(local_room) -> None:
    script = Script([BudgetExceeded("runaway")])

    with pytest.raises(BudgetExceeded, match="runaway"):
        _write(script)


@pytest.mark.parametrize("failure", [ValueError("bad gateway payload"), TypeError("bad client state")])
def test_gateway_shape_exceptions_are_not_reported_as_the_models_json_error(local_room, failure) -> None:
    script = Script([failure, _reply()])

    atom, reason = _write(script)

    assert atom is not None and reason is None
    assert "ответ не в формате" not in script.prompts[1]


def test_a_gateway_failure_after_a_code_error_keeps_the_previous_error_for_the_next_prompt(local_room) -> None:
    script = Script([_reply(BAD_CODE), ConnectionError("reset"), _reply()])

    atom, _ = _write(script)

    assert atom is not None
    assert "ZeroDivisionError" in script.prompts[2] and "1 / 0" in script.prompts[2]


def test_default_author_model_call_uses_the_bounded_router_profile(monkeypatch) -> None:
    seen = {}

    async def _complete(prompt, **kwargs):
        seen.update(kwargs)
        return "{}"

    from finengine.llm import openrouter

    monkeypatch.setattr(openrouter, "complete", _complete)

    asyncio.run(atom_author._default_complete("system", "prompt"))

    assert seen["retries"] == 1
    assert seen["max_output"] == 6000
    assert seen["reasoning_effort"] == "low"


def test_the_author_waits_by_its_own_limit_not_by_the_one_of_intent(monkeypatch) -> None:
    """Автор пишет код целиком и на dev не укладывался в общие 120 с (`TimeoutError`, 08.10.2026).
    Предел автора — своё поле: у разбора запроса он прежний."""
    async def _complete(prompt, **kwargs):
        await asyncio.sleep(0.2)
        return "{}"

    from finengine.config import Settings
    from finengine.llm import openrouter

    monkeypatch.setattr(openrouter, "complete", _complete)
    monkeypatch.setattr(atom_author.settings, "routing_model_timeout_seconds", 0.01)
    monkeypatch.setattr(atom_author.settings, "atom_author_model_timeout_seconds", 5.0)

    assert asyncio.run(atom_author._default_complete("system", "prompt")) == "{}"
    fields = Settings.model_fields
    assert fields["atom_author_model_timeout_seconds"].default == 300.0
    assert fields["routing_model_timeout_seconds"].default == 120.0


def test_the_author_model_call_is_cut_by_the_author_limit(monkeypatch) -> None:
    async def _complete(prompt, **kwargs):
        await asyncio.sleep(1)
        return "{}"

    from finengine.llm import openrouter

    monkeypatch.setattr(openrouter, "complete", _complete)
    monkeypatch.setattr(atom_author.settings, "atom_author_model_timeout_seconds", 0.01)

    with pytest.raises(TimeoutError):
        asyncio.run(atom_author._default_complete("system", "prompt"))


# ---------------------------------------------------------------------------
# describe_inputs
def _download_atom(output_type: str = "dyn:brent_csv") -> EphemeralAtom:
    return EphemeralAtom(AtomRequest(
        name="eph.brent_csv", purpose="ряд", output_type=output_type, output_schema=DOWNLOAD_SCHEMA,
        url="https://data.example.org/brent.csv"))


def test_describe_inputs_for_a_registered_type_shows_the_schema_and_the_gates_and_the_findings() -> None:
    doc = describe_inputs(margin_request(), {})

    assert '"ExtractedMetric"' in doc and "scalar_fact" in doc, "схема класса контракта"
    assert "norm" in doc and "verify.bounds" in doc, "числа приходят за гейтами — автор должен это знать"
    assert '"VerificationFinding"' in doc, "та же форма, что в графе: рядом с показателями приходят находки"
    assert doc.count('- "ExtractedMetric"') == 1, "два входа одного типа (EBITDA, REVENUE) — одна запись"


def test_describe_inputs_names_registered_concepts_but_not_unscoped_dynamic_inputs() -> None:
    registered = describe_inputs(margin_request(), {})
    dynamic = describe_inputs(
        AtomRequest(name="eph.calc", purpose="расчёт", output_type="dyn:calc",
                    output_schema={"type": "object"}, requires=(InputSpec("dyn:brent_csv"),)),
        {"eph.brent_csv": _download_atom()},
    )

    assert '(concept: "EBITDA", "REVENUE")' in registered
    assert "concept:" not in dynamic


@pytest.mark.parametrize("input_type", ["QuoteEvidence", "ExtractedStructure"])
def test_describe_inputs_does_not_promise_checked_numbers_for_types_without_the_metric_gates(input_type) -> None:
    request = AtomRequest(name="eph.quotes", purpose="цитаты", output_type="dyn:quotes",
                          output_schema={"type": "object"}, requires=(InputSpec(input_type, "TOPIC"),))
    line = _entry(describe_inputs(request, {}), input_type)

    assert "не применяются" in line and "не считай" in line
    assert "проверенн" not in line.replace("не считай данные проверенными", "")


def _entry(doc: str, input_type: str) -> str:
    """Запись одного входа: от строки `- "тип"` до следующей записи."""
    block = doc.split(f'- "{input_type}"', 1)[1]
    return block.split("\n- ", 1)[0]


def test_describe_inputs_for_a_dyn_type_uses_the_producers_schema_including_a_download() -> None:
    request = AtomRequest(name="eph.calc", purpose="расчёт", output_type="dyn:calc",
                          output_schema={"type": "object"}, requires=(InputSpec("dyn:brent_csv"),))

    doc = describe_inputs(request, {"eph.brent_csv": _download_atom()})

    assert '"dyn:brent_csv"' in doc and "DynamicArtifact" in doc and "payload" in doc
    assert '"text"' in doc and '"content_type"' in doc, "форма payload загрузки: url, content_type, text"
    assert "VerificationFinding" not in doc, "у dyn-входа находок проверок нет"


def test_describe_inputs_for_a_dyn_type_with_an_unknown_producer_says_so() -> None:
    request = AtomRequest(name="eph.calc", purpose="расчёт", output_type="dyn:calc",
                          output_schema={"type": "object"}, requires=(InputSpec("dyn:ghost"),))

    assert "не описана" in describe_inputs(request, {})


# ---------------------------------------------------------------------------
# case_inputs: реальные входы из Case
def _run_echo(base, severstal_text):
    """Граф с атомом, который возвращает всё, что получил в `inputs`, — как JSON-строку."""
    schema = {"type": "object", "required": ["seen"], "properties": {"seen": {"type": "string"}}}
    echo = 'import json\n\ndef run(inputs):\n    return {"seen": json.dumps(inputs, ensure_ascii=False)}\n'
    runtime = base.with_atoms([to_atom_def(_atom(echo, output_schema=schema))])
    planned = _plan(runtime)
    assert _execute(runtime, planned, severstal_text).succeeded
    return runtime, planned


def test_case_inputs_is_none_while_the_case_has_nothing(base) -> None:
    assert case_inputs(base.store, OWNER, CASE, margin_request()) is None


def test_case_inputs_in_the_case_equals_what_the_code_gets_in_the_graph_after_the_gates(
    base, severstal_text, local_room,
) -> None:
    runtime, _ = _run_echo(base, severstal_text)
    (echo_artifact,) = _stored(runtime)
    in_graph = json.loads(echo_artifact.payload["seen"])

    from_case = case_inputs(runtime.store, OWNER, CASE, margin_request())

    assert set(in_graph) == {"ExtractedMetric", "VerificationFinding"}, "форма в графе: показатели и находки"
    assert set(from_case) == set(in_graph)
    for group, items in in_graph.items():
        assert _canonical(from_case[group]) == _canonical(items), f"группа {group}: тот же состав и те же объекты"
        assert len(from_case[group]) == len(items) == 2, "по одному объекту на показатель, без дублей"


def test_case_inputs_keeps_the_order_of_requires_like_the_graph(base, severstal_text, local_room) -> None:
    """Код вправе брать `inputs["ExtractedMetric"][0]`: порядок в самопроверке обязан быть тем же, что в графе.
    Case заполнен планом с одним порядком входов, а новый атом просит их в обратном."""
    runtime, _ = _run_echo(base, severstal_text)
    reversed_requires = tuple(reversed(margin_request().requires))
    schema = {"type": "object", "required": ["seen"], "properties": {"seen": {"type": "string"}}}
    echo = 'import json\n\ndef run(inputs):\n    return {"seen": json.dumps(inputs, ensure_ascii=False)}\n'
    atom = _atom(echo, name="eph.margin_reversed", output_type="dyn:margin_reversed", output_schema=schema,
                 requires=reversed_requires)
    runtime = runtime.with_atoms([to_atom_def(atom)])
    assert _execute(runtime, _plan(runtime, atom.request.name, atom.request.output_type), severstal_text).succeeded
    (in_graph,) = [json.loads(a.payload["seen"]) for a in _stored(runtime, "dyn:margin_reversed")]

    from_case = case_inputs(runtime.store, OWNER, CASE, margin_request(requires=reversed_requires))

    assert [m["concept"] for m in in_graph["ExtractedMetric"]] == ["REVENUE", "EBITDA"]
    assert from_case == in_graph, "тот же состав и тот же порядок в каждой группе"


def test_case_inputs_has_no_duplicates_and_no_ungated_copies_of_a_metric(base, severstal_text, local_room) -> None:
    runtime, _ = _run_echo(base, severstal_text)
    rows = runtime.store.list_artifacts(OWNER, CASE)
    stages = {row["node_key"].atom_name for row in rows if row["output_type"] == "ExtractedMetric"}
    assert stages == {"read.value", "norm.unit", "norm.time", "verify.bounds"}, "в Case по копии на каждую стадию"

    from_case = case_inputs(runtime.store, OWNER, CASE, margin_request())

    ids = [item["scalar_fact"]["evidence_id"] for item in from_case["ExtractedMetric"]]
    assert len(ids) == len(set(ids)) == 2
    gated = {value.scalar_fact.evidence_id for row in rows
             if row["node_key"].atom_name == "verify.bounds" and row["output_type"] == "ExtractedMetric"
             for value in row["payloads"]}
    assert set(ids) == gated, "берутся показатели с конца цепочки гейтов, а не сырой read.value"


def test_case_inputs_is_none_when_one_of_the_required_concepts_is_missing(base, severstal_text, local_room) -> None:
    runtime, _ = _run_echo(base, severstal_text)
    needs_more = margin_request(requires=(InputSpec("ExtractedMetric", "EBITDA"),
                                          InputSpec("ExtractedMetric", "NET_DEBT")))

    assert case_inputs(runtime.store, OWNER, CASE, needs_more) is None
    only_ebitda = case_inputs(runtime.store, OWNER, CASE, margin_request(requires=(InputSpec("ExtractedMetric", "EBITDA"),)))
    assert [m["concept"] for m in only_ebitda["ExtractedMetric"]] == ["EBITDA"]
    assert len(only_ebitda["VerificationFinding"]) == 1, "находки только по нужному показателю"


def test_case_inputs_still_works_after_the_eph_result_lies_in_the_case(base, severstal_text, local_room) -> None:
    """Результат самого eph-атома ссылается на находки гейтов; это не должно прятать их от автора."""
    runtime, _ = _run_echo(base, severstal_text)

    from_case = case_inputs(runtime.store, OWNER, CASE, margin_request())

    assert len(from_case["VerificationFinding"]) == 2


def _canonical(items: list[dict]) -> list[str]:
    return sorted(json.dumps(item, sort_keys=True, ensure_ascii=False) for item in items)


def test_case_inputs_takes_dyn_results_of_the_current_producer_only(base, monkeypatch, severstal_text) -> None:
    def served(text: str):
        monkeypatch.setattr(ephemeral, "download_public_file", partial(
            download_public_file,
            transport=httpx.MockTransport(lambda request: httpx.Response(200, content=text.encode())),
            resolver=lambda host, port: ["93.184.216.34"]))

    old = _download_atom()
    new = EphemeralAtom(AtomRequest(
        name="eph.brent_csv", purpose="ряд", output_type="dyn:brent_csv", output_schema=DOWNLOAD_SCHEMA,
        url="https://data.example.org/brent_v2.csv"))
    for atom, text in ((old, "OLD"), (new, "NEW")):
        served(text)
        runtime = base.with_atoms([to_atom_def(atom)])
        assert _execute(runtime, _plan(runtime, "eph.brent_csv", "dyn:brent_csv"), severstal_text).succeeded
    request = AtomRequest(name="eph.calc", purpose="расчёт", output_type="dyn:calc",
                          output_schema={"type": "object"}, requires=(InputSpec("dyn:brent_csv"),))

    everything = case_inputs(base.store, OWNER, CASE, request)
    current = case_inputs(base.store, OWNER, CASE, request, atoms={"eph.brent_csv": new})

    assert {a["payload"]["text"] for a in everything["dyn:brent_csv"]} == {"OLD", "NEW"}
    assert [a["payload"]["text"] for a in current["dyn:brent_csv"]] == ["NEW"]
    assert group_inputs(()) == {}


# ---------------------------------------------------------------------------
# системный промпт
def test_the_system_prompt_is_the_one_from_the_plan() -> None:
    text = atom_author._SKILL.read_text(encoding="utf-8")

    for phrase in ("# Скилл: автор временных атомов AIFA-CLAW", "def run(inputs: dict) -> dict:",
                   "Не выдумывай числа.", "Объекты других типов, если встретятся (например, находки проверок), игнорируй.",
                   "## Формат ответа — только JSON", '"example_inputs"'):
        assert phrase in text


# ---------------------------------------------------------------------------
# живой вызов: реальная модель (и реальная комната, если задан адрес станции)
REPLIES_FIXTURE = Path(__file__).with_name("fixtures") / "live_author_replies.json"


def _real_metrics() -> dict:
    return group_inputs((_metric("EBITDA", 500.0), _metric("REVENUE", 2000.0)))


class Recorder:
    """Настоящий шлюз модели, который запоминает пары «промпт — ответ» для фикстуры."""

    def __init__(self) -> None:
        self.pairs: list[dict] = []

    async def __call__(self, system: str, prompt: str) -> str:
        reply = await atom_author._default_complete(system, prompt)
        self.pairs.append({"prompt": prompt, "reply": reply})
        return reply


@pytest.mark.skipif(os.environ.get("AIFA_LIVE_MODEL") != "1", reason="нужен AIFA_LIVE_MODEL=1 (и ключ модели)")
def test_live_model_writes_an_accepted_ratio_atom_and_the_replies_are_recorded(monkeypatch) -> None:
    """Простой контракт: маржа EBITDA = EBITDA / REVENUE * 100 на реальных входах (так автор
    работает в цикле: показатели уже в Case). Комната — станция, если задан FINENGINE_WORKSTATION_URL,
    иначе `local_room` (тогда проверена модель, а не станция).

    Без реальных входов модель шлёт пустой `example_inputs`, и проверка по правилу 4 промпта
    («нет данных — ValueError») не проходит: см. 34-status.md, «Часть 3»."""
    if not os.environ.get("FINENGINE_WORKSTATION_URL"):
        monkeypatch.setattr(sealed_room, "run_code", local_run_code)
    recorder = Recorder()

    atom, reason = AtomAuthor(recorder).write(
        margin_request(), inputs_doc=describe_inputs(margin_request(), {}), real_inputs=_real_metrics())
    REPLIES_FIXTURE.parent.mkdir(exist_ok=True)
    REPLIES_FIXTURE.write_text(json.dumps(recorder.pairs, ensure_ascii=False, indent=1), encoding="utf-8")

    assert reason is None and atom is not None, reason
    checked = local_run_code(atom.code, group_inputs((_metric("EBITDA", 500.0), _metric("REVENUE", 2000.0))))
    assert checked.output == {"margin_pct": 25.0}, "принятый код считает верно и на форме входов из графа"


@pytest.mark.skipif(not REPLIES_FIXTURE.exists(), reason="фикстура ответов модели ещё не записана живым прогоном")
def test_recorded_live_replies_are_accepted_without_the_network(local_room) -> None:
    pairs = json.loads(REPLIES_FIXTURE.read_text(encoding="utf-8"))
    script = Script([pair["reply"] for pair in pairs])

    atom, reason = _write(script, real_inputs=_real_metrics())

    assert reason is None and atom is not None


def test_the_author_calls_its_own_model_when_one_is_set(monkeypatch) -> None:
    """Модель автора атомов — своё поле; пустое — модель бэкенда по умолчанию, как было."""
    seen = []

    async def _complete(prompt, **kwargs):
        seen.append(kwargs.get("model"))
        return "{}"

    from finengine.llm import openrouter

    monkeypatch.setattr(openrouter, "complete", _complete)
    monkeypatch.setattr(atom_author.settings, "atom_author_model", "zai-org/GLM-5.1")
    asyncio.run(atom_author._default_complete("system", "prompt"))
    monkeypatch.setattr(atom_author.settings, "atom_author_model", "")
    asyncio.run(atom_author._default_complete("system", "prompt"))

    assert seen == ["zai-org/GLM-5.1", None]


def test_the_author_reply_in_a_markdown_fence_is_still_json(monkeypatch) -> None:
    """`zai-org/GLM-5.2` на Cloud.ru отдаёт верный JSON в ограде ```json — у автора атомов ограда
    снимается так же, как у планировщика, иначе каждая попытка читалась бы как «ответ не в формате»."""
    replies = iter(['```json\n{"code": "x = 1"}\n```', '{"code": "x = 1"}'])

    async def _complete(prompt, **kwargs):
        return next(replies)

    from finengine.llm import openrouter

    monkeypatch.setattr(openrouter, "complete", _complete)

    assert asyncio.run(atom_author._default_complete("system", "prompt")) == '{"code": "x = 1"}'
    assert asyncio.run(atom_author._default_complete("system", "prompt")) == '{"code": "x = 1"}'
