"""Правила цикла через API инструментов (шаг 13): каждое правило из `aifa/contracts/loop_rules.yaml` — тест здесь.

Ведущий — клиент, играющий ouroboros; правила держит `LoopSession`, поэтому отказ приходит кодом §3.0, а не
текстом навыка (F8, ADR-0023). Тесты правил самого исполнителя (комната, загрузчик) остаются в `tests/dynamic_run/`.
"""
from __future__ import annotations

import json

from loop_support import make_passport, open_cycle
from test_atom_author import BAD_SCHEMA_CODE, Script
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

from finengine.contracts import NodeKey, VerificationFinding
from finengine.skills.spec.atom_author import AtomAuthor
from finengine.skills.spec.dynamic_run import RunLimits, artifact_content
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
    """LR-24: атом с кодом пишется, когда его входы уже в Case; иначе `нет_входов`, а не вызов автора."""
    cycle = await open_cycle(base)
    request = _atom(url="", requires=(InputSpec("ExtractedMetric", "EBITDA"),))

    reply = await cycle.move(_refused_move(request))

    assert code_of(reply) == "ОТКАЗ нет_входов" and "входов атома eph.probe" in reply
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
    """LR-31: обещанный планом файл не собран — `файл_не_собран`, пока цикл не потратил ходы с кодом атома."""
    cycle = await open_cycle(base, scenario=ExtractScenario(base, None))
    arguments = json.loads(_move(scenarios=("Извлеки EBITDA",)))
    arguments.update(plan=["получить показатели", "собрать Excel"], files=[{"kind": "xlsx", "parts": 0, "formulas": False}])
    await cycle.call("aifa_move", arguments)

    reply = await cycle.finish(_finalize(cited(base)))

    assert code_of(reply) == "ОТКАЗ файл_не_собран" and "xlsx" in reply
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

    reply = await cycle.finish(_finalize("no-such-id"))

    assert code_of(reply) == "ПРИНЯТО" and "итог не принят" in reply and "EBITDA" in reply
    assert cycle.session.closed
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


def _echo_author() -> AtomAuthor:
    return AtomAuthor(Script([json.dumps({"code": ECHO_CODE, "example_inputs": {}}, ensure_ascii=False)]))


def _echo_request() -> AtomRequest:
    return AtomRequest(
        name="eph.echo", purpose="что видит код", output_type="dyn:echo", output_schema=ECHO_SCHEMA,
        requires=(InputSpec("ExtractedMetric", "EBITDA"),),
    )


async def test_the_code_of_an_atom_sees_only_the_results_of_its_inputs(base, local_room) -> None:
    """LR-05: код атома не видит сырой документ — только результаты входов; текст отчёта до него не доходит."""
    passport = make_passport()
    cycle = await open_cycle(
        base, scenario=ExtractScenario(base, passport), author=_echo_author(), passport=passport,
        document_text="СЕКРЕТНЫЙ ТЕКСТ ОТЧЁТА",
    )
    request = _echo_request()

    await cycle.move(_move(scenarios=("Извлеки EBITDA",)))
    reply = await cycle.move(_move(atoms=(request,), steps=({"atom": request.name},)))

    seen = json.loads(_dynamic_content(base, "dyn:echo"))["payload"]["seen"]
    assert code_of(reply) == "ГОТОВО" and "ExtractedMetric" in seen
    assert "СЕКРЕТНЫЙ" not in seen
    await cycle.close()


async def test_the_result_of_an_atom_carries_its_version_and_the_origin_the_wrapper_computed(base, local_room) -> None:
    """LR-09, LR-11: результат — от конкретного кода (`eph.<имя>@<хэш контракта и кода>`), происхождение — из входов."""
    passport = make_passport()
    cycle = await open_cycle(
        base, scenario=ExtractScenario(base, passport), author=_echo_author(), passport=passport, document_text="отчёт",
    )
    request = _echo_request()

    await cycle.move(_move(scenarios=("Извлеки EBITDA",)))
    await cycle.move(_move(atoms=(request,), steps=({"atom": request.name},)))

    content = json.loads(_dynamic_content(base, "dyn:echo"))
    assert content["producer"] == f"eph.echo@{EphemeralAtom(request, ECHO_CODE).version}"
    assert content["origin"], "происхождение считает обёртка, не код атома"
    await cycle.close()


async def test_a_result_of_the_wrong_shape_is_a_failed_attempt_and_no_atom(base, local_room) -> None:
    """LR-10: результат обязан соответствовать `output_schema`; код автора, не прошедший проверку, атомом не становится."""
    wrong = json.dumps({"code": BAD_SCHEMA_CODE, "example_inputs": {}}, ensure_ascii=False)
    cycle = await open_cycle(base, scenario=ExtractScenario(base, None), author=AtomAuthor(Script([wrong] * 3)))
    request = AtomRequest(
        name="eph.margin_with_brent", purpose="маржа", output_type="dyn:margin_with_brent", output_schema=CALC_SCHEMA,
        requires=(InputSpec("ExtractedMetric", "EBITDA"), InputSpec("ExtractedMetric", "REVENUE")),
    )

    reply = await cycle.move(_move(scenarios=("Извлеки EBITDA и выручку",), atoms=(request,), steps=({"atom": request.name},)))

    assert code_of(reply) == "ГОТОВО" and "output_schema" in reply
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

    reply = await cycle.move(_refused_move(_atom(url="", requires=())))

    assert code_of(reply) == "ОТКАЗ неверные_аргументы" and "без входов" in reply
    await cycle.close()


async def test_a_download_atom_is_described_in_new_atoms(base) -> None:
    """LR-27: атом-загрузка (с `url`) описывается в `new_atoms` и становится атомом цикла."""
    cycle = await open_cycle(base)

    await cycle.move(_refused_move(_atom(url="http://127.0.0.1:9/a.csv")))

    assert cycle.session.atoms["eph.probe"].request.is_download
    await cycle.close()
