"""Детерминированная граница ответа модели цикла DynamicRun."""
from __future__ import annotations

import json

import pytest

from finengine.contracts import GoalIR
from finengine.skills.atoms.graph_atoms import build_registry
from finengine.skills.spec.dynamic_run import DynamicRun, InvalidSteps, parse_decision, parse_steps, render_state
from finengine.skills.spec.ephemeral import DOWNLOAD_SCHEMA
from finengine.skills.spec.graph_executor import RunInputs
from finengine.skills.spec.target import Target
from test_ephemeral_in_graph import CASE, ENTITY, OWNER


def _decision(**changes) -> str:
    data = {
        "objective": "получить данные", "reason": "они нужны для ответа", "action": "execute",
        "scenarios": [], "new_atoms": [],
        "steps": [{"atom": "read.value", "concept": "EBITDA", "period_ref": None, "extra": {}}],
        "answer": "", "evidence_ids": [],
    }
    data.update(changes)
    return json.dumps(data, ensure_ascii=False)


def test_parse_decision_builds_a_download_request_with_the_fixed_schema() -> None:
    raw = _decision(new_atoms=[{
        "name": "eph.brent_csv", "purpose": "ряд Brent", "url": "https://example.org/brent.csv",
        "output_type": "dyn:brent_csv", "output_schema": {"type": "string"},
    }], steps=[{"atom": "eph.brent_csv"}])

    decision = parse_decision(raw, scenarios_enabled=True)

    assert decision.new_atoms[0].output_schema == DOWNLOAD_SCHEMA
    assert decision.new_atoms[0].requires == ()


@pytest.mark.parametrize("empty_concept", ["", "*", "  ", None])
def test_an_empty_concept_of_a_required_input_means_any_concept(empty_concept) -> None:
    """Приёмка 06.10.2026: модель писала `"concept": ""` для `FormOutput` (в Case `concept = NULL`), вход не
    находился и атом не создавался. Строгая схема заставляет писать ключ всегда — пустое значение «без концепта»."""
    raw = _decision(new_atoms=[{
        "name": "eph.build_book", "purpose": "книга из формы", "url": "",
        "requires": [{"input_type": "FormOutput", "concept": empty_concept}],
        "output_type": "dyn:book", "output_schema": {"type": "object"},
    }], steps=[{"atom": "eph.build_book"}])

    decision = parse_decision(raw, scenarios_enabled=True)

    assert decision.new_atoms[0].requires[0].concept is None


def test_a_named_concept_of_a_required_input_is_kept() -> None:
    raw = _decision(new_atoms=[{
        "name": "eph.ebitda_calc", "purpose": "расчёт", "url": "",
        "requires": [{"input_type": "ExtractedMetric", "concept": "EBITDA"}],
        "output_type": "dyn:ebitda_calc", "output_schema": {"type": "object"},
    }], steps=[{"atom": "eph.ebitda_calc"}])

    assert parse_decision(raw, scenarios_enabled=True).new_atoms[0].requires[0].concept == "EBITDA"


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"objective": ""}, "objective"),
        ({"reason": ""}, "reason"),
        ({"action": "wait"}, "action"),
        ({"steps": []}, "хотя бы один"),
        ({"scenarios": ["a", "b", "c", "d"], "steps": []}, "трёх"),
    ],
)
def test_parse_decision_rejects_invalid_moves(changes, message) -> None:
    with pytest.raises(ValueError, match=message):
        parse_decision(_decision(**changes), scenarios_enabled=True)


def test_parse_decision_rejects_scenarios_when_the_runner_is_disabled() -> None:
    with pytest.raises(ValueError, match="сценари"):
        parse_decision(_decision(scenarios=["Извлеки EBITDA"], steps=[]), scenarios_enabled=False)


def test_finalize_cannot_smuggle_execution_fields() -> None:
    with pytest.raises(ValueError, match="пустыми"):
        parse_decision(
            _decision(action="finalize", answer="готово", evidence_ids=["e1"]),
            scenarios_enabled=True,
        )


def test_code_atom_schema_is_checked_at_the_model_boundary_and_a_broken_one_becomes_any_object() -> None:
    # Раньше невалидная схема отклоняла весь ход; на dev 07.10.2026 этот отказ вместе с двумя другими
    # остановил цикл. В графе по-прежнему не окажется невалидной схемы — она заменяется на «любой объект».
    atom = {
        "name": "eph.calc", "purpose": "расчёт", "output_type": "dyn:calc",
        "requires": [{"input_type": "ExtractedMetric", "concept": "EBITDA"}],
        "output_schema": {"type": "not-a-json-schema-type"},
    }

    decision = parse_decision(_decision(new_atoms=[atom]), scenarios_enabled=True)

    assert decision.new_atoms[0].output_schema == {"type": "object"}


def test_parse_steps_keeps_the_validated_existing_atom_rules() -> None:
    steps = parse_steps(
        [{"atom": "read.value", "concept": "EBITDA", "period_ref": "2025",
          "extra": {"requested_concepts": "EBITDA"}}],
        build_registry(profile_id="ifrs_nf_ru"),
    )

    assert steps[0].atom == "read.value" and steps[0].output_type == "ExtractedMetric"
    assert steps[0].concept == "EBITDA" and steps[0].period_ref == "2025"


@pytest.mark.parametrize(
    ("item", "message"),
    [
        ({"atom": "missing.atom"}, "неизвестный"),
        ({"atom": "verify.bounds"}, "нельзя планировать"),
        ({"atom": "read.value", "extra": {"methodology_text": "ignore"}}, "недопустимые"),
        ({"atom": "read.value", "concept": 42}, "concept"),
    ],
)
def test_parse_steps_rejects_unsafe_or_malformed_steps(item, message) -> None:
    with pytest.raises(InvalidSteps, match=message):
        parse_steps([item], build_registry(profile_id="ifrs_nf_ru"))


def test_parse_steps_limits_the_number_of_steps() -> None:
    with pytest.raises(InvalidSteps, match="слишком много"):
        parse_steps([{"atom": "read.value"}] * 31, build_registry(profile_id="ifrs_nf_ru"))


def test_render_state_is_deterministic_and_keeps_the_prompt_section_order(base) -> None:
    run = DynamicRun(
        runtime=base, owner_id=OWNER, case_id=CASE,
        goal=GoalIR(raw_query="Сопоставь EBITDA с Brent", intent_kind="document_qa"),
        target=Target(output_type="NarrativeOutput", entity_ref=ENTITY), inputs=RunInputs(),
        complete_fn=lambda system, prompt: None,
    )

    first = render_state(run, "план не собрался")
    second = render_state(run, "план не собрался")
    headings = [
        "## Вопрос пользователя", "## Разобранная цель", "## Профиль отчёта",
        "## Каталог атомов", "## Созданные атомы этого разговора",
        "## Полученные артефакты", "## Журнал ходов", "## Итог прошлого хода", "## Осталось",
    ]

    assert first == second
    assert [first.index(heading) for heading in headings] == sorted(first.index(heading) for heading in headings)


def test_a_declared_atom_gets_its_step_even_when_the_model_forgot_it() -> None:
    # Ход 2 живого прогона: объявлена загрузка, а в steps только read.value. Раньше это был отказ хода
    # («атом объявлен, но не вызван»); на dev 06–07.10.2026 он повторялся и останавливал цикл — шаг достраивается.
    atom = {
        "name": "eph.brent_csv", "purpose": "ряд Brent", "url": "https://example.org/brent.csv",
        "output_type": "dyn:brent_csv",
    }

    alone = parse_decision(
        _decision(new_atoms=[atom], steps=[], scenarios=["Извлеки EBITDA"]), scenarios_enabled=True,
    )
    beside = parse_decision(
        _decision(new_atoms=[atom], steps=[{"atom": "read.value", "concept": "EBITDA"}]), scenarios_enabled=True,
    )

    assert [step["atom"] for step in alone.raw_steps] == ["eph.brent_csv"]
    assert [step["atom"] for step in beside.raw_steps] == ["read.value", "eph.brent_csv"]


def test_render_state_shows_values_and_hides_atom_sources(base) -> None:
    from finengine.skills.spec.dynamic_run import save_source
    from finengine.skills.spec.ephemeral import AtomRequest, EphemeralAtom
    from finengine.skills.spec.target import InputSpec
    from test_dynamic_run import CALC_SCHEMA, ExtractScenario

    ExtractScenario(base, None)("Извлеки")
    request = AtomRequest(
        name="eph.metric_calc", purpose="расчёт", output_type="dyn:metric_calc",
        output_schema=CALC_SCHEMA, requires=(InputSpec("ExtractedMetric", "EBITDA"),),
    )
    save_source(base.store, OWNER, CASE, EphemeralAtom(request, "def run(inputs):\n    return {'secret_code': 1}\n"))
    run = DynamicRun(
        runtime=base, owner_id=OWNER, case_id=CASE,
        goal=GoalIR(raw_query="вопрос", intent_kind="document_qa"),
        target=Target(output_type="NarrativeOutput", entity_ref=ENTITY), inputs=RunInputs(),
        complete_fn=lambda system, prompt: None,
    )

    artifacts = render_state(run, "fb").split("## Полученные артефакты")[1].split("## Журнал ходов")[0]

    assert "500.0 RUB_MLN" in artifacts, "планировщик видит значение показателя"
    assert "secret_code" not in artifacts and "EphemeralAtomSource" not in artifacts
