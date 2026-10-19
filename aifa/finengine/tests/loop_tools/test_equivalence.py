"""Цикл ведёт ouroboros вызовами инструментов — итоги и события те же, что у `DynamicRun` (шаг 13, ADR-0029).

Одни записанные решения планировщика идут двумя путями: ответами модели в `DynamicRun` и аргументами вызовов
`aifa_move`/`aifa_finish` через настоящий роут, очередь и `LoopSession`. Меняется одна переменная — ведущий.
"""
from __future__ import annotations

import asyncio
import json

from dynamic_support import normalized_events
from loop_support import make_passport, new_runtime, open_cycle
from test_atom_author import Script
from test_dynamic_run import (
    CALC_CODE,
    CALC_SCHEMA,
    ExtractScenario,
    _finalize,
    _give_up,
    _move,
)
from test_ephemeral_in_graph import CASE, ENTITY, OWNER

from finengine.contracts import GoalIR
from finengine.skills.spec.atom_author import AtomAuthor
from finengine.skills.spec.dynamic_run import DynamicRun
from finengine.skills.spec.ephemeral import AtomRequest, EphemeralAtom
from finengine.skills.spec.graph_executor import RunInputs
from finengine.skills.spec.target import InputSpec, Target

MARGIN = AtomRequest(
    name="eph.margin_with_brent", purpose="маржа EBITDA", output_type="dyn:margin_with_brent",
    output_schema=CALC_SCHEMA,
    requires=(InputSpec("ExtractedMetric", "EBITDA"), InputSpec("ExtractedMetric", "REVENUE")),
)
MARGIN_ID = EphemeralAtom(MARGIN, CALC_CODE).version


def _author() -> AtomAuthor:
    return AtomAuthor(Script([json.dumps({"code": CALC_CODE, "example_inputs": {}}, ensure_ascii=False)]))


def _decisions() -> list[str]:
    return [
        _move(scenarios=("Извлеки EBITDA и выручку",)),
        _move(atoms=(MARGIN,), steps=({"atom": MARGIN.name},)),
        _finalize(MARGIN_ID),
    ]


def _without_final_words(events: list[dict]) -> list[dict]:
    """ADR-0042, ADR-0044 п. 2: у `aifa_finish` нет ни цели, ни причины, ни текста ответа — допустимое различие."""
    final = [index for index, event in enumerate(events) if event.get("action") in ("finalize", "give_up")]
    blank = {"objective": "", "reason": "", "details": ""}
    return [dict(event, **blank) if index in final else event for index, event in enumerate(events)]


async def test_recorded_decisions_give_the_same_outcome_and_events_through_the_tools(
    base, read_value, tmp_path, local_room,
) -> None:
    """LR-34, C3.1, эквивалентность: тот же итог цикла, журнал ходов и события экрана, что у `DynamicRun`."""
    events: list[dict] = []
    passport = make_passport()
    engine_runtime = new_runtime(tmp_path, "engine", read_value)
    engine = DynamicRun(
        runtime=engine_runtime, owner_id=OWNER, case_id=CASE,
        goal=GoalIR(raw_query="сложный вопрос", intent_kind="document_qa"),
        target=Target(output_type="QuoteEvidence", entity_ref=ENTITY), inputs=RunInputs(),
        run_scenario=ExtractScenario(engine_runtime, passport), complete_fn=Script(_decisions()),
        author=_author(), run_passport=passport, event_sink=events.append,
    )
    expected = await asyncio.to_thread(engine.run, deterministic_error="нет пути")

    cycle = await open_cycle(base, scenario=ExtractScenario(base, passport), author=_author(), passport=passport)
    first, second, last = _decisions()
    replies = [await cycle.move(first), await cycle.move(second), await cycle.finish(last)]

    assert expected.status == "finalized"
    assert [reply.split("\n", 1)[0] for reply in replies] == ["ГОТОВО", "ГОТОВО", "ПРИНЯТО"]
    assert cycle.session.closed and tuple(cycle.session.journal) == expected.journal
    assert normalized_events(_without_final_words(cycle.events)) == normalized_events(
        _without_final_words(events)
    )
    final_event = [event for event in cycle.events if event.get("action") == "finalize"][0]
    assert final_event["details"] == "ответ — в чате"
    await cycle.close()


async def test_a_give_up_goes_the_same_way_as_in_the_service_cycle(base) -> None:
    """Эквивалентность: `give_up` — тот же итог, ход пользователю не тратится зря."""
    events: list[dict] = []
    engine = DynamicRun(
        runtime=base,
        owner_id=OWNER, case_id=CASE, goal=GoalIR(raw_query="сложный вопрос", intent_kind="document_qa"),
        target=Target(output_type="QuoteEvidence", entity_ref=ENTITY), inputs=RunInputs(),
        complete_fn=Script([_give_up()]), event_sink=events.append,
    )
    expected = await asyncio.to_thread(engine.run, deterministic_error="нет пути")
    cycle = await open_cycle(base)

    reply = await cycle.finish(_give_up())

    assert reply.split("\n", 1)[0] == "ПРИНЯТО" and expected.status == "gave_up"
    assert cycle.session.closed
    await cycle.close()
