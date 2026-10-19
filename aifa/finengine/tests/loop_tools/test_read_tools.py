"""Чтение цикла: `aifa_state` (состояние и артефакты) и `aifa_doc` (текст документов хода), C3.6, ADR-0040 п. 2.

Читают только Case и документы этого хода; ответ не длиннее `result_limit`, остаток — по просьбе.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from loop_support import open_cycle
from test_dynamic_run import ExtractScenario, _move
from test_ephemeral_in_graph import CASE, OWNER

from finengine.assistant import read_tools
from finengine.contracts import DynamicArtifact, NodeKey

LIMIT = 12_000
DOC = "doc-1"
PAGES = {
    1: "Титульный лист отчёта ЛУКОЙЛ.",
    2: "Выручка за 2024 год составила 8 000 млрд руб.",
    3: "Приложение: EBITDA за 2024 год — 1 200 млрд руб.",
}
TEXT = "".join(f"===PAGE {number}===\n{text}\n" for number, text in PAGES.items())


def code_of(reply: str) -> str:
    return reply.split("\n", 1)[0]


@pytest.fixture
def documents(monkeypatch: pytest.MonkeyPatch) -> None:
    texts = {DOC: TEXT, "doc-long": "".join(f"===PAGE {n}===\n{'слово ' * 700}\n" for n in range(1, 6))}
    monkeypatch.setattr(read_tools, "document_text", lambda doc_id, **_: texts.get(doc_id))
    monkeypatch.setattr(
        read_tools, "get_store", lambda: SimpleNamespace(get_document=lambda doc_id: SimpleNamespace(company_name="ЛУКОЙЛ"))
    )


def _put_dynamic(base, name: str, payload: dict, owner: str = OWNER) -> str:
    artifact = DynamicArtifact(
        output_type=f"dyn:{name}", payload=payload, origin=["generated"], producer=f"eph.{name}@1",
        input_evidence_ids=[],
    )
    base.ensure_case(owner_id=owner, case_id=CASE)
    base.store.put_artifact(
        owner, CASE, node_key=NodeKey(atom_name=f"eph.{name}", atom_version="1", args_fingerprint=name),
        output_type=artifact.output_type, payloads=(artifact,), concept=None, entity_ref=None, period_ref=None,
        basis=None, document_version=None, policy_version="test",
    )
    return artifact.evidence_id


async def test_the_state_shows_the_loop_the_way_the_planner_sees_it(base) -> None:
    """§3.1: состояние цикла — вопрос, цель, план, каталог, артефакты со значениями, журнал, остаток."""
    cycle = await open_cycle(base, scenario=ExtractScenario(base, None))
    await cycle.move(_move(scenarios=("Извлеки EBITDA и выручку",)))

    state = await cycle.call("aifa_state")

    assert code_of(state) == "ГОТОВО"
    for section in ("Вопрос пользователя", "Разобранная цель", "Каталог атомов", "Журнал ходов", "Осталось"):
        assert f"## {section}" in state
    assert "EBITDA" in state and "500.0" in state, "значения артефактов — в таблице"
    assert len(state) <= LIMIT
    await cycle.close()


async def test_a_state_that_does_not_fit_gives_the_table_the_room_and_names_the_rest(base) -> None:
    """§3.0: длинное — частями; остаток таблицы читается `aifa_state(artifact_ids)`."""
    for index in range(80):
        _put_dynamic(base, f"row{index}", {"text": "данные " * 40, "index": index})
    cycle = await open_cycle(base)

    state = await cycle.call("aifa_state")

    assert code_of(state) == "ГОТОВО" and len(state) <= LIMIT
    assert "Остальные — aifa_state с artifact_ids" in state
    await cycle.close()


async def test_artifacts_by_id_come_whole(base) -> None:
    """§3.1: `aifa_state(artifact_ids)` — полное содержимое: значения, единицы, периоды, источник."""
    cycle = await open_cycle(base, scenario=ExtractScenario(base, None))
    await cycle.move(_move(scenarios=("Извлеки EBITDA и выручку",)))
    metric = next(
        payload for row in base.store.list_artifacts(OWNER, CASE) for payload in row["payloads"]
        if getattr(payload, "concept", None) == "EBITDA"
    )

    reply = await cycle.call("aifa_state", {"artifact_ids": [metric.scalar_fact.evidence_id]})

    assert code_of(reply) == "ГОТОВО" and "### " + metric.scalar_fact.evidence_id in reply
    assert '"value":500.0' in reply.replace(" ", "") and "RUB" in reply.upper() or "unit" in reply
    await cycle.close()


async def test_only_the_case_of_this_turn_is_readable(base) -> None:
    """LR-35, ADR-0040 п. 2: артефакт другого владельца по id не читается, как и несуществующий."""
    foreign = _put_dynamic(base, "foreign", {"secret": "чужое"}, owner="someone-else")
    cycle = await open_cycle(base)

    reply = await cycle.call("aifa_state", {"artifact_ids": [foreign, "нет-такого"]})

    assert code_of(reply) == "ОТКАЗ неверные_аргументы" and "чужое" not in reply
    await cycle.close()


@pytest.mark.parametrize("ids", [["x"] * 21, [""], "x", [1]])
async def test_the_list_of_ids_is_bounded_and_made_of_ids(base, ids) -> None:
    cycle = await open_cycle(base)

    assert code_of(await cycle.call("aifa_state", {"artifact_ids": ids})) == "ОТКАЗ неверные_аргументы"
    await cycle.close()


async def test_documents_of_the_turn_are_listed_with_their_pages(base, documents) -> None:
    """LR-36, C3.6: без аргументов — документы хода (id, имя, число страниц)."""
    cycle = await open_cycle(base, doc_ids=(DOC,))

    reply = await cycle.call("aifa_doc")

    assert code_of(reply) == "ГОТОВО" and f"{DOC} | ЛУКОЙЛ | страниц: 3" in reply
    await cycle.close()


async def test_the_text_of_a_page_range_is_recognized_text(base, documents) -> None:
    """C3.6: `doc_id` + `pages` — распознанный текст этих страниц, остальных нет."""
    cycle = await open_cycle(base, doc_ids=(DOC,))

    reply = await cycle.call("aifa_doc", {"doc_id": DOC, "pages": "2"})

    assert code_of(reply) == "ГОТОВО" and PAGES[2] in reply
    assert PAGES[1] not in reply and PAGES[3] not in reply
    await cycle.close()


async def test_a_search_finds_places_with_their_pages(base, documents) -> None:
    """C3.6: `doc_id` + `query` — страница и фрагмент каждого найденного места."""
    cycle = await open_cycle(base, doc_ids=(DOC,))

    reply = await cycle.call("aifa_doc", {"doc_id": DOC, "query": "ebitda"})

    assert "стр. 3:" in reply and "стр. 2:" not in reply
    await cycle.close()


async def test_a_document_of_another_turn_is_not_readable(base, documents) -> None:
    """LR-37, C3.6: чужой или неизвестный `doc_id` — `ОТКАЗ нет_документа`."""
    cycle = await open_cycle(base, doc_ids=(DOC,))

    assert code_of(await cycle.call("aifa_doc", {"doc_id": "doc-long"})) == "ОТКАЗ нет_документа"
    assert code_of(await cycle.call("aifa_doc", {"doc_id": "нет"})) == "ОТКАЗ нет_документа"
    await cycle.close()


async def test_a_long_text_comes_in_parts_with_the_next_page_named(base, documents) -> None:
    """LR-38, C3.6: ответ длиннее предела — частями, с номером страницы, с которой просить дальше."""
    cycle = await open_cycle(base, doc_ids=("doc-long",))

    reply = await cycle.call("aifa_doc", {"doc_id": "doc-long", "pages": "1-5"})

    assert code_of(reply) == "ГОТОВО" and len(reply) <= LIMIT
    assert "Следующие: aifa_doc doc_id=doc-long pages=" in reply
    await cycle.close()


@pytest.mark.parametrize("args", [{"pages": "1"}, {"doc_id": DOC, "pages": "abc"}, {"doc_id": DOC, "pages": "9"}])
async def test_a_wrong_request_is_a_refusal_with_the_way_out(base, documents, args) -> None:
    cycle = await open_cycle(base, doc_ids=(DOC,))

    assert code_of(await cycle.call("aifa_doc", args)) == "ОТКАЗ неверные_аргументы"
    await cycle.close()
