"""Реплей решений планировщика через ouroboros с навыком `aifa-planning` (шаг 14, ADR-0044 п. 7).

Записанные решения идут вызовами `aifa_move`/`aifa_finish` через живой ouroboros профиля `contract`.
Проверяются все ходы и получение показателя; эквивалентность с `DynamicRun` держат тесты службы.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pipeline_fixture as fixture
import pytest
from engine_host import running_engine
from replay_support import (
    SKILL_DIR,
    arguments_of,
    normalize_turn,
    replays,
    scenario_of,
    skill_reads,
    tool_calls_of,
    without_nulls,
)
from test_chat_turn_end_to_end import kind_of, screen_events, start_turn

from stand import ContractStand
from stub_model.client import StubModelAdmin

QUESTION = "какая выручка за 2024 год?"
CUSTOM_INTENT = {
    "route": "plan",
    "intent_kind": "custom_analysis",
    "targets": [],
    "entity_ref": None,
    "period_ref": None,
    "role": None,
    "unresolved": ["сложный вопрос"],
    "ambiguities": [],
}
FINAL = "Выручка за 2024 год — 987654 млн руб. (по таблице результатов цикла)."
# Лента планировщика на экране; вызовы инструментов, текст и сессия по построению разные.
CYCLE_KINDS = {"dynamic_move", "plan_graph", "node_run", "plan_step", "typed_result", "typed_error"}
LABEL = re.compile(r"<id:\d+>")
ALL_REPLAYS = pytest.mark.parametrize("replay", replays(), ids=[item["name"] for item in replays()])


@contextmanager
def service(
    stand: ContractStand,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[str]:
    """Служба с фикстурным конвейером и запретом вызова собственного планировщика."""
    from finengine.config import settings

    tmp_path.mkdir(parents=True, exist_ok=True)
    replies = iter([CUSTOM_INTENT])

    async def parsed(system: str, prompt: str, *, model_version: Any = None) -> str:
        return json.dumps(next(replies, fixture.INTENT), ensure_ascii=False)

    async def planner_of_the_service(system: str, prompt: str) -> str:
        raise AssertionError("модель-планировщик службы вызвана в цикле агента")

    with running_engine(stand, tmp_path, monkeypatch) as base:
        monkeypatch.setattr(settings, "data_dir", tmp_path)
        monkeypatch.setattr("finengine.skills.atoms.intent._default_complete", parsed)
        monkeypatch.setattr(
            "finengine.skills.spec.dynamic_run._default_complete",
            planner_of_the_service,
        )
        yield base


async def run_turn(
    base: str, stub: StubModelAdmin, scenario: dict[str, Any]
) -> list[dict[str, Any]]:
    stub.clear()
    stub.use_scenario(scenario)
    return await screen_events(base, await start_turn(base, QUESTION))


def cycle_of(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """События ленты цикла в виде, который видит экран (нормализатор F5).

    Метки id нумеруются по порядку появления: два независимых узла одного ключа, завершившиеся
    в другом порядке, получили бы разные номера: сравнение идёт без номеров (ревью 14, В3)."""
    turn = normalize_turn(events)
    cycle = [_blank_final_words(event) for event in turn.events if event.get("type") in CYCLE_KINDS]
    masked: list[dict[str, Any]] = json.loads(
        LABEL.sub("<id>", json.dumps(cycle, ensure_ascii=False))
    )
    return masked


def _blank_final_words(event: dict[str, Any]) -> dict[str, Any]:
    """ADR-0042: у `aifa_finish` нет цели и причины, `details` у ведущих одни."""
    if event.get("type") == "dynamic_move" and event.get("action") in ("finalize", "give_up"):
        return {**event, **{"objective": "", "reason": ""}}
    return event


def moves_of(events: list[dict[str, Any]]) -> list[tuple[str, str]]:
    return [
        (e["legacy_event"]["status"], e["legacy_event"]["action"])
        for e in events
        if kind_of(e) == "dynamic_move"
    ]


@ALL_REPLAYS
@pytest.mark.contract
@pytest.mark.asyncio
async def test_recorded_decisions_give_the_same_moves_and_events_when_ouroboros_leads(
    replay: dict[str, Any],
    stand: ContractStand,
    stub: StubModelAdmin,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Реплей исполняет каждый ход и получает выручку до завершения цикла."""
    decisions = replay["decisions"]
    by_ouroboros = scenario_of(
        replay["name"],
        QUESTION,
        [*skill_reads("references/moves.md"), ("aifa_open", {}), *tool_calls_of(decisions)],
        FINAL,
    )
    with service(stand, tmp_path, monkeypatch) as base:
        actual = await run_turn(base, stub, by_ouroboros)

    assert stub.unmatched() == []
    assert actual[-1]["status"] == "succeeded"
    expected_moves = [
        event
        for decision in decisions
        for event in (
            [("running", "execute"), ("done", "execute")]
            if decision["action"] == "execute"
            else [("done", decision["action"])]
        )
    ]
    assert moves_of(actual) == expected_moves
    asked = [entry for entry in stub.journal() if entry["model"] == "stub-main"]
    results = {entry["round"] - 1: entry["last_tool_result"] for entry in asked}
    calls = [*skill_reads("references/moves.md"), ("aifa_open", {}), *tool_calls_of(decisions)]
    for number, (tool, args) in enumerate(calls, start=1):
        if tool == "aifa_move" and args.get("goals"):
            assert results[number].startswith("ГОТОВО") and "✓" in results[number]
    assert any(
        "987654" in reply.replace(" ", "").replace("\u00a0", "") for reply in results.values()
    )
    terminal = [m for m in moves_of(actual) if m[1] in ("finalize", "give_up")]
    assert terminal and terminal[-1][1] == decisions[-1]["action"]


LIVE = json.loads(
    (
        Path(__file__).resolve().parents[2]
        / "finengine"
        / "tests"
        / "dynamic_run"
        / "fixtures"
        / "live_run_replays.json"
    ).read_text(encoding="utf-8")
)
SNAPSHOT = json.loads(
    (Path(__file__).resolve().parents[2] / "contracts" / "tools.snapshot.json").read_text(
        encoding="utf-8"
    )
)
DECISIONS = [
    pytest.param(response, id=f"{recording['run']}-{index}")
    for recording in LIVE
    for index, response in enumerate(recording["responses"])
] + [
    pytest.param(decision, id=f"{item['name']}-{index}")
    for item in replays()
    for index, decision in enumerate(item["decisions"])
]


@pytest.mark.parametrize("decision", DECISIONS)
def test_a_recorded_decision_becomes_tool_arguments_the_service_reads_as_the_same_decision(
    decision: dict[str, Any],
) -> None:
    """Перевод «решение → вызов» ничего не теряет: из аргументов служба собирает то же решение."""
    from finengine.skills.spec.dynamic_run import decision_from_data, parse_decision

    tool, args = arguments_of(decision)
    migrated = {**decision, **args, "scenarios": []}
    terminal = decision["action"] != "execute"
    expected = parse_decision(
        json.dumps(migrated, ensure_ascii=False), scenarios_enabled=False, goals_enabled=True
    )
    if terminal:
        data = {
            "objective": "o",
            "reason": "r",
            "action": args["action"],
            **{k: v for k, v in args.items() if k != "action"},
        }
    else:
        data = {**args, "action": "execute"}
    actual = decision_from_data(
        data, scenarios_enabled=False, goals_enabled=True, answer_in_chat=terminal
    )

    assert tool == ("aifa_finish" if terminal else "aifa_move")
    assert set(args) <= set(SNAPSHOT["tools"][tool]["arguments"]), (
        "аргументы только из снимка схем (F4)"
    )
    # `null` в записи — «поля нет»: в вызов он не попадает, на смысл шага не влияет.
    assert actual.raw_steps == tuple(without_nulls(step) for step in expected.raw_steps)
    assert [
        (
            clause.goal_ref,
            clause.intent_kind,
            [target.model_dump(exclude={"target_id"}) for target in clause.targets],
            clause.period_ref,
            clause.role,
            clause.depends_on,
            clause.unresolved,
        )
        for clause in actual.new_goals
    ] == [
        (
            clause.goal_ref,
            clause.intent_kind,
            [target.model_dump(exclude={"target_id"}) for target in clause.targets],
            clause.period_ref,
            clause.role,
            clause.depends_on,
            clause.unresolved,
        )
        for clause in expected.new_goals
    ]
    assert (
        actual.action,
        actual.scenarios,
        actual.goals,
        actual.new_atoms,
        actual.plan,
        actual.files,
    ) == (
        expected.action,
        expected.scenarios,
        expected.goals,
        expected.new_atoms,
        expected.plan,
        expected.files,
    )
    assert (actual.evidence_ids, actual.deliverables) == (
        expected.evidence_ids,
        expected.deliverables,
    )
    if not terminal:
        assert (actual.objective, actual.reason) == (expected.objective, expected.reason)


@pytest.fixture
def engine(stand: ContractStand, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    with service(stand, tmp_path, monkeypatch) as base:
        yield base


REFERENCES = (
    "references/moves.md",
    "references/external-data.md",
    "references/new-atom.md",
    "references/atoms.md",
    "references/metrics.md",
    "references/codes.md",
)


@pytest.mark.contract
@pytest.mark.asyncio
async def test_ouroboros_reads_the_skill_and_every_reference_it_names(
    engine: str, stub: StubModelAdmin
) -> None:
    """Навык ↔ ouroboros: `skill_read` отдаёт путеводитель и справочники пакета целиком."""
    scenario = scenario_of("read-the-skill", QUESTION, skill_reads(*REFERENCES), "Навык прочитан.")

    events = await run_turn(engine, stub, scenario)

    asked = [e for e in stub.journal() if e["model"] == "stub-main"]
    assert stub.unmatched() == [] and events[-1]["status"] == "succeeded"
    assert "Правила корневой задачи" in json.dumps(asked[1]["messages"], ensure_ascii=False), (
        "путеводитель в защищённом разделе"
    )
    for round_no, name in enumerate(REFERENCES, start=3):
        text = (SKILL_DIR / name).read_text(encoding="utf-8").strip()
        shown = asked[round_no - 1]["last_tool_result"]
        assert text[:120] in shown and text[-120:] in shown, f"{name}: ouroboros отдал не весь файл"


PAGES = 60
NEEDLE_PAGE = 41


def long_document() -> str:
    """60 страниц OCR-разметки: первая — выдержка отчёта, в 41-й — искомое место."""
    pages = [f"===PAGE 1===\n{fixture.document_text()}\n"]
    for number in range(2, PAGES + 1):
        body = f"Страница {number}. " + " ".join(
            f"Строка {line} примечаний к отчётности, сумма {line * number} тыс. руб."
            for line in range(1, 60)
        )
        if number == NEEDLE_PAGE:
            body += " Условное обязательство по иску ООО «Ромашка» — 12 345 тыс. руб."
        pages.append(f"===PAGE {number}===\n{body}\n")
    return "".join(pages)


@pytest.mark.contract
@pytest.mark.asyncio
async def test_a_long_document_is_read_by_search_and_pages_and_never_whole(
    engine: str,
    stub: StubModelAdmin,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ADR-0040 п. 2, C3.6: `aifa_doc` — список, поиск, страницы; «все» страницы — частью."""
    from finengine.assistant import read_tools

    text = long_document()
    monkeypatch.setattr(
        read_tools, "document_text", lambda doc_id, **_: text if doc_id == fixture.DOC else None
    )
    doc = {"doc_id": fixture.DOC}
    calls = [
        *skill_reads("references/moves.md"),
        ("aifa_open", {}),
        ("aifa_doc", {}),
        ("aifa_doc", {**doc, "query": "Ромашка"}),
        ("aifa_doc", {**doc, "pages": f"{NEEDLE_PAGE - 1}-{NEEDLE_PAGE + 1}"}),
        ("aifa_doc", {**doc, "pages": f"1-{PAGES}"}),
        ("aifa_finish", {"action": "give_up"}),
    ]

    events = await run_turn(
        engine, stub, scenario_of("long-document", QUESTION, calls, "Документ прочитан частями.")
    )

    asked = [e for e in stub.journal() if e["model"] == "stub-main"]
    results = {entry["round"]: entry["last_tool_result"] for entry in asked}
    assert stub.unmatched() == [] and events[-1]["status"] == "succeeded"
    assert results[4].startswith("ГОТОВО")
    assert f"{fixture.DOC} | Северсталь | страниц:" in results[5]
    assert (
        results[6].startswith("ГОТОВО")
        and f"стр. {NEEDLE_PAGE}:" in results[6]
        and "Ромашка" in results[6]
    )
    assert (
        f"===PAGE {NEEDLE_PAGE}===" in results[7] and f"===PAGE {NEEDLE_PAGE - 1}===" in results[7]
    )
    whole = results[8]
    assert len(whole) <= 12_000 < len(text), "ответ не больше предела, документ — больше"
    assert "Следующие: aifa_doc" in whole and f"===PAGE {PAGES}===" not in whole
    # Контекст модели не переполнен: ни один результат инструмента не вышел за предел ответа.
    assert all(len(result) <= 12_000 for result in results.values() if result)


FIRST_ID = "11111111-1111-1111-1111-111111111111"
SECOND_ID = "22222222-2222-2222-2222-222222222222"


def verified_node(evidence: str) -> dict[str, Any]:
    return {
        "event_type": "node_run",
        "plan_graph_hash": "g",
        "node_run": {
            "atom": "verify.bounds",
            "status": "done",
            "produced_evidence_ids": [evidence],
        },
    }


def test_the_order_in_which_independent_nodes_finish_does_not_change_the_compared_cycle() -> None:
    """Ревью 14, В3: id названы раньше узлов, узлы одного ключа завершились в другом порядке."""
    named = {
        "event_type": "legacy",
        "legacy_event": {
            "type": "dynamic_move",
            "status": "done",
            "action": "execute",
            "details": f"{FIRST_ID} {SECOND_ID}",
        },
    }
    first = [named, verified_node(FIRST_ID), verified_node(SECOND_ID)]
    second = [named, verified_node(SECOND_ID), verified_node(FIRST_ID)]

    assert normalize_turn(first).events != normalize_turn(second).events, (
        "иначе тест ничего не ловит"
    )
    assert cycle_of(first) == cycle_of(second)
