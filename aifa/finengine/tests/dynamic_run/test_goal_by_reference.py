"""Часть 18 доводки: части составной цели исполняются напрямую, по ссылке ``goal_ref``.

Исходный случай (dev, 07.10.2026): «Посчитай EBITDA, найди цены на нефть Brent, посчитай корреляцию
между ними». Классификатор верно разобрал цель на ``calculate_registered_metric`` (EBITDA) и
``custom_analysis(depends_on)``, но планировщик пересказал часть фразой сценария, повторный разбор дал
``extract_registered_metric``, и конвейер ответил ``metric_not_found``.

Вход — настоящий ``FinengineAgent.query_stream`` → ``execute_routed_goal``; ``Case`` — SQLite; код
временного атома исполняет ``local_room``. Подставные только модели (классификатор, планировщик, автор
атомов) и сеть загрузки.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from finengine.contracts import GoalIR
from finengine.skills import production_runtime as production
from finengine.skills.spec.dynamic_run import DynamicRun, RunLimits, parse_decision, render_state
from finengine.skills.spec.graph_executor import RunInputs
from finengine.skills.spec.target import Target
from tests.integration.conftest import _make_fake_locate_and_read_value_executors
from tests.integration.ingress_support import (  # noqa: F401
    Turn,
    gate_env,
    patched_build_registry,
    set_intent_sequence,
)

from test_chat_outcome_contract import SequenceModel, _arm_dynamic
from test_download_atom import _csv_ok, _serve
from test_ephemeral_in_graph import CASE, ENTITY, OWNER

MESSAGE = "Посчитай EBITDA, найди цены на нефть Brent, посчитай корреляцию между ними"
ANALYSIS_PART = "найди цены на нефть Brent, посчитай корреляцию между ними"
_PLAN = {"route": "plan", "targets": [], "entity_ref": None, "period_ref": None, "role": None, "ambiguities": []}
EBITDA_FORM = {
    **_PLAN, "intent_kind": "calculate_registered_metric", "targets": [{"concept": "EBITDA"}],
    "unresolved": [], "goal_ref": "goal-1", "depends_on": [],
    "additional_goals": [{
        **_PLAN, "goal_ref": "goal-2", "intent_kind": "custom_analysis", "depends_on": ["goal-1"],
        "unresolved": [ANALYSIS_PART],
    }],
}
# Вид, который выбрал бы повторный разбор фразы «Извлеки EBITDA…» (на dev так и вышло).
EXTRACT_REPARSE = {
    **_PLAN, "intent_kind": "extract_registered_metric", "targets": [{"concept": "EBITDA"}], "unresolved": [],
}
CALC_REPARSE = {
    **_PLAN, "intent_kind": "calculate_registered_metric", "targets": [{"concept": "EBITDA"}], "unresolved": [],
}
EBITDA_VALUES = {"OPERATING_PROFIT": 400.0, "DEPRECIATION_AMORTIZATION": 60.0, "SALES_REVENUE": 1000.0}

BRENT_ATOM = {
    "name": "eph.brent_csv", "purpose": "цены Brent, CSV", "requires": [], "output_type": "dyn:brent_csv",
    "output_schema": {"type": "object"}, "url": "https://data.example.org/brent.csv",
}
STATS_ATOM = {
    "name": "eph.ebitda_brent", "purpose": "EBITDA и число точек Brent",
    "requires": [
        {"input_type": "CalculatedMetric", "concept": "EBITDA"},
        {"input_type": "dyn:brent_csv", "concept": None},
    ],
    "output_type": "dyn:ebitda_brent",
    "output_schema": {
        "type": "object", "required": ["ebitda", "brent_points"],
        "properties": {"ebitda": {"type": "number", "x-unit": "RUB_MLN"}, "brent_points": {"type": "number", "x-unit": "count"}},
    },
}
STATS_CODE = (
    "def run(inputs):\n"
    "    ebitda = inputs['CalculatedMetric'][0]['derived_fact']['value']\n"
    "    rows = inputs['dyn:brent_csv'][0]['payload']['text'].strip().splitlines()[1:]\n"
    "    return {'ebitda': ebitda, 'brent_points': len(rows)}\n"
)


def _move(**fields) -> dict:
    return {
        "objective": "получить данные", "reason": "они нужны для ответа", "action": "execute",
        "scenarios": [], "goals": [], "new_atoms": [], "steps": [], "answer": "", "evidence_ids": [], **fields,
    }


def _give_up(answer: str = "Не удалось.") -> dict:
    return _move(action="give_up", answer=answer, objective="остановиться", reason=answer)


def _ids(prompt: str, marker: str) -> list[str]:
    return [line.split("|", 1)[0].strip() for line in prompt.splitlines() if f"| {marker} |" in line]


def _finalize_stats(prompt: str) -> dict:
    ids = [*_ids(prompt, "CalculatedMetric"), *_ids(prompt, "dyn:ebitda_brent")]
    return _move(
        action="finalize", objective="ответить", reason="расчёт готов", evidence_ids=ids,
        answer="EBITDA 460,0 млн руб.; точек Brent: 2.",
    )


def _row(prompt: str, marker: str, concept: str) -> list[str] | None:
    """Строка таблицы артефактов: [id, тип, концепт, период, значение] либо None."""
    for line in prompt.splitlines():
        cells = [cell.strip() for cell in line.split("|")]
        if len(cells) == 5 and cells[1:3] == [marker, concept]:
            return cells
    return None


def _arm(monkeypatch, replies: list, *, classifier=None):
    locate, read = _make_fake_locate_and_read_value_executors(EBITDA_VALUES)
    patched_build_registry(monkeypatch, locate, read)
    monkeypatch.setattr(
        "finengine.skills.spec.atom_author._default_complete",
        SequenceModel([{"code": STATS_CODE, "example_inputs": {}}]),
    )
    classifier = classifier or set_intent_sequence(monkeypatch, [EBITDA_FORM, EXTRACT_REPARSE])
    return classifier, _arm_dynamic(monkeypatch, replies)


class Spy:
    """Каждый вызов ``execute_routed_goal`` (внешний и вложенный): какая цель дошла до конвейера."""

    def __init__(self, monkeypatch) -> None:
        self.calls: list[dict] = []
        self.cycles: list[dict] = []
        real_route, real_run = production.execute_routed_goal, production.DynamicRun

        def _route(**kwargs):
            self.calls.append(kwargs)
            return real_route(**kwargs)

        def _cycle(**kwargs):
            self.cycles.append(kwargs)
            return real_run(**kwargs)

        monkeypatch.setattr(production, "execute_routed_goal", _route)
        monkeypatch.setattr(production, "DynamicRun", _cycle)

    @property
    def nested(self) -> list[dict]:
        return [call for call in self.calls if call.get("allow_dynamic") is False]


def _turn(monkeypatch, message: str = MESSAGE) -> tuple[Turn, list[dict]]:
    turn = Turn()
    turn.arm(monkeypatch)
    return turn, turn.run(message)


def _text(events: list[dict]) -> str:
    return next(event["text"] for event in events if event.get("type") == "text_delta")


# --- П18.1: исходный случай -------------------------------------------------------------------------


def test_P18_1_the_original_phrase_goal_ref_calculates_ebitda_download_code_finalize(monkeypatch, local_room) -> None:
    _serve(monkeypatch, _csv_ok)
    classifier, planner = _arm(monkeypatch, [
        _move(goals=["goal-1"]),
        _move(new_atoms=[BRENT_ATOM], steps=[{"atom": "eph.brent_csv"}]),
        _move(new_atoms=[STATS_ATOM], steps=[{"atom": "eph.ebitda_brent"}]),
        _finalize_stats,
    ])
    spy = Spy(monkeypatch)

    turn, events = _turn(monkeypatch)

    errors = [event for event in events if event.get("type") == "typed_error"]
    assert not errors, errors
    assert turn.dispatcher_calls == 0 and len(spy.cycles) == 1
    assert len(classifier.calls) == 1, "модель-классификатор — один раз на сообщение, не на часть цели"
    row = _row(planner.prompts[1], "CalculatedMetric", "EBITDA")
    assert row is not None and row[3] == "unknown" and row[4].startswith("460.0"), planner.prompts[1]
    assert "| dyn:brent_csv |" in planner.prompts[2] and "| dyn:ebitda_brent |" in planner.prompts[3]
    assert "460,0" in _text(events)


def test_P18_2_the_same_goal_sent_as_a_scenario_phrase_works_as_before(monkeypatch, local_room) -> None:
    classifier, planner = _arm(monkeypatch, [
        _move(scenarios=["Рассчитай EBITDA по всем доступным периодам"]),
        _give_up(),
    ], classifier=set_intent_sequence(monkeypatch, [EBITDA_FORM, CALC_REPARSE]))
    spy = Spy(monkeypatch)

    _turn(monkeypatch)

    assert len(spy.cycles) == 1 and len(spy.nested) == 1 and len(classifier.calls) == 2
    assert _row(planner.prompts[1], "CalculatedMetric", "EBITDA") is not None


def test_P18_3_a_single_custom_analysis_does_not_offer_goals_and_runs_as_before(monkeypatch, local_room) -> None:
    single = {**_PLAN, "intent_kind": "custom_analysis", "unresolved": [MESSAGE]}
    _, planner = _arm(monkeypatch, [_give_up()])
    set_intent_sequence(monkeypatch, [single])

    _turn(monkeypatch)

    state = planner.prompts[0]
    block = state.split("## Разобранная цель\n", 1)[1].split("\n\n## ", 1)[0]
    assert block.startswith("goal_type=custom_analysis; показатели=[]") and "goal-" not in block


# --- К18.1: классификатор → состояние → планировщик --------------------------------------------------


def _goal() -> GoalIR:
    return GoalIR.model_validate({
        "raw_query": MESSAGE, "intent_kind": "calculate_registered_metric", "targets": [{"concept": "EBITDA", "output_kind": "scalar"}],
        "goal_ref": "goal-1", "additional_goals": [{
            "goal_ref": "goal-2", "raw_query": MESSAGE, "intent_kind": "custom_analysis",
            "depends_on": ["goal-1"], "unresolved": [ANALYSIS_PART],
        }],
    })


def _run(base, goal: GoalIR, *, with_channel: bool = True) -> DynamicRun:
    channel = production.GoalChannel(context=None, document_text="", role="", event_sink=None) if with_channel else None
    return DynamicRun(
        runtime=base, owner_id=OWNER, case_id=CASE, goal=goal,
        target=Target(output_type="DynamicPlan", entity_ref=ENTITY), inputs=RunInputs(),
        complete_fn=lambda system, prompt: None, limits=RunLimits(), run_goals=channel,
    )


def _goal_block(state: str) -> str:
    return state.split("## Разобранная цель\n", 1)[1].split("\n\n## Профиль отчёта", 1)[0]


def test_K18_1_the_goal_block_says_how_to_get_each_part_and_whether_it_is_obtained(base) -> None:
    run = _run(base, _goal())
    before = _goal_block(render_state(run, ""))

    run.goal_status["goal-1"] = "получена"
    after = _goal_block(render_state(run, ""))

    part1 = next(line for line in before.splitlines() if line.startswith("- goal-1"))
    part2 = next(line for line in before.splitlines() if line.startswith("- goal-2"))
    assert "goal_type=calculate_registered_metric" in part1 and "EBITDA" in part1
    assert 'по ссылке: goals ["goal-1"]' in part1 and "не получена" in part1
    assert "силами планировщика" in part2 and "goals" not in part2 and ANALYSIS_PART in part2
    assert "получена в этом прогоне" in next(line for line in after.splitlines() if line.startswith("- goal-1"))
    prompt = Path("src/finengine/skills/spec/dynamic_run_planner.md").read_text(encoding="utf-8")
    assert "`goals`" in prompt and "goal_ref" in prompt, "имена полей промпта совпадают с выводом состояния"


def test_K18_1_without_the_channel_every_part_is_the_planners_own(base) -> None:
    block = _goal_block(render_state(_run(base, _goal(), with_channel=False), ""))

    assert "по ссылке" not in block and block.count("силами планировщика") == 2


def test_the_state_grows_by_a_hundred_characters_per_part_not_by_a_table(base) -> None:
    block = _goal_block(render_state(_run(base, _goal()), ""))

    assert len(block) < 1_000


# --- К18.2: планировщик → канал → execute_routed_goal -------------------------------------------------


def test_K18_2_the_pipeline_receives_the_clause_itself_and_the_classifier_is_not_called(monkeypatch, local_room) -> None:
    classifier, _ = _arm(monkeypatch, [_move(goals=["goal-1"]), _give_up()])
    spy = Spy(monkeypatch)

    _turn(monkeypatch)

    (nested,) = spy.nested
    original = next(call["goal"] for call in spy.calls if call.get("allow_dynamic") is not False)
    clause = original.clauses()[0]
    goal = nested["goal"]
    assert len(classifier.calls) == 1
    assert (goal.intent_kind, goal.targets, goal.entity_ref, goal.period_ref, goal.role) == (
        clause.intent_kind, clause.targets, clause.entity_ref, clause.period_ref, clause.role,
    )
    assert (goal.parameters, goal.unresolved, goal.ambiguities, goal.raw_query) == (
        clause.parameters, clause.unresolved, clause.ambiguities, clause.raw_query,
    )
    assert not goal.additional_goals and nested["case_id"] == spy.calls[0]["case_id"]


def test_K18_2_the_single_message_with_the_same_goal_gives_the_result_the_reference_gives(monkeypatch, local_room) -> None:
    """Эталон — одиночное сообщение; тот же кортеж обязан лежать в Case после хода по ссылке (тест ниже)."""
    locate, read = _make_fake_locate_and_read_value_executors(EBITDA_VALUES)
    patched_build_registry(monkeypatch, locate, read)
    set_intent_sequence(monkeypatch, [CALC_REPARSE])

    _, events = _turn(monkeypatch, "Рассчитай EBITDA")

    typed = [event["result"] for event in events if event.get("type") == "typed_result"]
    assert {
        (item["output_type"], item["concept"], item["period_ref"], item["derived_fact"]["value"]) for item in typed
    } == {("CalculatedMetric", "EBITDA", "unknown", 460.0)}


def test_K18_2_by_reference_the_case_holds_the_same_result(monkeypatch, local_room) -> None:
    _, planner = _arm(monkeypatch, [_move(goals=["goal-1"]), _give_up()])

    _turn(monkeypatch)

    row = _row(planner.prompts[1], "CalculatedMetric", "EBITDA")
    assert row is not None and row[3] == "unknown" and row[4].startswith("460.0")


# --- К18.3: канал → Case → следующий ход ---------------------------------------------------------------


def test_K18_3_the_results_of_a_part_are_inputs_of_an_atom_in_the_next_move(monkeypatch, local_room) -> None:
    _serve(monkeypatch, _csv_ok)
    _, planner = _arm(monkeypatch, [
        _move(goals=["goal-1"], new_atoms=[BRENT_ATOM], steps=[{"atom": "eph.brent_csv"}]),
        _move(new_atoms=[STATS_ATOM], steps=[{"atom": "eph.ebitda_brent"}]),
        _finalize_stats,
    ])

    _, events = _turn(monkeypatch)

    assert "| dyn:ebitda_brent |" in planner.prompts[2], "атом прочёл CalculatedMetric из Case того же хода"
    assert "460,0" in _text(events) and "2" in _text(events)


# --- К18.4: отказ канала → итог хода → планировщик -----------------------------------------------------


def test_K18_4_a_reference_to_custom_analysis_is_refused_with_the_way_out_and_no_inner_cycle(monkeypatch, local_room) -> None:
    _, planner = _arm(monkeypatch, [_move(goals=["goal-2"]), _give_up()])
    spy = Spy(monkeypatch)

    _turn(monkeypatch)

    feedback = planner.prompts[1].split("## Итог прошлого хода\n", 1)[1].split("\n\n## ", 1)[0]
    assert "goal-2" in feedback and "custom_analysis" in feedback and "по ссылке" in feedback
    assert "шагами" in feedback and "новым атомом" in feedback, "из отказа следует действие"
    assert len(spy.cycles) == 1 and not spy.nested, "ни вложенного цикла, ни запуска конвейера"


def test_K18_4_a_refusal_marks_only_the_part_that_was_refused_not_its_neighbours(monkeypatch, local_room) -> None:
    _, planner = _arm(monkeypatch, [_move(goals=["goal-1", "goal-2"]), _give_up()])
    spy = Spy(monkeypatch)

    _turn(monkeypatch)

    block = _goal_block(planner.prompts[1])
    goal_1 = next(line for line in block.splitlines() if line.startswith("- goal-1"))
    assert "не получена в этом прогоне" in goal_1 and "отказ" not in goal_1, "goal-1 не исполнялась"
    assert not spy.nested


def test_K18_4_a_reference_to_a_missing_part_is_refused_with_the_list_of_real_ones(monkeypatch, local_room) -> None:
    _, planner = _arm(monkeypatch, [_move(goals=["goal-7"]), _give_up()])
    spy = Spy(monkeypatch)

    _turn(monkeypatch)

    feedback = planner.prompts[1].split("## Итог прошлого хода\n", 1)[1].split("\n\n## ", 1)[0]
    assert "goal-7" in feedback and "goal-1" in feedback and "goal-2" in feedback
    assert len(spy.cycles) == 1 and not spy.nested


def test_K18_4_a_part_refused_by_reference_can_be_obtained_by_a_free_scenario(monkeypatch, local_room) -> None:
    """Известный риск: повторный разбор выбрал другой вид. Отказ не запирает планировщика."""
    misclassified = {**EBITDA_FORM, "intent_kind": "extract_registered_metric"}  # классификатор выбрал другой вид
    _, planner = _arm(monkeypatch, [
        _move(goals=["goal-1"]),
        _move(scenarios=["Рассчитай EBITDA по всем доступным периодам"]),
        _give_up(),
    ], classifier=set_intent_sequence(monkeypatch, [misclassified, CALC_REPARSE]))

    _turn(monkeypatch)

    feedback = planner.prompts[1].split("## Итог прошлого хода\n", 1)[1].split("\n\n## ", 1)[0]
    assert "metric_not_found" in feedback or "✗" in feedback
    assert "scenarios" in feedback and "Реестре показателей" in feedback, "отказ ведёт к свободному сценарию расчёта"
    # live dev 07.10.2026: «либо своими шагами» — и ход ушёл в пустые locate/read.value
    assert "шагами locate и read.value" in feedback and "либо своими шагами" not in feedback
    assert _row(planner.prompts[2], "CalculatedMetric", "EBITDA") is not None


# --- Р18.5: несколько частей в одном ходе --------------------------------------------------------------

# Показатель попадает в кандидаты разбора по словам сообщения: «выручку» и «EBITDA» должны в нём стоять.
TWO_PARTS_MESSAGE = "Извлеки выручку, посчитай EBITDA и корреляцию между ними"
TWO_DETERMINISTIC = {
    **_PLAN, "intent_kind": "extract_registered_metric", "targets": [{"concept": "SALES_REVENUE"}],
    "unresolved": [], "goal_ref": "goal-1", "depends_on": [],
    "additional_goals": [
        {**_PLAN, "goal_ref": "goal-2", "intent_kind": "calculate_registered_metric",
         "targets": [{"concept": "EBITDA"}], "depends_on": ["goal-1"], "unresolved": []},
        {**_PLAN, "goal_ref": "goal-3", "intent_kind": "custom_analysis", "depends_on": ["goal-2"],
         "unresolved": [ANALYSIS_PART]},
    ],
}


def test_R18_5_two_parts_with_depends_on_run_as_one_graph(monkeypatch, local_room) -> None:
    _, planner = _arm(monkeypatch, [_move(goals=["goal-1", "goal-2"]), _give_up()])
    set_intent_sequence(monkeypatch, [TWO_DETERMINISTIC])
    spy = Spy(monkeypatch)

    _, events = _turn(monkeypatch, TWO_PARTS_MESSAGE)

    (nested,) = spy.nested
    assert [clause.goal_ref for clause in nested["goal"].clauses()] == ["goal-1", "goal-2"]
    graphs = [event for event in events if event.get("type") == "plan_graph" and "compound" in event.get("planner_version", "")]
    assert graphs, "одна составная цель — один общий граф"
    table = planner.prompts[1]
    assert "| ExtractedMetric | SALES_REVENUE |" in table and "| CalculatedMetric | EBITDA |" in table


def test_R18_5_a_dependent_part_without_its_base_is_refused_naming_the_missing_part(monkeypatch, local_room) -> None:
    _, planner = _arm(monkeypatch, [_move(goals=["goal-2"]), _give_up()])
    set_intent_sequence(monkeypatch, [TWO_DETERMINISTIC])
    spy = Spy(monkeypatch)

    _turn(monkeypatch, TWO_PARTS_MESSAGE)

    feedback = planner.prompts[1].split("## Итог прошлого хода\n", 1)[1].split("\n\n## ", 1)[0]
    assert "goal-2" in feedback and "goal-1" in feedback and "не получен" in feedback
    assert not spy.nested


def test_R18_5_a_dependent_part_runs_alone_once_its_base_is_obtained(monkeypatch, local_room) -> None:
    _, planner = _arm(monkeypatch, [_move(goals=["goal-1"]), _move(goals=["goal-2"]), _give_up()])
    set_intent_sequence(monkeypatch, [TWO_DETERMINISTIC])
    spy = Spy(monkeypatch)

    _turn(monkeypatch, TWO_PARTS_MESSAGE)

    assert len(spy.nested) == 2
    assert [clause.depends_on for clause in spy.nested[1]["goal"].clauses()] == [[]], "опора уже в Case: ссылка снята"
    assert "| CalculatedMetric | EBITDA |" in planner.prompts[2]


# --- Решение планировщика: поле goals ------------------------------------------------------------------


def _reply(**fields) -> str:
    return json.dumps(_move(**fields), ensure_ascii=False)


def test_goals_alone_make_a_valid_execute_and_are_parsed() -> None:
    decision = parse_decision(_reply(goals=["goal-1", "goal-1"]), scenarios_enabled=True, goals_enabled=True)

    assert decision.goals == ("goal-1",), "повтор ссылки в ходе — одна часть"


def test_goals_are_refused_without_a_channel_like_scenarios_without_a_runner() -> None:
    with pytest.raises(ValueError, match="части цели недоступны"):
        parse_decision(_reply(goals=["goal-1"]), scenarios_enabled=True, goals_enabled=False)


@pytest.mark.parametrize("action", ["finalize", "give_up"])
def test_goals_must_be_empty_for_the_terminal_actions(action) -> None:
    with pytest.raises(ValueError, match="goals"):
        parse_decision(_reply(action=action, answer="ответ", goals=["goal-1"]), scenarios_enabled=True, goals_enabled=True)


def test_the_move_limit_on_actions_is_shared_between_scenarios_and_goals() -> None:
    parse_decision(_reply(scenarios=["a", "b"], goals=["goal-1"]), scenarios_enabled=True, goals_enabled=True)
    with pytest.raises(ValueError, match="не больше трёх"):
        parse_decision(_reply(scenarios=["a", "b"], goals=["goal-1", "goal-2"]), scenarios_enabled=True, goals_enabled=True)


def test_goals_must_be_a_list_of_non_empty_strings() -> None:
    with pytest.raises(ValueError, match="goals"):
        parse_decision(_reply(goals="goal-1"), scenarios_enabled=True, goals_enabled=True)


def test_a_reply_without_the_goals_field_is_still_valid() -> None:
    raw = json.dumps({k: v for k, v in _move(scenarios=["Извлеки выручку"]).items() if k != "goals"})

    assert parse_decision(raw, scenarios_enabled=True, goals_enabled=True).goals == ()


def test_the_response_schema_carries_goals_as_a_required_array_of_strings() -> None:
    from finengine.skills.spec.dynamic_run import _PLANNER_RESPONSE_FORMAT

    schema = _PLANNER_RESPONSE_FORMAT["json_schema"]["schema"]
    assert schema["properties"]["goals"] == {"type": "array", "items": {"type": "string"}}
    assert "goals" in schema["required"]


# --- Тупиковое действие ---------------------------------------------------------------------------------


def test_a_repeat_of_an_already_obtained_part_without_new_results_is_a_dead_end(monkeypatch, local_room) -> None:
    _, planner = _arm(monkeypatch, [
        _move(goals=["goal-1"]), _move(goals=["goal-1"]), _move(goals=["goal-1"]), _give_up(),
    ])
    spy = Spy(monkeypatch)

    _turn(monkeypatch)

    assert len(spy.nested) == 2, "третий повтор до конвейера не дошёл"
    assert "повторяет тупиковое действие" in planner.prompts[3] and "часть goal-1" in planner.prompts[3]


# --- К18.5: решение → dynamic_move → экран --------------------------------------------------------------


def test_K18_5_dynamic_move_carries_the_parts_with_a_readable_description(monkeypatch, local_room) -> None:
    _arm(monkeypatch, [_move(goals=["goal-1"]), _give_up()])

    _, events = _turn(monkeypatch)

    moves = [event for event in events if event.get("type") == "dynamic_move" and event["move"] == 1]
    assert {event["status"] for event in moves} == {"running", "done"}
    for event in moves:
        (part,) = event["goals"]
        assert part["goal_ref"] == "goal-1" and "EBITDA" in part["description"] and "Рассчитать" in part["description"]
        assert event["scenarios"] == []
    assert all(event["goals"] == [] for event in events if event.get("type") == "dynamic_move" and event["move"] == 2)


# --- Ограждения: прежние пути ----------------------------------------------------------------------------


def test_a_scenario_inside_the_cycle_still_cannot_start_a_cycle(monkeypatch, local_room) -> None:
    """Р13.3 для свободных сценариев не изменилась."""
    _, planner = _arm(monkeypatch, [_move(scenarios=["Посчитай корреляцию"]), _give_up()])
    set_intent_sequence(monkeypatch, [EBITDA_FORM, EBITDA_FORM])
    spy = Spy(monkeypatch)

    _turn(monkeypatch)

    assert len(spy.cycles) == 1 and "scenario_not_deterministic" in planner.prompts[1]


def test_the_goal_prompt_rule_replaces_the_old_one_not_adds_to_it() -> None:
    prompt = Path("src/finengine/skills/spec/dynamic_run_planner.md").read_text(encoding="utf-8")

    assert "Не меняй вид части, пересказывая её фразой сценария" not in prompt
    assert re.search(r"в `goals`", prompt) and "goal_ref" in prompt
    assert '"goals": ["goal-1"]' in prompt or '"goals": []' in prompt, "пример JSON дополнен полем goals"


def test_the_registry_rules_do_not_override_a_part_of_the_goal() -> None:
    """Живой прогон dev 07.10.2026: часть разобрана как extract_*, а пользователь написал «посчитай» — модель
    выбрала сценарий расчёта из раздела реестра и ссылку не использовала."""
    prompt = Path("src/finengine/skills/spec/dynamic_run_planner.md").read_text(encoding="utf-8").replace("\r\n", "\n")
    registry = prompt.split("## Реестр показателей\n", 1)[1].split("\n## ", 1)[0]

    first_rule = registry.split("\n- ", 1)[1].split("\n- ", 1)[0]
    assert "сначала получи по ссылке" in first_rule and "вид выбрал разбор цели" in first_rule, (
        "правило о ссылке стоит первым в списке: правила реестра ниже него — для показателей вне цели"
    )
    phase = Path("src/finengine/skills/spec/dynamic_run_plan_phase.md").read_text(encoding="utf-8")
    assert "по ссылке" in phase
