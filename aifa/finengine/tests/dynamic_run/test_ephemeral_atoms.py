"""Временные атомы с кодом (гибридный планировщик, часть 2): контракт, версия, AtomDef, обёртка.

Код атомов — простые функции `run(inputs)`, написанные руками; исполняет фикстура
`local_room` (та же точка входа, что в комнате станции). Модели в этой части нет.
"""
from __future__ import annotations

import pytest

from dynamic_support import MARGIN_CODE, MARGIN_SCHEMA
from dynamic_support import margin_request as _request
from finengine.contracts import (
    CalculatedMetric,
    DerivedFact,
    DynamicArtifact,
    ExtractedMetric,
    NodeKey,
    ScalarFact,
)
from finengine.policy.sandbox_profiles import (
    DYNAMIC_SANDBOX_PROFILES,
    SANDBOX_PROFILES,
    NetworkAccess,
)
from finengine.skills.atoms.graph_atoms import build_registry
from finengine.skills.spec.atom_registry import NodeArgs
from finengine.skills.spec.ephemeral import (
    DOWNLOAD_SCHEMA,
    AtomRequest,
    EphemeralAtom,
    check_request,
    group_inputs,
    to_atom_def,
)
from finengine.skills.spec.graph_executor import (
    ExecutionContext,
    RunInputs,
    validate_sandbox_dispatch,
)
from finengine.skills.spec.target import InputSpec, OutputSpec

ECHO_SCHEMA = {"type": "object", "required": ["seen"], "properties": {"seen": {"type": "string"}}}
ECHO_CODE = """
import json

def run(inputs):
    return {"seen": json.dumps(inputs, ensure_ascii=False)}
"""


@pytest.fixture(scope="module")
def registry():
    return build_registry(profile_id="ifrs_nf_ru")


def _download_request(**changes) -> AtomRequest:
    fields = dict(
        name="eph.brent_series", purpose="ряд Brent", output_type="dyn:brent_series",
        output_schema=DOWNLOAD_SCHEMA, url="https://example.com/brent.csv",
    )
    fields.update(changes)
    return AtomRequest(**fields)


def _metric(concept: str, value: float) -> ExtractedMetric:
    fact = ScalarFact(
        produced_by_atom="read.value", produced_by_atom_version="1.0.0", source_ref="doc",
        concept=concept, period_ref="2024", value=value, unit="RUB_MLN",
    )
    return ExtractedMetric(scalar_fact=fact, concept=concept, period_ref="2024")


def _calculated(concept: str, value: float) -> CalculatedMetric:
    fact = DerivedFact(
        produced_by_atom="calc.formula_engine", produced_by_atom_version="1.0.0",
        source_ref="formula", concept=concept, period_ref="2024", formula_id="f", formula_version="1",
        expression="x", inputs=["a"], value=value, unit="RUB_MLN",
    )
    return CalculatedMetric(derived_fact=fact, concept=concept, period_ref="2024")


def _context(evidence: tuple, *, inputs: RunInputs | None = None) -> ExecutionContext:
    key = NodeKey(atom_name="verify.bounds", atom_version="1", args_fingerprint="x")
    return ExecutionContext(
        run_id="run-1", run_passport=None, inputs=inputs or RunInputs(),
        evidence_by_key={key: evidence}, requires=(key,),
    )


# ---------------------------------------------------------------------------
# check_request: каждое правило — отдельный отрицательный тест
def test_valid_code_request_has_no_problems(registry) -> None:
    assert check_request(_request(), registry) == []


def test_valid_download_request_has_no_problems(registry) -> None:
    assert check_request(_download_request(), registry) == []


@pytest.mark.parametrize("name", ["ebitda_margin", "eph.ab", "eph.Margin", "eph.a-b-c", "eph." + "a" * 41])
def test_name_must_be_eph_snake_case(registry, name) -> None:
    problems = check_request(_request(name=name), registry)

    assert any("не вида eph." in problem for problem in problems)


def test_output_type_outside_dyn_is_forbidden(registry) -> None:
    problems = check_request(_request(output_type="ExtractedMetric"), registry)

    assert any("только dyn:*" in problem for problem in problems)


def test_download_with_requires_is_forbidden(registry) -> None:
    request = _download_request(requires=(InputSpec("ExtractedMetric", "EBITDA"),))

    assert any("не получает входов" in problem for problem in check_request(request, registry))


@pytest.mark.parametrize("url", ["ftp://example.com/a.csv", "file:///etc/passwd", "http://", "example.com/a.csv"])
def test_download_url_must_be_http_with_host(registry, url) -> None:
    problems = check_request(_download_request(url=url), registry)

    assert any("только http(s)" in problem for problem in problems)


def test_code_atom_without_requires_is_forbidden(registry) -> None:
    problems = check_request(_request(requires=()), registry)

    assert any("не из чего считать" in problem for problem in problems)


def test_input_nobody_produces_is_forbidden(registry) -> None:
    request = _request(requires=(InputSpec("NoSuchArtifact", "EBITDA"),))

    assert any("никто не производит" in problem for problem in check_request(request, registry))


def test_dyn_input_needs_no_registered_producer(registry) -> None:
    """dyn:* производит временный атом соседнего хода — его нет в реестре, и это нормально."""
    request = _request(requires=(InputSpec("dyn:brent_series"), InputSpec("ExtractedMetric", "EBITDA")))

    assert check_request(request, registry) == []


@pytest.mark.parametrize("schema, path", [
    ({"type": "object", "properties": {"x": {"type": "number"}}}, "$.x"),
    ({"type": "object", "properties": {"row": {"type": "object", "properties": {"y": {"type": "integer"}}}}},
     "$.row.y"),
    ({"type": "object", "properties": {"xs": {"type": "array", "items": {"type": "number"}}}}, "$.xs[]"),
])
def test_number_without_unit_is_forbidden(registry, schema, path) -> None:
    problems = check_request(_request(output_schema=schema), registry)

    assert any(path in problem and "x-unit" in problem for problem in problems)


def test_non_numeric_fields_need_no_unit(registry) -> None:
    schema = {"type": "object", "properties": {"label": {"type": "string"}, "ok": {"type": "boolean"}}}

    assert check_request(_request(output_schema=schema), registry) == []


# ---------------------------------------------------------------------------
# версия
def test_version_is_stable_for_equal_atoms() -> None:
    assert EphemeralAtom(_request(), MARGIN_CODE).version == EphemeralAtom(_request(), MARGIN_CODE).version


@pytest.mark.parametrize("changed", [
    EphemeralAtom(_request(), MARGIN_CODE + "\n# другой код"),
    EphemeralAtom(_request(purpose="другая цель"), MARGIN_CODE),
    EphemeralAtom(_request(output_type="dyn:other"), MARGIN_CODE),
    EphemeralAtom(_request(requires=(InputSpec("ExtractedMetric", "EBITDA"),)), MARGIN_CODE),
])
def test_version_changes_with_code_or_contract(changed) -> None:
    assert changed.version != EphemeralAtom(_request(), MARGIN_CODE).version


def test_producer_is_name_at_version() -> None:
    atom = EphemeralAtom(_request(), MARGIN_CODE)

    assert atom.producer == f"eph.ebitda_margin@{atom.version}"
    assert len(atom.version) == 12


# ---------------------------------------------------------------------------
# to_atom_def
def test_code_atom_becomes_a_generated_atom_def_under_the_room_profile() -> None:
    atom = EphemeralAtom(_request(), MARGIN_CODE)

    definition = to_atom_def(atom)

    assert definition.name == "eph.ebitda_margin" and definition.version == atom.version
    assert definition.requires == _request().requires
    assert definition.produces == (OutputSpec(output_type="dyn:ebitda_margin"),)
    assert definition.execution_kind == "generated"
    assert (definition.cache_policy, definition.cost_class) == ("run_scoped", "cheap")
    assert definition.sandbox_profile == "sealed_room"


def test_room_profile_has_code_and_no_network_and_is_not_a_fourth_fixed_profile() -> None:
    profile = DYNAMIC_SANDBOX_PROFILES["sealed_room"]

    assert profile.code_execution and profile.network_access is NetworkAccess.NONE
    assert "sealed_room" not in SANDBOX_PROFILES, "фиксированная тройка SANDBOX_PROFILES не меняется"


def test_atom_def_passes_sandbox_dispatch() -> None:
    validate_sandbox_dispatch(to_atom_def(EphemeralAtom(_request(), MARGIN_CODE)))


def test_download_atom_def_is_an_io_atom_without_inputs_and_without_the_room() -> None:
    definition = to_atom_def(EphemeralAtom(_download_request()))

    assert definition.execution_kind == "io" and definition.requires == ()
    assert definition.sandbox_profile != "sealed_room"
    assert definition.produces == (OutputSpec(output_type="dyn:brent_series"),)
    validate_sandbox_dispatch(definition)


# ---------------------------------------------------------------------------
# group_inputs
def test_group_inputs_groups_by_type_name_and_by_dyn_type() -> None:
    download = DynamicArtifact(
        output_type="dyn:brent_series", payload={"text": "1,2"}, origin=["external"],
        producer="eph.brent_series@abc",
    )
    grouped = group_inputs((_metric("EBITDA", 500.0), _metric("REVENUE", 2000.0), download))

    assert set(grouped) == {"ExtractedMetric", "dyn:brent_series"}
    assert [m["concept"] for m in grouped["ExtractedMetric"]] == ["EBITDA", "REVENUE"]
    assert grouped["ExtractedMetric"][0]["scalar_fact"]["value"] == 500.0
    assert grouped["dyn:brent_series"] == [download.model_dump(mode="json")]


# ---------------------------------------------------------------------------
# исполнение (_execute через AtomDef.executor)
def _run(atom: EphemeralAtom, evidence: tuple, *, inputs: RunInputs | None = None) -> list[DynamicArtifact]:
    return to_atom_def(atom).executor(NodeArgs(), _context(evidence, inputs=inputs))


def test_execute_runs_the_code_on_dependencies_and_wraps_the_result(local_room) -> None:
    atom = EphemeralAtom(_request(), MARGIN_CODE)
    ebitda, revenue = _metric("EBITDA", 500.0), _metric("REVENUE", 2000.0)

    (artifact,) = _run(atom, (ebitda, revenue))

    assert isinstance(artifact, DynamicArtifact)
    assert artifact.payload == {"margin_pct": 25.0}
    assert artifact.output_type == "dyn:ebitda_margin" and artifact.producer == atom.producer
    assert artifact.input_evidence_ids == [ebitda.scalar_fact.evidence_id, revenue.scalar_fact.evidence_id]


def test_execute_does_not_pass_document_text_or_pdf_bytes_to_the_code(local_room) -> None:
    secret = RunInputs(document_text="СЕКРЕТНЫЙ ТЕКСТ ОТЧЁТА", pdf_bytes=b"SECRET-PDF-BYTES", role="analyst")
    atom = EphemeralAtom(_request(output_schema=ECHO_SCHEMA), ECHO_CODE)

    (artifact,) = _run(atom, (_metric("EBITDA", 500.0),), inputs=secret)

    seen = artifact.payload["seen"]
    assert "ExtractedMetric" in seen and "EBITDA" in seen, "результаты зависимостей код видит"
    assert "СЕКРЕТНЫЙ" not in seen and "SECRET-PDF" not in seen and "analyst" not in seen


def test_schema_violation_is_a_node_error(local_room) -> None:
    code = 'def run(inputs):\n    return {"margin_pct": "много"}\n'

    with pytest.raises(ValueError, match="output_schema"):
        _run(EphemeralAtom(_request(), code), (_metric("EBITDA", 1.0),))


def test_missing_required_field_is_a_node_error(local_room) -> None:
    with pytest.raises(ValueError, match="margin_pct"):
        _run(EphemeralAtom(_request(), "def run(inputs):\n    return {}\n"), (_metric("EBITDA", 1.0),))


def test_code_error_is_a_node_error_with_the_traceback_tail(local_room) -> None:
    code = "def run(inputs):\n    return {'margin_pct': 1 / 0}\n"

    with pytest.raises(RuntimeError, match="ZeroDivisionError"):
        _run(EphemeralAtom(_request(), code), (_metric("EBITDA", 1.0),))


@pytest.mark.parametrize("evidence, expected", [
    ((), ["generated"]),
    ((_metric("EBITDA", 1.0),), ["generated", "reported"]),
    ((_calculated("MARGIN", 1.0),), ["calculated", "generated"]),
    ((_metric("EBITDA", 1.0), _calculated("MARGIN", 1.0)), ["calculated", "generated", "reported"]),
])
def test_origin_is_computed_by_the_wrapper_from_inputs(local_room, evidence, expected) -> None:
    (artifact,) = _run(EphemeralAtom(_request(), 'def run(inputs):\n    return {"margin_pct": 1.0}\n'), evidence)

    assert artifact.origin == expected


@pytest.mark.parametrize("model", ["ExtractedStructure", "QuoteEvidence", "ConditionEvidence"])
def test_numbers_and_quotes_read_from_the_report_keep_the_reported_origin(model) -> None:
    from finengine import contracts
    from finengine.skills.spec.ephemeral import _origin

    assert _origin((getattr(contracts, model).model_construct(),)) == ["generated", "reported"]


def test_origin_of_a_dynamic_input_is_inherited(local_room) -> None:
    external = DynamicArtifact(
        output_type="dyn:brent_series", payload={"text": "1"}, origin=["external"], producer="eph.b@1",
    )

    (artifact,) = _run(EphemeralAtom(_request(), 'def run(inputs):\n    return {"margin_pct": 1.0}\n'), (external,))

    assert artifact.origin == ["external", "generated"]


def test_code_cannot_claim_a_reported_origin_for_itself(local_room) -> None:
    """«reported» — это «число взято из отчёта», его даёт только вход; свои слова кода ничего не значат."""
    code = 'def run(inputs):\n    return {"margin_pct": 1.0, "origin": ["reported"]}\n'
    schema = {**MARGIN_SCHEMA, "properties": {**MARGIN_SCHEMA["properties"], "origin": {"type": "array"}}}

    (artifact,) = _run(EphemeralAtom(_request(output_schema=schema), code), ())

    assert artifact.origin == ["generated"]
