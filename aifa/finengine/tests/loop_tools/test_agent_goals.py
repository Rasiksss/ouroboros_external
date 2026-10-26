"""Части цели, которые описал агент (ADR-0060 п. 2, CONTRACTS.md §3.1): сценарий службы по коду, проверки службы.

Канал частей — подставной (`Goals`): здесь проверяется, что служба принимает и отклоняет, и что уходит в конвейер;
сам конвейер по части цели — `test_open_and_restore.py` и сквозной `tests/assistant/test_tool_api_to_journal.py`.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from loop_support import make_passport, open_cycle
from tool_harness import ctx_of

from finengine.contracts import GoalClause
from finengine.skills.spec.dynamic_run import ScenarioResult

MOVE = {"objective": "получить показатели", "reason": "нужны для ответа"}
FORMULA = {
    "name_ru": "Долг к выручке", "unit": "dimensionless", "expression": "TOTAL_DEBT / SALES_REVENUE",
    "components": [{"key": "TOTAL_DEBT"}, {"key": "SALES_REVENUE"}],
}


@dataclass
class Goals:
    """Канал частей цели: исполнимы все; помнит, какие части и с какими параметрами ему отдали."""

    given: list[GoalClause] = field(default_factory=list)
    ok: bool = True

    def refusal(self, clause: GoalClause) -> str | None:
        return "у вида custom_analysis нет детерминированного корня" if clause.intent_kind == "custom_analysis" else None

    def __call__(self, clauses: list[GoalClause]) -> ScenarioResult:
        self.given.extend(clauses)
        return ScenarioResult(self.ok, "✓ получено" if self.ok else "✗ данных нет в отчёте")


async def agent_cycle(base, *, role: str | None = None):
    cycle = await open_cycle(base, passport=make_passport(), entry_stage="agent")
    goals = Goals()
    cycle.session.run_goals = goals
    cycle.session.goal = cycle.session.goal.model_copy(update={"role": role})
    return cycle, goals


def code_of(reply: str) -> str:
    return reply.split("\n", 1)[0]


async def test_a_goal_names_a_scenario_of_the_service_and_goes_to_the_pipeline_as_described(base) -> None:
    """Сценарий — по коду, коды показателей — из реестра профиля: часть уходит в конвейер без разбора фразы."""
    cycle, goals = await agent_cycle(base)

    reply = await cycle.call("aifa_move", {**MOVE, "goals": [
        {"ref": "g1", "scenario": "extract_registered_metric", "concepts": ["sales_revenue"], "period": "2025"},
    ]})

    assert code_of(reply) == "ГОТОВО" and "✓ получено" in reply
    [clause] = goals.given
    assert (clause.goal_ref, clause.intent_kind, clause.period_ref) == ("g1", "extract_registered_metric", "2025")
    assert [target.concept for target in clause.targets] == ["SALES_REVENUE"]
    assert "- g1: extract_registered_metric" in await cycle.call("aifa_state"), "часть видна в состоянии цикла"
    await cycle.close()


async def test_a_code_outside_the_registry_is_refused_with_where_to_look_and_the_move_is_kept(base) -> None:
    """Код не из «Реестра показателей» — отказ с подсказкой; ход цикла не потрачен, в конвейер ничего не ушло."""
    cycle, goals = await agent_cycle(base)

    reply = await cycle.call("aifa_move", {**MOVE, "goals": [
        {"ref": "g1", "scenario": "extract_registered_metric", "concepts": ["PPTX_SLIDES"]},
    ]})

    assert code_of(reply) == "ОТКАЗ неверные_аргументы"
    assert "PPTX_SLIDES" in reply and "Реестре показателей" in reply and "extract_ad_hoc_metric" in reply
    assert goals.given == [] and cycle.session.turns == 0
    await cycle.close()


async def test_a_metric_that_is_not_calculated_is_not_a_calculation(base) -> None:
    cycle, goals = await agent_cycle(base)

    reply = await cycle.call("aifa_move", {**MOVE, "goals": [
        {"ref": "g1", "scenario": "calculate_registered_metric", "concepts": ["SALES_REVENUE"]},
    ]})

    assert code_of(reply) == "ОТКАЗ неверные_аргументы" and "не рассчитываемые" in reply
    assert goals.given == []
    await cycle.close()


async def test_a_scenario_the_service_does_not_have_is_refused_and_not_substituted(base) -> None:
    """Просьбу, которой нет среди сценариев, служба не подменяет другим сценарием (ход 21.10.2026: pptx → `form`)."""
    cycle, goals = await agent_cycle(base)

    unknown = await cycle.call("aifa_move", {**MOVE, "goals": [{"ref": "g1", "scenario": "pptx"}]})
    rootless = await cycle.call("aifa_move", {**MOVE, "goals": [{"ref": "g2", "scenario": "custom_analysis"}]})

    assert code_of(unknown) == "ОТКАЗ неверные_аргументы" and "не сценарий службы" in unknown
    assert code_of(rootless) == "ОТКАЗ неверные_аргументы" and "нет детерминированного пути" in rootless
    assert goals.given == []
    await cycle.close()


async def test_phrases_of_scenarios_are_not_accepted_from_the_agent(base) -> None:
    """ADR-0060 п. 2: фраз `scenarios` инструмент не принимает — классификатор их больше не разбирает."""
    cycle, goals = await agent_cycle(base)

    # Мимо `Cycle.call`: он переводит фразы записанных решений в части цели.
    reply = await cycle.plugin.call(
        "aifa_move", ctx_of(cycle.run.run_id), {**MOVE, "scenarios": ["Собери файл pptx — краткую презентацию"]},
    )

    assert code_of(reply.text) == "ОТКАЗ неверные_аргументы" and "объект части цели в goals" in reply.text
    assert goals.given == []
    await cycle.close()


async def test_a_form_takes_the_role_of_the_interface_and_without_one_needs_it(base) -> None:
    """Роль части — названная агентом, иначе выбранная в интерфейсе; у формы без роли — отказ со списком ролей."""
    without, _ = await agent_cycle(base)
    with_role, goals = await agent_cycle(base, role="risks")
    form = {**MOVE, "goals": [{"ref": "g1", "scenario": "form"}]}

    refused = await without.call("aifa_move", form)
    accepted = await with_role.call("aifa_move", form)

    assert code_of(refused) == "ОТКАЗ неверные_аргументы" and "нужна role" in refused and "risks" in refused
    assert code_of(accepted) == "ГОТОВО" and goals.given[0].role == "risks"
    await without.close()
    await with_role.close()


async def test_a_formula_outside_the_registry_is_checked_before_it_is_run(base) -> None:
    """`calculate_ad_hoc_metric`: формулу агента проверяют те же правила, что формулу классификатора."""
    cycle, goals = await agent_cycle(base)
    bad = {**FORMULA, "expression": "max(TOTAL_DEBT, 1)"}

    refused = await cycle.call("aifa_move", {**MOVE, "goals": [
        {"ref": "g1", "scenario": "calculate_ad_hoc_metric", "formula": bad}]})
    accepted = await cycle.call("aifa_move", {**MOVE, "goals": [
        {"ref": "g2", "scenario": "calculate_ad_hoc_metric", "formula": FORMULA}]})

    assert code_of(refused) == "ОТКАЗ неверные_аргументы" and "формула не принята" in refused
    assert code_of(accepted) == "ГОТОВО", accepted
    assert goals.given[0].parameters["ad_hoc_formula"]["expression"] == "TOTAL_DEBT / SALES_REVENUE"
    await cycle.close()


async def test_a_dependency_must_be_described_and_goes_first(base) -> None:
    """`depends_on` — на описанные части; в одном ходе опора идёт в конвейер раньше зависимой."""
    cycle, goals = await agent_cycle(base)
    revenue = {"ref": "g1", "scenario": "extract_registered_metric", "concepts": ["SALES_REVENUE"]}
    change = {"ref": "g2", "scenario": "explain_change", "concepts": ["SALES_REVENUE"], "depends_on": ["g1"]}

    dangling = await cycle.call("aifa_move", {**MOVE, "goals": [change]})
    together = await cycle.call("aifa_move", {**MOVE, "goals": [change, revenue]})

    assert code_of(dangling) == "ОТКАЗ неверные_аргументы" and "неописанные части" in dangling
    assert code_of(together) == "ГОТОВО" and [clause.goal_ref for clause in goals.given] == ["g1", "g2"]
    await cycle.close()


async def test_cyclic_dependencies_are_refused_before_any_scenario_runs(base) -> None:
    cycle, goals = await agent_cycle(base)
    reply = await cycle.call("aifa_move", {**MOVE, "goals": [
        {"ref": "g1", "scenario": "form", "role": "risks", "depends_on": ["g2"]},
        {"ref": "g2", "scenario": "form", "role": "risks", "depends_on": ["g1"]},
    ]})
    assert code_of(reply) == "ОТКАЗ неверные_аргументы" and "цикл зависимостей" in reply
    assert goals.given == [] and cycle.session.turns == 0
    await cycle.close()


async def test_a_goal_cannot_depend_on_itself(base) -> None:
    cycle, goals = await agent_cycle(base)
    reply = await cycle.call("aifa_move", {**MOVE, "goals": [
        {"ref": "g1", "scenario": "form", "role": "risks", "depends_on": ["g1"]},
    ]})
    assert code_of(reply) == "ОТКАЗ неверные_аргументы" and "цикл зависимостей" in reply
    assert goals.given == []
    await cycle.close()


async def test_a_part_described_again_with_other_parameters_is_not_a_dead_end(base) -> None:
    """Повтор тупика — та же часть с теми же параметрами; поправленная часть с тем же ref — новое действие."""
    cycle, goals = await agent_cycle(base)
    goals.ok = False
    first = {"ref": "g1", "scenario": "extract_registered_metric", "concepts": ["SALES_REVENUE"], "period": "2024"}

    failed = await cycle.call("aifa_move", {**MOVE, "goals": [first]})
    repeated = await cycle.call("aifa_move", {**MOVE, "goals": [first]})
    changed = await cycle.call("aifa_move", {**MOVE, "goals": [{**first, "period": "2025"}]})

    assert "данных нет в отчёте" in failed
    assert code_of(repeated) == "ОТКАЗ повтор_тупика"
    assert code_of(changed) == "ГОТОВО" and [clause.period_ref for clause in goals.given] == ["2024", "2025"]
    await cycle.close()


async def test_a_part_the_engine_could_not_get_is_redone_by_the_state_status_not_by_a_workaround(base) -> None:
    """Прогон 3f05f49d: BANK_DEPOSITS не найден трижды, корень искал обходы. ADR-0083: отказ отсылает к статусу
    показателей в состоянии и к новой попытке шага; пересмотр плана подзадачей из отказа ушёл (ADR-0082)."""
    cycle, goals = await agent_cycle(base)
    goals.ok = False

    reply = await cycle.call("aifa_move", {**MOVE, "goals": [
        {"ref": "g1", "scenario": "extract_registered_metric", "concepts": ["bank_deposits"]},
    ]})

    assert code_of(reply) == "ГОТОВО" and "✗ данных нет в отчёте" in reply
    assert "«Статус показателей» в aifa_state" in reply
    assert "новой попыткой того же шага" in reply
    assert "пересмотр плана" not in reply and "планировщику" not in reply
    assert "новый атом либо give_up" not in reply and "ту же ссылку не повторяй" not in reply
    await cycle.close()
