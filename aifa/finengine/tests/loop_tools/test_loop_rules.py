"""Правила цикла через API инструментов (шаг 13): каждое правило из `aifa/contracts/loop_rules.yaml` — тест здесь.

Ведущий — клиент, играющий ouroboros; правила держит `LoopSession`, поэтому отказ приходит кодом §3.0, а не
текстом навыка (F8, ADR-0023). Тесты правил самого исполнителя (комната, загрузчик) остаются в `tests/dynamic_run/`.
"""
from __future__ import annotations

import json

from loop_support import open_cycle
from test_deliverables import _file_artifact
from test_dynamic_run import (
    ExtractScenario,
    RefusingScenario,
    _finalize,
    _give_up,
    _move,
)
from test_ephemeral_atoms import _metric
from test_ephemeral_in_graph import CASE, ENTITY, OWNER

from finengine.contracts import NodeKey, VerificationFinding
from finengine.skills.spec.dynamic_run import RunLimits
from finengine.skills.spec.ephemeral import AtomRequest
from finengine.skills.spec.target import InputSpec
from finengine.store.cases import evidence_id_of

UNIT_LESS = {"type": "object", "properties": {"value": {"type": "number"}}}


def cited(base, concept: str = "EBITDA") -> str:
    """id показателя, который лежит в Case (id метрики-подставки случаен: берём настоящий)."""
    return next(
        evidence_id_of(payload) for row in base.store.list_artifacts(OWNER, CASE)
        for payload in row["payloads"] if getattr(payload, "concept", None) == concept
    )


def code_of(reply: str) -> str:
    return reply.split("\n", 1)[0]


def _atom(**changes) -> AtomRequest:
    fields = dict(
        name="eph.probe", purpose="проба", output_type="dyn:probe", output_schema={"type": "object"},
        url="https://data.example.org/a.csv",
    )
    fields.update(changes)
    return AtomRequest(**fields)


def _refused_move(atom: AtomRequest) -> str:
    return _move(atoms=(atom,), steps=({"atom": atom.name},))


def _put_file(base) -> str:
    file = _file_artifact()
    base.store.put_artifact(
        OWNER, CASE, node_key=NodeKey(atom_name="out.file", atom_version="1", args_fingerprint="file"),
        output_type="FileArtifact", payloads=(file,), concept=None, entity_ref=ENTITY, period_ref=None,
        basis=None, document_version="sha256:v1", policy_version=base.policy.policy_version,
    )
    return file.artifact_id


async def test_an_atom_may_produce_only_a_dyn_type(base) -> None:
    """LR-01: временный атом производит только `dyn:*` — `check_request` отказывает кодом `не_dyn_тип`."""
    cycle = await open_cycle(base)

    reply = await cycle.move(_refused_move(_atom(output_type="ExtractedMetric")))

    assert code_of(reply) == "ОТКАЗ не_dyn_тип"
    await cycle.close()


async def test_a_download_atom_cannot_have_inputs(base) -> None:
    """LR-03: у загрузки нет `requires` — она не видит данных Case; контракт отклонён."""
    cycle = await open_cycle(base)

    reply = await cycle.move(_refused_move(_atom(requires=(InputSpec("ExtractedMetric", "EBITDA"),))))

    assert code_of(reply).startswith("ОТКАЗ") and "не получает входов" in reply
    await cycle.close()


async def test_a_number_of_the_result_needs_its_unit(base) -> None:
    """LR-07: у каждого числа результата есть единица (`x-unit`); без неё — `нет_единицы`."""
    cycle = await open_cycle(base)
    request = _atom(url="", output_schema=UNIT_LESS, requires=(InputSpec("ExtractedMetric", "EBITDA"),))

    assert code_of(await cycle.move(_refused_move(request))) == "ОТКАЗ нет_единицы"
    await cycle.close()


async def test_a_code_atom_needs_inputs_that_are_already_in_the_case(base) -> None:
    """LR-22: атом с кодом пишется, когда его входы уже в Case; иначе `нет_входов`, а не вызов автора."""
    cycle = await open_cycle(base)
    request = _atom(url="", requires=(InputSpec("ExtractedMetric", "EBITDA"),))

    reply = await cycle.move(_refused_move(request))

    assert code_of(reply) == "ОТКАЗ нет_входов" and "входов атома eph.probe" in reply
    await cycle.close()


async def test_the_same_dead_end_move_is_refused(base) -> None:
    """LR-23: точный повтор хода без новых результатов — `повтор_тупика`."""
    cycle = await open_cycle(base, scenario=RefusingScenario())
    move = _move(scenarios=("Извлеки EBITDA",))

    first = await cycle.move(move)
    second = await cycle.move(move)

    assert code_of(first) == "ГОТОВО" and "данных нет в отчёте" in first
    assert code_of(second) == "ОТКАЗ повтор_тупика"
    await cycle.close()


async def test_a_move_has_at_most_three_actions(base) -> None:
    """LR-20: за ход не больше трёх действий — сценарии и части цели вместе."""
    cycle = await open_cycle(base, scenario=RefusingScenario())

    reply = await cycle.move(_move(scenarios=("а", "б", "в", "г")))

    assert code_of(reply) == "ОТКАЗ неверные_аргументы" and "не больше трёх" in reply
    await cycle.close()


async def test_a_part_of_the_goal_must_be_one_the_goal_has(base) -> None:
    """LR-24: части цели называют по `goal_ref` из разобранной цели; неизвестная — отказ с перечнем."""
    cycle = await open_cycle(base)
    cycle.session.run_goals = type("Goals", (), {"refusal": lambda self, clause: None})()
    arguments = json.loads(_move(scenarios=()))
    arguments.update(goals=["goal-9"], steps=[])

    reply = await cycle.call("aifa_move", arguments)

    assert code_of(reply) == "ОТКАЗ неверные_аргументы" and "нет части" in reply
    await cycle.close()


async def test_a_finish_cannot_cite_what_is_not_in_the_case(base) -> None:
    """LR-16 (до шага 13a): `aifa_finish` со ссылкой на несуществующий артефакт — отказ."""
    cycle = await open_cycle(base)

    reply = await cycle.finish(_finalize("no-such-id"))

    assert code_of(reply) == "ОТКАЗ неверные_аргументы" and "несуществующие артефакты" in reply
    assert not cycle.session.closed
    await cycle.close()


async def test_a_finish_cannot_cite_a_failed_verification(base) -> None:
    """LR-16 (до шага 13a): артефакт, чью проверку не прошло, ответом не становится."""
    metric = _metric("EBITDA", 500.0)
    base.store.put_artifact(
        OWNER, CASE, node_key=NodeKey(atom_name="read.value", atom_version="1", args_fingerprint="m"),
        output_type="ExtractedMetric", payloads=(metric,), concept="EBITDA", entity_ref=ENTITY, period_ref="2024",
        basis=None, document_version="sha256:v1", policy_version=base.policy.policy_version,
    )
    finding = VerificationFinding(
        check_kind="bounds", status="fail", detail="значение вне границ", source_ref=metric.scalar_fact.evidence_id,
        produced_by_atom="verify.bounds", produced_by_atom_version="1",
    )
    base.store.put_artifact(
        OWNER, CASE, node_key=NodeKey(atom_name="verify.bounds", atom_version="1", args_fingerprint="f"),
        output_type="VerificationFinding", payloads=(finding,), concept=None, entity_ref=ENTITY, period_ref=None,
        basis=None, document_version="sha256:v1", policy_version=base.policy.policy_version,
    )
    cycle = await open_cycle(base)

    reply = await cycle.finish(_finalize(metric.scalar_fact.evidence_id))

    assert code_of(reply) == "ОТКАЗ неверные_аргументы" and "проваленную проверку" in reply
    await cycle.close()


async def test_a_finish_with_a_citable_result_is_accepted(base) -> None:
    """LR-16: найденное доказательство — `ПРИНЯТО`; цикл закрыт."""
    cycle = await open_cycle(base, scenario=ExtractScenario(base, None))
    await cycle.move(_move(scenarios=("Извлеки EBITDA",)))

    reply = await cycle.finish(_finalize(cited(base)))

    assert code_of(reply) == "ПРИНЯТО" and cycle.session.closed
    await cycle.close()


async def test_the_promised_file_must_be_built_before_the_finish(base) -> None:
    """LR-29: обещанный планом файл не собран — `файл_не_собран`, пока цикл не потратил ходы с кодом атома."""
    cycle = await open_cycle(base, scenario=ExtractScenario(base, None))
    arguments = json.loads(_move(scenarios=("Извлеки EBITDA",)))
    arguments.update(plan=["получить показатели", "собрать Excel"], files=[{"kind": "xlsx", "parts": 0, "formulas": False}])
    await cycle.call("aifa_move", arguments)

    reply = await cycle.finish(_finalize(cited(base)))

    assert code_of(reply) == "ОТКАЗ файл_не_собран" and "xlsx" in reply
    await cycle.close()


async def test_a_built_file_is_published_to_the_screen_with_the_finish(base) -> None:
    """LR-31: собранный файл уходит в ответ сам и публикуется событием `file` при `aifa_finish`."""
    file_id = _put_file(base)
    cycle = await open_cycle(base)
    cycle.session.initial_file_ids = frozenset()

    reply = await cycle.call("aifa_finish", {"action": "finalize", "deliverables": [file_id]})

    assert code_of(reply) == "ПРИНЯТО" and "итоги.xlsx" in reply
    assert [event["type"] for event in cycle.events if event["type"] == "file"] == ["file"]
    final = [event for event in cycle.events if event.get("action") == "finalize"][0]
    assert final["details"] == "файлы: итоги.xlsx"
    await cycle.close()


async def test_the_cycle_is_finite_and_the_limit_leaves_only_the_last_word(base) -> None:
    """LR-17: `RunLimits` — после исчерпания ходов `aifa_move` отвечает `ЛИМИТ`, а `aifa_finish` принимается."""
    cycle = await open_cycle(base, scenario=ExtractScenario(base, None), limits=RunLimits(max_moves=2))

    first = await cycle.move(_move(scenarios=("Извлеки EBITDA",)))
    second = await cycle.move(_move(scenarios=("Извлеки выручку",)))
    third = await cycle.move(_move(scenarios=("Ещё",)))

    assert code_of(first) == "ГОТОВО" and code_of(second) == "ЛИМИТ" and code_of(third) == "ЛИМИТ"
    assert "лимит ходов" in third and cycle.session.turns == 2
    assert code_of(await cycle.finish(_finalize(cited(base)))) == "ПРИНЯТО"
    await cycle.close()


async def test_moves_without_progress_exhaust_the_cycle(base) -> None:
    """LR-17: `idle_moves` ходов подряд без новых результатов — `ЛИМИТ` на самом последнем из них."""
    cycle = await open_cycle(base, limits=RunLimits(max_moves=6, idle_moves=2))
    unmade = _atom(output_type="not-dyn")

    replies = [await cycle.move(_refused_move(_atom(name=f"eph.p{index}", output_type="not-dyn"))) for index in range(2)]

    assert [code_of(reply) for reply in replies] == ["ОТКАЗ не_dyn_тип", "ОТКАЗ не_dyn_тип"]
    assert code_of(await cycle.move(_move(scenarios=("а",)))) == "ЛИМИТ"
    assert unmade.name == "eph.probe"
    await cycle.close()


async def test_the_new_atoms_limit_refuses_a_new_atom_but_not_the_old_ones(base) -> None:
    """LR-17: лимит новых атомов `RunLimits` — `лимит_атомов`."""
    cycle = await open_cycle(base, limits=RunLimits(max_new_atoms=0))

    reply = await cycle.move(_refused_move(_atom()))

    assert code_of(reply) == "ОТКАЗ лимит_атомов"
    await cycle.close()


async def test_the_last_word_that_is_refused_closes_the_cycle_with_a_summary(base) -> None:
    """LR-32: после лимита недействительное последнее слово не зацикливает ход — сводка по найденному, как у службы."""
    cycle = await open_cycle(base, scenario=ExtractScenario(base, None), limits=RunLimits(max_moves=1))
    await cycle.move(_move(scenarios=("Извлеки EBITDA",)))

    reply = await cycle.finish(_finalize("no-such-id"))

    assert code_of(reply) == "ПРИНЯТО" and "итог не принят" in reply and "EBITDA" in reply
    assert cycle.session.closed
    await cycle.close()


async def test_give_up_needs_no_evidence(base) -> None:
    """LR-33: `give_up` принимается без ссылок; ответ пользователю — последнее сообщение корня (ADR-0024 п. 1)."""
    cycle = await open_cycle(base)

    reply = await cycle.finish(_give_up())

    assert reply == "ПРИНЯТО\nЦикл закрыт."
    await cycle.close()
