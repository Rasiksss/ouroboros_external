"""Тексты навыков о скорости планирования (прогоны на dev 22.10.2026): форма плана, размер, бухгалтерия `task_plan`, каталог."""

from __future__ import annotations

import pathlib
import re

from aifa_engine_support import SKILL_DIR

SKILLS = SKILL_DIR.parent
PLANNING = SKILLS / "aifa-planning"


def read(path: pathlib.Path) -> str:
    return " ".join(path.read_text(encoding="utf-8").split())


def plan_author() -> str:
    return read(SKILLS / "aifa-plan-author" / "SKILL.md")


def plans() -> str:
    return read(PLANNING / "references" / "plans.md")


def guide() -> str:
    return read(PLANNING / "SKILL.md")


def test_the_planner_takes_the_schema_before_it_writes_and_adds_no_fields_of_its_own() -> None:
    """Прогон 22.10: план с `moves_budget` и без `schema_version` отклонён, ушло три подзадачи планирования."""
    text = plan_author()

    assert 'До составления плана получи схему `task_plan(action="schema")`' in text
    assert "поля вне схемы не добавляй" in text


def test_a_step_input_that_names_a_step_is_repeated_in_depends_on() -> None:
    """Прогон 22.10: вход шага ссылался на шаг, которого не было в `depends_on` — отказ дважды в двух прогонах."""
    text = plan_author()

    assert 'source="step"' in text
    assert "обязательно повтори в `depends_on`" in text


def test_the_planner_checks_the_proposal_against_a_list_before_returning_it() -> None:
    text = plan_author()
    check = text.split("Перед отправкой проверь", 1)[1].split("Верни полный объект", 1)[0]

    for item in ("только из схемы", '`source="step"` есть в `depends_on`', "объявлены", "`verification_method`"):
        assert item in check, item


def test_the_proposal_is_compact_and_has_no_conditional_branches_as_steps() -> None:
    """Прогон 22.10: план на 25 КБ и 13 шагов с условными ветками писался три минуты."""
    text = plan_author()

    assert "не больше 12 000 знаков" in text
    assert "критерий на шаг один" in text
    assert "число шагов не ограничено" in text
    assert "Ветвление" in text and "не записывай шагами" in text


def test_the_planner_does_not_reread_what_the_root_already_found_in_the_report() -> None:
    assert "не перечитывай документ" in plan_author()


def test_the_root_hands_the_planner_the_request_and_does_not_retell_the_state_or_the_catalog() -> None:
    """ADR-0084: профиль, периоды, реестр и каталог планировщик читает сам; пересказ корня дублировал его чтение."""
    step = plans().split("2. `schedule_task(", 1)[1].split("3. Дождись", 1)[0]

    assert "исходный запрос без пересказа" in step
    assert "планировщик читает сам (`aifa_state`, `aifa_atoms`)" in step and "не пересказывай" in step
    assert "названия подходящих атомов" not in step and "профиль отчёта и список периодов" not in step


def test_the_root_does_not_write_a_running_step_that_the_execution_call_starts_itself() -> None:
    """Прогон 22.10: 10 вызовов `step running` в первом прогоне, хотя dispatch ставит `running` сам."""
    text = plans()

    assert "`step` со статусом `running` не записывай" in text
    assert "без исполнительного вызова" in text


def test_the_schema_is_taken_once_per_turn() -> None:
    assert "Схему бери один раз за ход" in plans()


def test_result_refs_are_pasted_from_the_answer_and_never_typed() -> None:
    """Прогон 22.10: два `review` подряд отклонены, в хеше `sha256` модель исказила один знак."""
    text = plans()

    # Ссылки на результаты корень больше не набирает: их берёт `accept_step` (ADR-0079).
    assert "приими шаг одной записью" in text
    assert "`accept_step`" in text


def test_every_executor_call_of_the_root_carries_the_plan_binding() -> None:
    assert "обязателен `_plan" in plans()
    assert "в том числе первый" in plans()


def test_the_planner_reads_the_state_and_the_catalog_itself_and_the_root_only_checks_the_plan() -> None:
    """ADR-0084: прогон 22.10 заставил корень читать каталог до плана, потому что планировщик его не открывал;
    теперь это условие стоит в навыке планировщика, а корень читает каталог при проверке предложения."""
    text = guide()

    assert "Планировщик сам читает реестр показателей, профиль и периоды (`aifa_state`) и каталог атомов" in text
    assert "ты их ему не пересказываешь" in text
    assert "Каталог читай, когда проверяешь план" in text
    assert "Каталог читаешь сам, до заказа плана" not in text
    assert text.index("Каталог читай, когда проверяешь план") < text.index("### Делегирование")


def test_the_planner_must_read_the_catalog_before_the_steps_and_name_an_atom_or_a_scenario_for_each_step() -> None:
    text = plan_author()

    assert "Контекст службы читай сам: `aifa_state` (профиль отчёта, доступные периоды, реестр показателей" in text
    assert "каталог атомов `aifa_atoms` — до составления шагов, а не после" in text
    assert "Плана, где шаг извлечения или расчёта не назван атомом или сценарием по карточке каталога, не возвращай" in text
    assert "каждый шаг с атомом или сценарием назван по карточке" in text


def test_the_guide_stays_within_its_size_limit() -> None:
    assert len(guide()) <= 9_000


def test_no_new_rule_leans_on_development_vocabulary() -> None:
    for text in (plan_author(), plans(), guide()):
        assert not re.search(r"ADR-|CONTRACTS|DynamicRun|PROGRESS", text)


def test_the_root_does_not_study_the_report_before_it_orders_the_plan() -> None:
    """Прогон 15:16–15:36: корень 8 минут изучал отчёт сам (12 вызовов `aifa_doc`), а план так и не заказал."""
    text = guide()

    assert "### Корень не читает отчёт" in text
    assert "`aifa_doc` до заказа плана и при приёмке не вызывай" in text
    assert "Не знаешь, где в отчёте показатель, — не повод читать" in text


def test_the_root_hands_over_only_what_the_state_already_knows() -> None:
    step = plans().split("2. `schedule_task(", 1)[1].split("3. Дождись", 1)[0]

    assert "профиль отчёта и список периодов" not in step, "планировщик читает их сам (ADR-0084)"
    assert "что уже найдено" not in step


def test_the_root_does_not_read_the_skill_of_the_planner() -> None:
    assert "Навык `aifa-plan-author` не читай" in guide()


def test_the_plan_binding_is_for_executor_calls_after_the_plan_is_accepted() -> None:
    """Прогоны 22.10: `_plan` на `aifa_open` дважды и на заказе плана — отказ `PLAN_EXECUTION_REFUSED`."""
    text = plans()

    assert "`aifa_open` и заказ плана — без `_plan`" in text


def test_the_root_does_not_read_the_catalog_and_registry_snapshots_before_ordering_the_plan() -> None:
    """ADR-0085: прогон 26.10 — корень 6 минут читал `atoms.md` и `metrics.md` и все справочники подряд."""
    text = guide()

    assert "`references/atoms.md` — снимок каталога и `references/metrics.md` — общие показатели реестра" in text
    assert "оба — только при проверке плана, до заказа не читай" in text
    assert "Остальные — по мере нужды, не все подряд" in text


def test_the_root_passes_the_request_to_the_planner_word_for_word() -> None:
    step = plans().split("2. `schedule_task(", 1)[1].split("3. Дождись", 1)[0]

    assert "Запрос передавай дословно, своими словами не излагай" in step
    assert "профиль и показатели не называй" in step


def test_the_planner_does_not_call_aifa_doc_while_planning() -> None:
    """ADR-0085: прогон 26.10 — планировщик сделал 15 вызовов `aifa_doc` за минуту, читая отчёт постранично."""
    text = plan_author()

    assert "`aifa_doc` не вызывай: значения и страницы находит исполнитель" in text
    assert "`aifa_doc` открывай только ради конкретного неясного места" not in text


def test_the_planner_merges_code_steps_that_one_atom_can_do() -> None:
    """ADR-0087: прогон 26.10 — три автора атомов подряд (ставка, статистики, Excel) и лимит цикла до сборки файла."""
    text = plan_author()

    assert "Шаги с кодом объединяй" in text
    assert "если их можно выполнить одним атомом" in text
    assert "запиши один шаг-автор" in text
    assert "Цепочку авторов оставляй, только если между ними нужен другой исполнитель" in text
    assert "Каждый лишний автор — минуты цикла" in text
