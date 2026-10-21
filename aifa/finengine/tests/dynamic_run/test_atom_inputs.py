"""Входы атома из `Case`: то же, что код получает в графе (`atom_inputs.case_inputs`, шаг 17).

Реальные входы кормят приёмку кода (`atom_intake`, `LoopSession.register_atom`), поэтому их состав и порядок должны
совпадать с тем, что `GraphExecutor` отдаёт атому. Принятый ранее живой ответ автора (`live_author_replies.json`)
проверяется без сети тем же `check_code`, что у подзадачи.
"""
from __future__ import annotations

import json
from functools import partial
from pathlib import Path

import httpx
import pytest

from dynamic_support import margin_request
from finengine.skills.document_acquisition.downloader import download_public_file
from finengine.skills.spec import ephemeral
from finengine.skills.spec.atom_inputs import case_inputs
from finengine.skills.spec.atom_intake import check_code
from finengine.skills.spec.ephemeral import DOWNLOAD_SCHEMA, AtomRequest, EphemeralAtom, group_inputs, to_atom_def
from finengine.skills.spec.target import InputSpec
from test_ephemeral_atoms import _metric
from test_ephemeral_in_graph import CASE, OWNER, _atom, _execute, _plan, _stored

REPLIES_FIXTURE = Path(__file__).with_name("fixtures") / "live_author_replies.json"

def _download_atom(output_type: str = "dyn:brent_csv") -> EphemeralAtom:
    return EphemeralAtom(AtomRequest(
        name="eph.brent_csv", purpose="ряд", output_type=output_type, output_schema=DOWNLOAD_SCHEMA,
        url="https://data.example.org/brent.csv"))




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


def _real_metrics() -> dict:
    return group_inputs((_metric("EBITDA", 500.0), _metric("REVENUE", 2000.0)))


@pytest.mark.skipif(not REPLIES_FIXTURE.exists(), reason="фикстура ответов модели ещё не записана живым прогоном")
def test_recorded_live_reply_of_the_author_is_accepted_by_the_intake_without_the_network(local_room) -> None:
    """Записанный ответ автора (GLM, живой прогон) проходит ту же проверку, что код подзадачи: `check_code` на входах."""
    pairs = json.loads(REPLIES_FIXTURE.read_text(encoding="utf-8"))
    code = json.loads(pairs[0]["reply"])["code"]

    verdict = check_code(margin_request(), code, _real_metrics())

    assert verdict.error is None and verdict.output == {"margin_pct": 25.0}
