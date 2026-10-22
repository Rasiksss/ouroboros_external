"""`aifa_atoms`: каталог атомов для агента — список карточек и контракт с исходным кодом (ADR-0064).

Читающий инструмент вне очереди: карточки собираются из реестра цикла (поставляемые атомы) и из атомов этого `Case`
(временные); отдельной системы каталогизации нет.
"""
from __future__ import annotations

import inspect

import pytest
from dynamic_support import MARGIN_CODE
from loop_support import open_cycle
from test_register_atom import NAME, cycle_with_inputs, register_args

from finengine.config import settings

LIMIT = 12_000


def code_of(reply: str) -> str:
    return reply.split("\n", 1)[0]


def card_of(reply: str, name: str) -> str:
    """Карточка атома из ответа `list`: от заголовка `### name` до следующего."""
    return reply.split(f"### {name}\n", 1)[1].split("\n### ", 1)[0]


async def listing(cycle, **args) -> str:
    return await cycle.call("aifa_atoms", {"action": "list", **args})


async def whole_listing(cycle) -> str:
    """Все страницы `list` одной строкой: каталог не обязан помещаться в один ответ."""
    pages, offset = [], 0
    while True:
        page = await listing(cycle, offset=offset)
        pages.append(page)
        following = page.rsplit("offset=", 1)[1].split()[0] if "Следующие:" in page else None
        if following is None:
            return "\n".join(pages)
        offset = int(following)


async def test_the_list_shows_cards_without_source_code(base) -> None:
    """LR-62, ADR-0064 п. 1: `list` — реестр с описаниями и контрактами; исходного кода в нём нет."""
    cycle = await open_cycle(base)

    reply = await listing(cycle)

    assert code_of(reply) == "ГОТОВО" and len(reply) <= LIMIT
    card = card_of(reply, "out.file")
    for field in ("Имя: out.file", "Версия: ", "Назначение: ", "Входы: ", "Выходы: FileArtifact", "Форматы: xlsx, pdf, csv, json"):
        assert field in card
    assert "Ограничения: " in card and "def " not in reply
    await cycle.close()


async def test_the_card_states_what_the_atom_cannot_do(base) -> None:
    """ADR-0064 п. 3: ограничения сопровождают технические поля — по ним агент решает, хватает ли атома."""
    cycle = await open_cycle(base)

    card = card_of(await listing(cycle), "out.file")

    limits = card.split("Ограничения: ", 1)[1].lower()
    assert "один лист" in limits and "презентац" in limits
    await cycle.close()


async def test_gates_and_infrastructure_are_not_cards(base) -> None:
    """LR-65: как в `catalog_text` — гейты достраивает планировщик, в шаги агента они не входят."""
    cycle = await open_cycle(base)

    reply = await whole_listing(cycle)

    assert "### norm.unit" not in reply and "### verify.bounds" not in reply and "### ingest.document" not in reply
    assert "### read.value" in reply and "### calc.formula" in reply
    await cycle.close()


async def test_a_long_list_is_paged_and_every_atom_is_reachable(base) -> None:
    """§3.0: ответ ≤ `result_limit`; остаток — `offset`, как у `artifact_index`."""
    cycle = await open_cycle(base)

    first = await listing(cycle)
    everything = await whole_listing(cycle)

    assert len(first) <= LIMIT
    names = [line.removeprefix("### ") for line in everything.splitlines() if line.startswith("### ")]
    assert len(names) == len(set(names)) > 20, "каждый атом — ровно в одной карточке"
    await cycle.close()


async def test_the_list_shows_the_temporary_atoms_of_this_case(base, local_room) -> None:
    """LR-66, ADR-0064 п. 2: временные атомы Case — в списке, с пометкой области; чужого и несданного нет."""
    cycle = await cycle_with_inputs(base)
    assert NAME not in await whole_listing(cycle)
    await cycle.call("aifa_register_atom", register_args(), depth=1)

    reply = await whole_listing(cycle)

    card = card_of(reply, NAME)
    assert f"Версия: {cycle.session.atoms[NAME].version}" in card
    assert "Область: временный, этот Case" in card and "Выходы: dyn:ebitda_margin" in card
    assert "def run" not in reply
    await cycle.close()


async def test_get_returns_the_contract_the_exact_version_and_the_source(base) -> None:
    """LR-63, ADR-0064 п. 1: `get` — контракт и исходный код атома, версия указана точно."""
    cycle = await open_cycle(base)
    atom = cycle.session.runtime.registry.get("out.file")

    reply = await cycle.call("aifa_atoms", {"action": "get", "name": "out.file"})

    assert code_of(reply) == "ГОТОВО" and len(reply) <= LIMIT
    assert f"out.file@{atom.version}" in reply and "Форматы: xlsx, pdf, csv, json" in reply
    assert "def _executor(args, ctx)" in reply and "render_typed_result(source, file_kind)" in reply
    await cycle.close()


async def test_the_source_of_a_built_in_atom_names_its_helpers_and_is_not_passed_off_as_whole(base) -> None:
    """LR-64, ADR-0064 п. 1: фрагмент не выглядит полной реализацией — вспомогательные функции названы ссылками."""
    cycle = await open_cycle(base)

    reply = await cycle.call("aifa_atoms", {"action": "get", "name": "out.file"})

    assert "Вспомогательные функции" in reply and "код не приведён" in reply
    assert "finengine.skills.atoms.out_file.render_typed_result" in reply
    assert "finengine.skills.atoms.out_file.build_file_artifact" in reply
    await cycle.close()


async def test_get_of_a_temporary_atom_gives_its_code_and_schema(base, local_room) -> None:
    """ADR-0064 п. 4: кодовый автор видит реализацию существующего атома Case."""
    cycle = await cycle_with_inputs(base)
    await cycle.call("aifa_register_atom", register_args(), depth=1)

    reply = await cycle.call("aifa_atoms", {"action": "get", "name": NAME}, depth=1)

    assert code_of(reply) == "ГОТОВО"
    assert f"{NAME}@{cycle.session.atoms[NAME].version}" in reply
    assert MARGIN_CODE.strip() in reply and "output_schema" in reply and "x-unit" in reply
    await cycle.close()


async def test_a_long_source_comes_in_parts_with_the_next_line_named(base, monkeypatch) -> None:
    """LR-67, §3.0: длинный исходник частями, `offset` — номер строки, с которой продолжить; части вместе — весь код."""
    monkeypatch.setattr(settings, "ouroboros_result_limit", 2_500)
    cycle = await open_cycle(base)
    whole = inspect.getsource(cycle.session.runtime.registry.get("out.file").executor)

    parts, offset = [], 0
    while True:
        reply = await cycle.call("aifa_atoms", {"action": "get", "name": "out.file", "offset": offset})
        assert code_of(reply) == "ГОТОВО" and len(reply) <= 2_500
        parts.append(reply)
        if "Продолжение: aifa_atoms" not in reply:
            break
        following = int(reply.rsplit("offset=", 1)[1].split()[0])
        assert following > offset
        offset = following

    assert len(parts) > 1, "исходник не поместился в один ответ"
    joined = "\n".join(parts)
    for line in whole.splitlines():
        assert line.strip() == "" or line.strip() in joined
    await cycle.close()


@pytest.mark.parametrize(
    "args",
    [
        {},
        {"action": "drop"},
        {"action": "get"},
        {"action": "get", "name": "no.such_atom"},
        {"action": "get", "name": "norm.unit"},
        {"action": "list", "name": "out.file"},
        {"action": "list", "offset": -1},
    ],
)
async def test_a_wrong_call_is_refused_by_the_rules_of_the_tool(base, args) -> None:
    """LR-69: действие, `name`, `offset` и неизвестный атом — `ОТКАЗ неверные_аргументы`."""
    cycle = await open_cycle(base)

    reply = await cycle.call("aifa_atoms", args)

    assert code_of(reply) == "ОТКАЗ неверные_аргументы"
    await cycle.close()


async def test_a_stale_version_is_refused_with_the_current_one(base) -> None:
    """LR-69: версия, которой уже нет, не отдаётся под видом актуальной — ответ называет актуальную."""
    cycle = await open_cycle(base)
    current = cycle.session.runtime.registry.get("out.file").version

    exact = await cycle.call("aifa_atoms", {"action": "get", "name": f"out.file@{current}"})
    stale = await cycle.call("aifa_atoms", {"action": "get", "name": "out.file@0.0.1"})

    assert code_of(exact) == "ГОТОВО"
    assert code_of(stale) == "ОТКАЗ неверные_аргументы" and f"out.file@{current}" in stale
    await cycle.close()


async def test_a_refusal_for_an_unknown_atom_points_to_the_list(base) -> None:
    cycle = await open_cycle(base)

    reply = await cycle.call("aifa_atoms", {"action": "get", "name": "out.excel"})

    assert 'action="list"' in reply
    await cycle.close()


async def test_the_planner_the_root_and_the_author_all_read_the_catalog(base) -> None:
    """LR-68, ADR-0064 п. 3: инструмент не только для корня — подзадача читает тот же каталог."""
    cycle = await open_cycle(base)

    by_root = await listing(cycle)
    by_subtask = await cycle.call("aifa_atoms", {"action": "list"}, depth=1)

    assert code_of(by_root) == code_of(by_subtask) == "ГОТОВО"
    assert by_root == by_subtask
    await cycle.close()


async def test_the_catalog_is_not_readable_before_the_loop_is_opened(base, tmp_path) -> None:
    """LR-61: каталог читается из цикла хода — без `aifa_open` `нет_цикла`, как у `aifa_state`."""
    from loop_support import Gateway, _NoGoal
    from tool_harness import Plugin, ctx_of, open_run

    from finengine.assistant.loop_desk import LoopDesk
    from finengine.assistant.tool_api import ToolApi
    from test_ephemeral_in_graph import CASE, OWNER

    run = open_run(user_id=OWNER, case_id=CASE, doc_ids=(), message="вопрос")
    plugin = Plugin(ToolApi(Gateway(), _NoGoal(), LoopDesk()))

    reply = await plugin.call("aifa_atoms", ctx_of(run.run_id), {"action": "list"})

    assert code_of(reply.text) == "ОТКАЗ нет_цикла"
    await plugin.close()
    await run.end()


async def test_a_name_with_stray_spaces_still_finds_the_atom(base) -> None:
    """Отзыв ревью: пробел вокруг имени (и версии) не превращает существующий атом в «нет в каталоге»."""
    cycle = await open_cycle(base)
    current = cycle.session.runtime.registry.get("out.file").version

    padded = await cycle.call("aifa_atoms", {"action": "get", "name": " out.file "})
    versioned = await cycle.call("aifa_atoms", {"action": "get", "name": f" out.file @ {current} "})

    assert code_of(padded) == code_of(versioned) == "ГОТОВО"
    await cycle.close()


def test_a_huge_contract_is_read_in_parts_and_never_cut_away() -> None:
    """Отзыв ревью: контракт временного атома отдаётся целиком — длинный читается частями по `offset`, как и код."""
    from finengine.assistant import ouro_view

    contract = ["output_schema: {"] + [f' "field_{n}": {{"type": "number", "x-unit": "RUB"}},' for n in range(900)] + ["}"]
    code = ["def run(inputs):", "    return {'a': 1}"]
    sections = [("Контракт", "", contract), ("Исходный код", "python", code)]
    pages, offset = [], 0
    while True:
        reply = ouro_view.atom_detail(
            "eph.big@1", "Имя: eph.big", "ссылки " * 5_000, sections, name="eph.big", offset=offset, limit=LIMIT,
        )
        assert len(reply) <= LIMIT and "[строка обрезана]" not in reply
        pages.append(reply)
        if "Продолжение: aifa_atoms" not in reply:
            break
        offset = int(reply.rsplit("offset=", 1)[1].split()[0])

    joined = "\n".join(pages)
    assert len(pages) > 1 and "field_899" in joined and "def run(inputs):\n    return {'a': 1}" in joined


def test_a_card_that_does_not_fit_says_it_was_cut() -> None:
    from finengine.assistant import ouro_view

    reply = ouro_view.atoms_list([("a", "x" * 5_000), ("b", "y")], offset=0, header="h", limit=1_200)

    assert "[карточка обрезана" in reply and "offset=1" in reply


def test_helpers_used_only_inside_a_nested_function_are_still_named() -> None:
    """Отзыв ревью: ссылки ищутся и во вложенных функциях, а пустой список не выдаётся за «вся реализация»."""
    from finengine.skills.atoms.out_file import render_typed_result
    from finengine.skills.spec import atom_cards
    from finengine.skills.spec.atom_registry import AtomDef

    def make():
        def _executor(args, ctx):
            def inner(source):
                return render_typed_result(source, "csv")

            return inner(args)

        return _executor

    def bare(args, ctx):
        return []

    def atom_with(executor):
        return AtomDef(
            name="x.nested", version="1", produces=("T",), sandbox_profile="compute_no_network",
            execution_kind="deterministic", cache_policy="none", cost_class="free", executor=executor,
        )

    named = atom_cards._built_in_source(atom_with(make()))
    silent = atom_cards._built_in_source(atom_with(bare))

    assert "finengine.skills.atoms.out_file.render_typed_result" in named.helpers
    assert silent.helpers == () and "вся реализация" not in silent.helpers_text()
    assert "неполным" in silent.helpers_text()


async def test_the_list_accepts_an_empty_name_a_model_may_add(base) -> None:
    """Отзыв ревью: пустое `name` у `list` — не причина отказа в чтении каталога."""
    cycle = await open_cycle(base)

    reply = await cycle.call("aifa_atoms", {"action": "list", "name": ""})

    assert code_of(reply) == "ГОТОВО"
    await cycle.close()


def test_an_executor_that_is_not_a_whole_function_still_gets_a_card_source() -> None:
    """Отзыв ревью: лямбда внутри вызова не разбирается как модуль — `get` отвечает без ссылок и с пометкой, а не падает."""
    from finengine.skills.spec import atom_cards
    from finengine.skills.spec.atom_registry import AtomDef

    atom = AtomDef(
        name="x.lambda", version="1", produces=("T",), sandbox_profile="compute_no_network",
        execution_kind="deterministic", cache_policy="none", cost_class="free",
        executor=sorted(
            [1],
            key=lambda args, ctx=None: [],
        ) and (lambda args, ctx=None: []),
    )

    source = atom_cards._built_in_source(atom)

    assert source.lines and source.helpers == ()
    assert "Вспомогательн" in source.helpers_text()
