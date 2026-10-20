"""`aifa_register_atom` и `aifa_report` (CONTRACTS.md §3.1, ADR-0042 п. 1, ADR-0043 пп. 4, 6, ADR-0044 п. 1).

Проверка и регистрация атома — одно место (`atom_intake`); счётчик попыток общий для отчётов и регистраций.
Код исполняет `local_room` (подмена `sealed_room.run_code` запуском той же точки входа комнаты).
"""
from __future__ import annotations

import asyncio
import threading

import pytest
from dynamic_support import MARGIN_CODE, margin_request
from loop_support import Cycle, Gateway, _NoGoal, make_passport, open_cycle
from test_dynamic_run import ExtractScenario, _move
from test_ephemeral_in_graph import CASE, OWNER
from tool_harness import Plugin, ctx_of, first_line, open_run

from finengine.assistant.loop_desk import LoopDesk
from finengine.assistant.tool_api import ToolApi
from finengine.skills.spec import sealed_room
from finengine.skills.spec.atom_intake import AttemptLog
from finengine.skills.spec.dynamic_run import RunLimits, load_case_atoms

WRONG_SHAPE = "def run(inputs):\n    return {'margin_pct': 'много'}\n"
BAD_CODE = "def run(inputs):\n    return {'margin_pct': 1 / 0}\n"
NAME = "eph.ebitda_margin"


def first_line_of(text: str) -> str:
    return text.split("\n", 1)[0]


def register_args(code: str = MARGIN_CODE, **changes) -> dict:
    request = margin_request(**changes)
    return {
        "name": request.name, "purpose": request.purpose, "output_type": request.output_type,
        "output_schema": request.output_schema, "code": code,
        "requires": [{"input_type": spec.input_type, "concept": spec.concept} for spec in request.requires],
    }


async def cycle_with_inputs(base, **kwargs) -> Cycle:
    """Цикл, в `Case` которого уже есть EBITDA и выручка: такому атому есть из чего считать."""
    passport = make_passport()
    cycle = await open_cycle(base, scenario=ExtractScenario(base, passport), passport=passport, document_text="отчёт", **kwargs)
    await cycle.move(_move(scenarios=("Извлеки EBITDA и выручку",)))
    return cycle


def attempts(cycle: Cycle) -> list[tuple[int, str, int, str]]:
    return [
        (e["move"], e["atom"], e["attempt"], e["status"]) for e in cycle.events if e["type"] == "author_attempt"
    ]


async def test_a_working_atom_is_registered_under_its_name_and_version(base, local_room) -> None:
    """LR-48, C4.3: регистрация называет `eph.<имя>@<версия>` (версия — хэш контракта и кода), атом лежит в сессии."""
    cycle = await cycle_with_inputs(base)

    reply = await cycle.call("aifa_register_atom", register_args(), depth=1)

    version = cycle.session.atoms[NAME].version
    assert first_line_of(reply) == "ГОТОВО" and f"{NAME}@{version}" in reply
    assert cycle.session.atoms[NAME].code == MARGIN_CODE
    assert attempts(cycle) == [(1, NAME, 1, "ok")]
    await cycle.close()


async def test_a_registered_atom_runs_as_a_step_of_the_next_move(base, local_room) -> None:
    """LR-55, C4.3: после успеха шаг `aifa_move` с `eph`-атомом исполняется как любой атом."""
    cycle = await cycle_with_inputs(base)
    await cycle.call("aifa_register_atom", register_args(), depth=1)

    moved = await cycle.call("aifa_move", {"objective": "маржа", "reason": "нужна", "steps": [{"atom": NAME}]})

    assert first_line_of(moved) == "ГОТОВО" and "dyn:ebitda_margin" in moved
    await cycle.close()


async def test_a_failing_code_is_refused_with_the_tail_and_the_attempts_left(base, local_room) -> None:
    """C4.3: ошибка кода — `ОТКАЗ` с хвостом ошибки и остатком попыток; атом не зарегистрирован."""
    cycle = await cycle_with_inputs(base)

    reply = await cycle.call("aifa_register_atom", register_args(BAD_CODE), depth=1)

    assert first_line_of(reply) == "ОТКАЗ неверные_аргументы"
    assert "ZeroDivisionError" in reply and "осталось попыток: 2" in reply
    assert NAME not in cycle.session.atoms
    await cycle.close()


async def test_the_fourth_failed_registration_of_a_name_in_one_move_is_refused_by_the_limit(base, local_room) -> None:
    """LR-44, C4.3: после трёх неудач на имя за ход цикла — `лимит_попыток`; на следующем ходе счёт заново."""
    cycle = await cycle_with_inputs(base)

    for _ in range(3):
        assert first_line_of(await cycle.call("aifa_register_atom", register_args(BAD_CODE), depth=1)).startswith("ОТКАЗ неверные")
    fourth = await cycle.call("aifa_register_atom", register_args(BAD_CODE), depth=1)
    await cycle.move(_move(scenarios=("ещё одно извлечение",)))
    again = await cycle.call("aifa_register_atom", register_args(BAD_CODE), depth=1)

    assert first_line_of(fourth) == "ОТКАЗ лимит_попыток"
    assert first_line_of(again) == "ОТКАЗ неверные_аргументы", "новый ход цикла — новый счёт"
    assert [a[2] for a in attempts(cycle) if a[0] == 1] == [1, 2, 3], "четвёртая попытка проверкой не была"
    await cycle.close()


async def test_a_station_fault_does_not_spend_an_attempt(base, monkeypatch) -> None:
    """LR-52, C5, ADR-0043 п. 6: сбой станции — `СБОЙ_СЛУЖБЫ`, предел неудач не растёт."""
    cycle = await cycle_with_inputs(base)
    monkeypatch.setattr(
        sealed_room, "run_code", lambda *_a, **_k: sealed_room.CodeRun(None, "станция не исполнила код", station_fault=True)
    )

    for _ in range(4):
        reply = await cycle.call("aifa_register_atom", register_args(), depth=1)
        assert first_line_of(reply).startswith("СБОЙ_СЛУЖБЫ")

    assert [a[3] for a in attempts(cycle)] == ["station_fault"] * 4
    await cycle.close()


async def test_a_contract_that_breaks_the_rules_is_refused_before_the_room(base, monkeypatch) -> None:
    """LR-42: правила `check_request` (имя `eph.<snake_case>`, `dyn:*`-тип) держит служба; станция не вызывается."""
    cycle = await cycle_with_inputs(base)
    monkeypatch.setattr(sealed_room, "run_code", lambda *_a, **_k: pytest.fail("до станции дойти не должно"))

    bad_name = await cycle.call("aifa_register_atom", register_args(name="margin"), depth=1)
    bad_type = await cycle.call("aifa_register_atom", register_args(output_type="ExtractedMetric"), depth=1)

    assert first_line_of(bad_name) == "ОТКАЗ неверные_аргументы" and "eph.<snake_case>" in bad_name
    assert first_line_of(bad_type) == "ОТКАЗ не_dyn_тип"
    await cycle.close()


async def test_an_atom_without_inputs_in_the_case_is_refused(base, local_room) -> None:
    """LR-43, C4.3: входов `requires` в `Case` ещё нет — `нет_входов` (подсказка: получи их ходом)."""
    cycle = await open_cycle(base, scenario=ExtractScenario(base, None))

    reply = await cycle.call("aifa_register_atom", register_args(), depth=1)

    assert first_line_of(reply) == "ОТКАЗ нет_входов"
    await cycle.close()


async def test_a_result_of_the_wrong_shape_is_a_failed_registration(base, local_room) -> None:
    """LR-47: результат обязан соответствовать `output_schema`; код с другой формой атомом не становится."""
    cycle = await cycle_with_inputs(base)

    reply = await cycle.call("aifa_register_atom", register_args(WRONG_SHAPE), depth=1)

    assert first_line_of(reply) == "ОТКАЗ неверные_аргументы" and "output_schema" in reply
    assert NAME not in cycle.session.atoms
    await cycle.close()


async def test_the_source_of_a_registered_atom_is_kept_in_the_case(base, local_room) -> None:
    """LR-49: исходник принятого атома лежит в `Case` — после перезапуска цикл поднимет его оттуда."""
    cycle = await cycle_with_inputs(base)
    await cycle.call("aifa_register_atom", register_args(), depth=1)

    kept = load_case_atoms(base.store, OWNER, CASE)

    assert kept[NAME].code == MARGIN_CODE and kept[NAME].version == cycle.session.atoms[NAME].version
    await cycle.close()


async def test_registration_without_a_cycle_is_refused_as_no_cycle() -> None:
    """C3.9: цикл открывает только `НУЖЕН_ЦИКЛ`; до него `aifa_register_atom` — `ОТКАЗ нет_цикла`."""
    run = open_run()
    plugin = Plugin(ToolApi(Gateway(), _NoGoal(), LoopDesk()))
    ctx = ctx_of(run.run_id, task_id="sub-task", root_task_id="root-task", task_depth=1)

    reply = await plugin.call("aifa_register_atom", ctx, register_args())

    assert first_line(reply) == "ОТКАЗ нет_цикла"
    await plugin.close()
    await run.end()


async def test_the_limit_of_new_atoms_is_the_limit_of_the_run(base, local_room) -> None:
    """LR-44: новые атомы ограничены `RunLimits` — `лимит_атомов`."""
    cycle = await cycle_with_inputs(base, limits=RunLimits(max_new_atoms=1))
    await cycle.call("aifa_register_atom", register_args(), depth=1)

    reply = await cycle.call("aifa_register_atom", register_args(name="eph.second_margin"), depth=1)

    assert first_line_of(reply) == "ОТКАЗ лимит_атомов"
    await cycle.close()


async def test_registration_needs_an_open_cycle_and_stops_at_the_limit_and_after_the_finish(base, local_room) -> None:
    """C3.8: без цикла — `нет_цикла`, после `ЛИМИТ` — `ЛИМИТ`, после `aifa_finish` — `цикл_закрыт`."""
    cycle = await cycle_with_inputs(base, limits=RunLimits(max_moves=1))

    assert first_line_of(await cycle.call("aifa_register_atom", register_args(), depth=1)) == "ЛИМИТ"
    await cycle.call("aifa_finish", {"action": "give_up"})
    assert first_line_of(await cycle.call("aifa_register_atom", register_args(), depth=1)) == "ОТКАЗ цикл_закрыт"
    await cycle.close()


async def test_a_new_registration_of_a_name_lifts_its_dead_end(base, local_room) -> None:
    """ADR-0043 п. 6: новая регистрация имени снимает сигнатуры его тупика — шаг с ним можно повторить."""
    cycle = await cycle_with_inputs(base)
    await cycle.call("aifa_register_atom", register_args(), depth=1)
    cycle.session.dead_end_actions[("step", NAME, "", "", "")] = cycle.session.evidence_generation

    await cycle.call("aifa_register_atom", register_args(MARGIN_CODE + "\n# вторая версия\n"), depth=1)

    assert not [signature for signature in cycle.session.dead_end_actions if signature[1:2] == (NAME,)]
    await cycle.close()


async def test_reports_and_registrations_of_one_name_share_one_counter_per_move(base, local_room) -> None:
    """LR-53, LR-56, C4.4: `aifa_report` и `aifa_register_atom` нумеруются одним счётчиком на (ход цикла, имя), `move` — последний начатый."""
    cycle = await cycle_with_inputs(base)

    await cycle.call("aifa_report", {"label": NAME, "status": "error", "note": "не сошлось", "code": BAD_CODE}, depth=1)
    await cycle.call("aifa_register_atom", register_args(BAD_CODE), depth=1)
    await cycle.call("aifa_report", {"label": NAME, "status": "ok", "note": "исправил"}, depth=1)
    await cycle.call("aifa_register_atom", register_args(), depth=1)
    await cycle.move(_move(scenarios=("следующий ход",)))
    await cycle.call("aifa_report", {"label": NAME, "status": "ok", "note": "ещё раз"}, depth=1)

    assert attempts(cycle) == [
        (1, NAME, 1, "error"), (1, NAME, 2, "error"), (1, NAME, 3, "ok"), (1, NAME, 4, "ok"), (2, NAME, 1, "ok"),
    ]
    await cycle.close()


async def test_a_report_carries_its_note_as_the_error_and_the_code_it_was_given(base) -> None:
    cycle = await cycle_with_inputs(base)

    await cycle.call("aifa_report", {"label": NAME, "status": "error", "note": "деление на ноль", "code": BAD_CODE}, depth=1)

    (event,) = [e for e in cycle.events if e["type"] == "author_attempt"]
    assert event["error"] == "деление на ноль" and event["code"] == BAD_CODE and event["status"] == "error"
    await cycle.close()


async def test_the_counter_gives_each_concurrent_caller_its_own_number() -> None:
    """C3.8: увеличение счётчика атомарно — параллельные вызовы не получают одинаковый номер."""
    log = AttemptLog()
    numbers: list[int] = []

    def take() -> None:
        for _ in range(200):
            numbers.append(log.number(1, NAME))

    threads = [threading.Thread(target=take) for _ in range(8)]
    for thread in threads:
        thread.start()
    await asyncio.to_thread(lambda: [thread.join() for thread in threads])

    assert sorted(numbers) == list(range(1, 1601))


async def test_a_report_with_a_wrong_status_or_without_a_label_is_a_bad_argument(base) -> None:
    cycle = await cycle_with_inputs(base)

    assert first_line_of(await cycle.call("aifa_report", {"label": NAME, "status": "бывает"}, depth=1)) == "ОТКАЗ неверные_аргументы"
    assert first_line_of(await cycle.call("aifa_report", {"label": "", "status": "ok"}, depth=1)) == "ОТКАЗ неверные_аргументы"
    await cycle.close()


async def test_a_step_repeated_after_a_dead_end_is_allowed_once_the_atom_is_registered_again(base, local_room) -> None:
    """ADR-0043 п. 6, C4.3: тупик шага с `eph`-атомом снимает новая регистрация имени; без неё повтор — `повтор_тупика`."""
    cycle = await cycle_with_inputs(base)
    step = {"objective": "маржа", "reason": "нужна", "steps": [{"atom": NAME}]}
    await cycle.call("aifa_register_atom", register_args(), depth=1)
    first = await cycle.call("aifa_move", step)
    second = await cycle.call("aifa_move", step)
    refused = await cycle.call("aifa_move", step)

    await cycle.call("aifa_register_atom", register_args(MARGIN_CODE + "\n# другая версия\n"), depth=1)
    again = await cycle.call("aifa_move", step)

    assert first_line_of(first) == "ГОТОВО" and "новых результатов нет" in second
    assert first_line_of(refused) == "ОТКАЗ повтор_тупика"
    assert first_line_of(again) == "ГОТОВО" and "новых результатов нет" not in again
    await cycle.close()
