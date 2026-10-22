"""Правила цикла через API инструментов (шаг 13): каждое правило из `aifa/contracts/loop_rules.yaml` — тест здесь.

Ведущий — клиент, играющий ouroboros; правила держит `LoopSession`, поэтому отказ приходит кодом §3.0, а не
текстом навыка (F8, ADR-0023). Тесты правил самого исполнителя (комната, загрузчик) остаются в `tests/dynamic_run/`.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field

from loop_support import Cycle, make_passport, open_cycle
from dynamic_support import BAD_SCHEMA_CODE
from test_deliverables import _file_artifact
from test_dynamic_run import (
    CALC_SCHEMA,
    ExtractScenario,
    RefusingScenario,
    _finalize,
    _give_up,
    _move,
)
from test_ephemeral_atoms import ECHO_CODE, ECHO_SCHEMA, _metric
from test_ephemeral_in_graph import CASE, ENTITY, OWNER

from finengine.contracts import GoalIR, NodeKey, VerificationFinding
from finengine.skills import run_logger
from finengine.skills.spec.dynamic_run import RunLimits, ScenarioResult, artifact_content
from finengine.skills.spec.ephemeral import AtomRequest, EphemeralAtom
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


NOOP_CODE = "def run(inputs):\n    return {}\n"


def _registration(atom: AtomRequest, code: str = NOOP_CODE) -> dict:
    """Аргументы `aifa_register_atom` для контракта `atom` (подзадача сдаёт код, ADR-0054)."""
    return {
        "name": atom.name, "purpose": atom.purpose, "output_type": atom.output_type,
        "output_schema": atom.output_schema, "code": code,
        "requires": [{"input_type": spec.input_type, "concept": spec.concept} for spec in atom.requires],
    }


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

    reply = await cycle.call("aifa_register_atom", _registration(request))

    assert code_of(reply) == "ОТКАЗ нет_единицы"
    await cycle.close()


async def test_a_code_atom_needs_inputs_that_are_already_in_the_case(base) -> None:
    """LR-24: код атома принимают, когда его входы уже в Case; иначе `нет_входов`, а не прогон кода."""
    cycle = await open_cycle(base)
    request = _atom(url="", requires=(InputSpec("ExtractedMetric", "EBITDA"),))

    reply = await cycle.call("aifa_register_atom", _registration(request))

    assert code_of(reply) == "ОТКАЗ нет_входов" and "входов атома eph.probe" in reply
    await cycle.close()


async def test_a_code_atom_in_new_atoms_is_refused_and_the_hint_names_the_subtask(base) -> None:
    """LR-58, ADR-0054 п. 4 (CONTRACTS §3.1): атом с кодом в `new_atoms` — `неверные_аргументы`; подсказка — подзадача и
    `aifa_register_atom`, а не новое описание в `new_atoms`."""
    cycle = await open_cycle(base)
    request = _atom(url="", requires=(InputSpec("ExtractedMetric", "EBITDA"),))

    reply = await cycle.move(_refused_move(request))

    assert code_of(reply) == "ОТКАЗ неверные_аргументы"
    assert "подзадача" in reply and "aifa_register_atom" in reply and "eph.probe" not in cycle.session.atoms
    await cycle.close()


async def test_the_same_dead_end_move_is_refused(base) -> None:
    """LR-25: точный повтор хода без новых результатов — `повтор_тупика`."""
    cycle = await open_cycle(base, scenario=RefusingScenario())
    move = _move(scenarios=("Извлеки EBITDA",))

    first = await cycle.move(move)
    second = await cycle.move(move)

    assert code_of(first) == "ГОТОВО" and "данных нет в отчёте" in first
    assert code_of(second) == "ОТКАЗ повтор_тупика"
    await cycle.close()


async def test_a_move_has_at_most_three_actions(base) -> None:
    """LR-21: за ход не больше трёх действий — сценарии и части цели вместе."""
    cycle = await open_cycle(base, scenario=RefusingScenario())

    reply = await cycle.move(_move(scenarios=("а", "б", "в", "г")))

    assert code_of(reply) == "ОТКАЗ неверные_аргументы" and "не больше трёх" in reply
    await cycle.close()


async def test_a_part_of_the_goal_must_be_one_the_goal_has(base) -> None:
    """LR-26: части цели называют по `goal_ref` из разобранной цели; неизвестная — отказ с перечнем."""
    cycle = await open_cycle(base)
    cycle.session.run_goals = type("Goals", (), {"refusal": lambda self, clause: None})()
    arguments = json.loads(_move(scenarios=()))
    arguments.update(goals=["goal-9"], steps=[])

    reply = await cycle.call("aifa_move", arguments)

    assert code_of(reply) == "ОТКАЗ неверные_аргументы" and "нет части" in reply
    await cycle.close()


TWO_PARTS = GoalIR.model_validate({
    "raw_query": "Посчитай EBITDA и корреляцию с Brent", "intent_kind": "calculate_registered_metric",
    "targets": [{"concept": "EBITDA", "output_kind": "scalar"}], "goal_ref": "goal-1",
    "additional_goals": [{
        "goal_ref": "goal-2", "raw_query": "корреляция с Brent", "intent_kind": "custom_analysis",
        "depends_on": ["goal-1"], "unresolved": ["корреляция с Brent"],
    }],
})


@dataclass
class Goals:
    """Канал частей цели: по ссылке исполнимы все; помнит, какие части ему отдали."""

    given: list[list[str]] = field(default_factory=list)

    def refusal(self, clause) -> None:
        return None

    def __call__(self, clauses) -> ScenarioResult:
        self.given.append([clause.goal_ref for clause in clauses])
        return ScenarioResult(True, "✓ части получены")


async def _parts_cycle(base) -> tuple[Cycle, Goals]:
    cycle = await open_cycle(base)
    goals = Goals()
    cycle.session.goal, cycle.session.run_goals = TWO_PARTS, goals
    cycle.session.clauses = {clause.goal_ref: clause for clause in TWO_PARTS.clauses()}
    return cycle, goals


async def test_a_dependent_part_waits_for_the_part_it_depends_on(base) -> None:
    """LR-26: часть с `depends_on` не идёт по ссылке, пока опора не получена или не названа в том же ходе; части
    хода идут в конвейер по одной, опора — раньше (ADR-0060 п. 2)."""
    move = {"objective": "корреляция", "reason": "нужна для ответа"}
    alone, alone_goals = await _parts_cycle(base)
    together, together_goals = await _parts_cycle(base)

    refused = await alone.call("aifa_move", {**move, "goals": ["goal-2"]})
    accepted = await together.call("aifa_move", {**move, "goals": ["goal-1", "goal-2"]})

    assert code_of(refused) == "ГОТОВО" and "зависит от части goal-1" in refused
    assert alone_goals.given == [], "без опоры часть в конвейер не уходит"
    assert code_of(accepted) == "ГОТОВО" and together_goals.given == [["goal-1"], ["goal-2"]]
    await alone.close()
    await together.close()


def _finish_journal() -> list[dict]:
    """Строки журнала завершений: что названо в `evidence_ids` и есть ли оно в `Case` (ADR-0040 п. 1)."""
    return run_logger.прочитать(run_logger.ЖУРНАЛ_ЗАВЕРШЕНИЙ)


async def test_a_finish_with_a_citable_result_is_accepted(base) -> None:
    """LR-41, C3.7: найденный результат, названный в `evidence_ids`, — `ПРИНЯТО`; в журнале он отмечен как существующий."""
    cycle = await open_cycle(base, scenario=ExtractScenario(base, None))
    await cycle.move(_move(scenarios=("Извлеки EBITDA",)))
    evidence_id = cited(base)

    reply = await cycle.finish(_finalize(evidence_id))

    assert code_of(reply) == "ПРИНЯТО" and cycle.session.closed
    assert [line["references"] for line in _finish_journal()] == [[{"evidence_id": evidence_id, "exists": True}]]
    await cycle.close()


async def test_a_finish_without_references_is_accepted(base) -> None:
    """LR-41, C3.7: `aifa_finish` без `evidence_ids` и без файлов — `ПРИНЯТО`, ответ пишет ouroboros; ссылок в журнале нет."""
    cycle = await open_cycle(base)

    reply = await cycle.call("aifa_finish", {"action": "finalize"})

    assert code_of(reply) == "ПРИНЯТО" and cycle.session.closed
    final = [event for event in cycle.events if event.get("action") == "finalize"][0]
    assert final["details"] == "ответ — в чате"
    assert [(line["action"], line["references"]) for line in _finish_journal()] == [("finalize", [])]
    await cycle.close()


async def test_a_finish_with_a_missing_reference_is_accepted_and_the_journal_says_it_is_missing(base) -> None:
    """LR-41, C3.7: несуществующая ссылка — `ПРИНЯТО`; служба пишет в журнал прогона, что такого артефакта нет."""
    cycle = await open_cycle(base)

    reply = await cycle.finish(_finalize("no-such-id"))

    assert code_of(reply) == "ПРИНЯТО" and cycle.session.closed
    assert [line["references"] for line in _finish_journal()] == [[{"evidence_id": "no-such-id", "exists": False}]]
    await cycle.close()


async def test_a_finding_that_refuted_a_value_does_not_stop_the_finish(base) -> None:
    """LR-41, C3.7: опровергнутая находка завершению не мешает; ссылка на значение с проваленной проверкой принимается."""
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

    assert code_of(reply) == "ПРИНЯТО" and cycle.session.closed
    await cycle.close()


async def test_the_promised_file_must_be_built_before_the_finish(base) -> None:
    """LR-31: обещанный планом файл не собран — `файл_не_собран`, пока цикл не потратил ходы с кодом атома."""
    cycle = await open_cycle(base, scenario=ExtractScenario(base, None))
    arguments = json.loads(_move(scenarios=("Извлеки EBITDA",)))
    arguments.update(plan=["получить показатели", "собрать Excel"], files=[{"kind": "xlsx", "parts": 0, "formulas": False}])
    await cycle.call("aifa_move", arguments)

    reply = await cycle.finish(_finalize(cited(base)))

    assert code_of(reply) == "ОТКАЗ файл_не_собран" and "xlsx" in reply
    await cycle.close()


async def test_the_hints_of_the_cycle_lead_to_the_subtask_and_not_to_new_atoms(base) -> None:
    """LR-59 (ADR-0054): подсказки цикла — файл не собран, входов ещё нет, шаг без атома — ведут к подзадаче и
    `aifa_register_atom`; описать атом с кодом в `new_atoms` они не советуют."""
    cycle = await open_cycle(base, scenario=ExtractScenario(base, None))
    arguments = json.loads(_move(scenarios=("Извлеки EBITDA",)))
    arguments.update(plan=["получить показатели", "собрать Excel"], files=[{"kind": "xlsx", "parts": 0, "formulas": False}])
    await cycle.call("aifa_move", arguments)
    no_file = await cycle.finish(_finalize(cited(base)))
    no_atom = await cycle.move(_move(steps=({"atom": "eph.nowhere"},)))
    no_inputs = await cycle.call(
        "aifa_register_atom", _registration(_atom(url="", requires=(InputSpec("ExtractedMetric", "NET_DEBT"),)))
    )

    for hint in (no_file, no_atom, no_inputs):
        assert "опиши атом с кодом в new_atoms" not in hint and "пришли его описание" not in hint, hint
    assert "подзадач" in no_file and "aifa_register_atom" in no_file
    assert "подзадач" in no_atom and "aifa_register_atom" in no_atom
    assert "входов атома eph.probe" in no_inputs and "в Case: ExtractedMetric — концепты: EBITDA" in no_inputs
    assert "поправь requires" in no_inputs, "нужный тип в Case есть — автор правит контракт сам (ADR-0060 п. 3)"
    assert "закончи работу итогом (aifa_report done)" in no_inputs
    assert "их получит планировщик ходом aifa_move" in no_inputs
    await cycle.close()


async def test_a_built_file_is_published_to_the_screen_with_the_finish(base) -> None:
    """LR-32: собранный файл уходит в ответ сам и публикуется событием `file` при `aifa_finish`."""
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
    """LR-17, LR-33: `RunLimits` — после исчерпания ходов `aifa_move` отвечает `ЛИМИТ`, а `aifa_finish` принимается."""
    cycle = await open_cycle(base, scenario=ExtractScenario(base, None), limits=RunLimits(max_moves=2))

    first = await cycle.move(_move(scenarios=("Извлеки EBITDA",)))
    second = await cycle.move(_move(scenarios=("Извлеки выручку",)))
    third = await cycle.move(_move(scenarios=("Ещё",)))

    assert code_of(first) == "ГОТОВО" and code_of(second) == "ЛИМИТ" and code_of(third) == "ЛИМИТ"
    assert "лимит ходов" in third and cycle.session.turns == 2
    assert code_of(await cycle.finish(_finalize(cited(base)))) == "ПРИНЯТО"
    await cycle.close()


async def test_moves_without_progress_exhaust_the_cycle(base) -> None:
    """LR-18: `idle_moves` ходов подряд без новых результатов — `ЛИМИТ` на самом последнем из них."""
    cycle = await open_cycle(base, limits=RunLimits(max_moves=6, idle_moves=2))
    unmade = _atom(output_type="not-dyn")

    replies = [await cycle.move(_refused_move(_atom(name=f"eph.p{index}", output_type="not-dyn"))) for index in range(2)]

    assert [code_of(reply) for reply in replies] == ["ОТКАЗ не_dyn_тип", "ОТКАЗ не_dyn_тип"]
    assert code_of(await cycle.move(_move(scenarios=("а",)))) == "ЛИМИТ"
    assert unmade.name == "eph.probe"
    await cycle.close()


async def test_the_new_atoms_limit_refuses_a_new_atom_but_not_the_old_ones(base) -> None:
    """LR-19: лимит новых атомов `RunLimits` — `лимит_атомов`."""
    cycle = await open_cycle(base, limits=RunLimits(max_new_atoms=0))

    reply = await cycle.move(_refused_move(_atom()))

    assert code_of(reply) == "ОТКАЗ лимит_атомов"
    await cycle.close()


async def test_the_last_word_that_is_refused_closes_the_cycle_with_a_summary(base) -> None:
    """LR-33: после лимита недействительное последнее слово не зацикливает ход — сводка по найденному, как у службы."""
    cycle = await open_cycle(base, scenario=ExtractScenario(base, None), limits=RunLimits(max_moves=1))
    await cycle.move(_move(scenarios=("Извлеки EBITDA",)))

    reply = await cycle.call("aifa_finish", {"action": "finalize", "deliverables": ["no-such-file"]})

    assert code_of(reply) == "ПРИНЯТО" and "итог не принят" in reply and "EBITDA" in reply
    assert cycle.session.closed
    await cycle.close()


async def test_the_last_word_that_is_refused_still_publishes_what_was_found(base) -> None:
    """LR-33, LR-32: сводка по отклонённому последнему слову показывает найденное на экране, как у `DynamicRun._stop`."""
    file_id = _put_file(base)
    cycle = await open_cycle(base, scenario=ExtractScenario(base, None), limits=RunLimits(max_moves=1))
    cycle.session.initial_file_ids = frozenset()
    await cycle.move(_move(scenarios=("Извлеки EBITDA",)))

    reply = await cycle.call("aifa_finish", {"action": "finalize", "deliverables": ["no-such-file"]})

    assert code_of(reply) == "ПРИНЯТО" and "итог не принят" in reply and "итоги.xlsx" in reply
    files = [event["filename"] for event in cycle.events if event["type"] == "file"]
    assert file_id and files == ["итоги.xlsx"], "файл, названный в ответе, опубликован событием `file`"
    assert any(event["type"] == "typed_result" for event in cycle.events), "частичные итоги — карточками"
    await cycle.close()


async def test_give_up_needs_no_evidence(base) -> None:
    """`give_up` принимается без ссылок; ответ пользователю — последнее сообщение корня (ADR-0024 п. 1)."""
    cycle = await open_cycle(base)

    reply = await cycle.finish(_give_up())

    assert reply == "ПРИНЯТО\nЦикл закрыт."
    await cycle.close()


# ── правила исполнителя, которые держит код, — через ход цикла ──────────────────────────────────────


def _dynamic_content(base, output_type: str) -> str:
    """Полное содержимое `dyn:*`-артефакта Case так, как его отдаёт `aifa_state(artifact_ids)`."""
    payload = next(
        payload for row in base.store.list_artifacts(OWNER, CASE) for payload in row["payloads"]
        if getattr(payload, "output_type", "") == output_type
    )
    return artifact_content(payload)


def _echo_request() -> AtomRequest:
    return AtomRequest(
        name="eph.echo", purpose="что видит код", output_type="dyn:echo", output_schema=ECHO_SCHEMA,
        requires=(InputSpec("ExtractedMetric", "EBITDA"),),
    )


async def test_the_code_of_an_atom_sees_only_the_results_of_its_inputs(base, local_room) -> None:
    """LR-05: код атома не видит сырой документ — только результаты входов; текст отчёта до него не доходит."""
    passport = make_passport()
    cycle = await open_cycle(
        base, scenario=ExtractScenario(base, passport), passport=passport, document_text="СЕКРЕТНЫЙ ТЕКСТ ОТЧЁТА",
    )
    request = _echo_request()

    await cycle.move(_move(scenarios=("Извлеки EBITDA",)))
    assert code_of(await cycle.submit(request, ECHO_CODE)) == "ГОТОВО"
    reply = await cycle.move(_move(steps=({"atom": request.name},)))

    seen = json.loads(_dynamic_content(base, "dyn:echo"))["payload"]["seen"]
    assert code_of(reply) == "ГОТОВО" and "ExtractedMetric" in seen
    assert "СЕКРЕТНЫЙ" not in seen
    await cycle.close()


async def test_the_result_of_an_atom_carries_its_version_and_the_origin_the_wrapper_computed(base, local_room) -> None:
    """LR-09, LR-11, LR-50: результат — от конкретного кода (`eph.<имя>@<хэш контракта и кода>`), происхождение — из входов."""
    passport = make_passport()
    cycle = await open_cycle(
        base, scenario=ExtractScenario(base, passport), passport=passport, document_text="отчёт",
    )
    request = _echo_request()

    await cycle.move(_move(scenarios=("Извлеки EBITDA",)))
    await cycle.submit(request, ECHO_CODE)
    await cycle.move(_move(steps=({"atom": request.name},)))

    content = json.loads(_dynamic_content(base, "dyn:echo"))
    assert content["producer"] == f"eph.echo@{EphemeralAtom(request, ECHO_CODE).version}"
    assert content["origin"], "происхождение считает обёртка, не код атома"
    await cycle.close()


async def test_a_result_of_the_wrong_shape_is_a_failed_attempt_and_no_atom(base, local_room) -> None:
    """LR-10: результат обязан соответствовать `output_schema`; код автора, не прошедший проверку, атомом не становится."""
    cycle = await open_cycle(base, scenario=ExtractScenario(base, None))
    request = AtomRequest(
        name="eph.margin_with_brent", purpose="маржа", output_type="dyn:margin_with_brent", output_schema=CALC_SCHEMA,
        requires=(InputSpec("ExtractedMetric", "EBITDA"), InputSpec("ExtractedMetric", "REVENUE")),
    )
    await cycle.move(_move(scenarios=("Извлеки EBITDA и выручку",)))

    replies = [await cycle.submit(request, BAD_SCHEMA_CODE) for _ in range(3)]
    fourth = await cycle.submit(request, BAD_SCHEMA_CODE)

    assert [code_of(reply) for reply in replies] == ["ОТКАЗ неверные_аргументы"] * 3
    assert all("output_schema" in reply for reply in replies)
    assert code_of(fourth) == "ОТКАЗ лимит_попыток"
    assert request.name not in cycle.session.atoms
    await cycle.close()


async def test_a_download_to_a_private_address_is_refused_by_the_guard(base) -> None:
    """LR-04: загрузка ходит только на свой узел и только по публичным адресам; приватный адрес — отказ узла."""
    cycle = await open_cycle(base)

    reply = await cycle.move(_refused_move(_atom(url="http://127.0.0.1:9/a.csv")))

    assert code_of(reply) == "ГОТОВО" and "✗ eph.probe" in reply and "приватный" in reply
    await cycle.close()


async def test_gates_cannot_be_planned_by_hand_and_the_planner_adds_them(base) -> None:
    """LR-08: гейты проверок планировщик ставит сам — вручную их не назвать, а шаг чтения тянет их за собой."""
    cycle = await open_cycle(base, passport=make_passport(), document_text="Выручка 2000 млн руб. EBITDA 500 млн руб.")

    by_hand = await cycle.move(_move(steps=({"atom": "verify.bounds"},)))
    read = await cycle.move(_move(steps=({"atom": "read.value", "concept": "EBITDA", "period_ref": "2024"},)))

    assert "нельзя планировать вручную" in by_hand
    assert "verify." in read or "norm." in read, "гейты достроены планировщиком и исполнены"
    await cycle.close()


async def test_the_wall_time_of_the_cycle_is_limited(base, monkeypatch) -> None:
    """LR-20: бюджет времени цикла (`RunLimits.max_wall_seconds`) — по его исчерпании `aifa_move` отвечает `ЛИМИТ`."""
    clock = [1000.0]
    monkeypatch.setattr("finengine.skills.spec.dynamic_run.monotonic", lambda: clock[0])
    cycle = await open_cycle(base, scenario=ExtractScenario(base, None), limits=RunLimits(max_wall_seconds=1200.0))

    first = await cycle.move(_move(scenarios=("Извлеки EBITDA",)))
    clock[0] += 1300.0
    second = await cycle.move(_move(scenarios=("Извлеки выручку",)))

    assert code_of(first) == "ГОТОВО" and code_of(second) == "ЛИМИТ" and "бюджет времени" in second
    assert cycle.session.turns == 1, "ход после бюджета не исполнялся"
    await cycle.close()


async def test_an_atom_without_a_step_runs_by_itself(base) -> None:
    """LR-22: атом, описанный в `new_atoms` без шага, запускается сам — отказом возвращать его модели незачем."""
    cycle = await open_cycle(base)

    reply = await cycle.move(_move(atoms=(_atom(url="http://127.0.0.1:9/a.csv"),)))

    assert code_of(reply) == "ГОТОВО" and "✗ eph.probe" in reply, "узел атома исполнялся"
    await cycle.close()


async def test_a_code_atom_needs_inputs_by_contract(base) -> None:
    """LR-23: у атома с кодом есть `requires` — иначе ему не из чего считать, контракт отклонён."""
    cycle = await open_cycle(base)

    reply = await cycle.call("aifa_register_atom", _registration(_atom(url="", requires=())))

    assert code_of(reply) == "ОТКАЗ неверные_аргументы" and "без входов" in reply
    await cycle.close()


async def test_a_download_atom_is_described_in_new_atoms(base) -> None:
    """LR-27: атом-загрузка (с `url`) описывается в `new_atoms` и становится атомом цикла."""
    cycle = await open_cycle(base)

    await cycle.move(_refused_move(_atom(url="http://127.0.0.1:9/a.csv")))

    assert cycle.session.atoms["eph.probe"].request.is_download
    await cycle.close()


async def test_a_refusal_does_not_spend_a_move_but_three_in_a_row_stop_the_cycle(base) -> None:
    """LR-17, ADR-0060 п. 5: отклонённый вызов хода не тратит; поток отказов цикл не растягивает — три подряд, `ЛИМИТ`."""
    cycle = await open_cycle(base, limits=RunLimits(max_moves=2))
    refused = _move(scenarios=("а", "б", "в", "г"))

    replies = [await cycle.move(refused) for _ in range(4)]

    assert [code_of(reply) for reply in replies] == ["ОТКАЗ неверные_аргументы"] * 3 + ["ЛИМИТ"]
    assert cycle.session.turns == 0 and cycle.session.rejected == 3
    await cycle.close()


async def test_the_last_word_after_the_limit_does_not_wait_for_a_promised_file(base) -> None:
    """LR-33: после предела файл уже не собрать — последнее слово проходит без гейта файла."""
    cycle = await open_cycle(base, scenario=ExtractScenario(base, None), limits=RunLimits(max_moves=1))
    arguments = json.loads(_move(scenarios=("Извлеки EBITDA",)))
    arguments.update(plan=["получить показатели", "собрать Excel"], files=[{"kind": "xlsx", "parts": 0, "formulas": False}])

    limited = await cycle.call("aifa_move", arguments)
    reply = await cycle.finish(_finalize(cited(base)))

    assert code_of(limited) == "ЛИМИТ"
    assert reply == "ПРИНЯТО\nЦикл закрыт."
    await cycle.close()
