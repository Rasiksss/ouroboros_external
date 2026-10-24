"""Правила после чтения Excel из dev-прогона 61b91465: компоненты расчётных показателей, итог по `facts`."""

from aifa_engine_support import SKILL_DIR

SKILLS = SKILL_DIR.parent


def text(relative: str) -> str:
    return " ".join((SKILLS / relative).read_text(encoding="utf-8").split())


def test_author_writes_components_of_a_calculated_metric_into_the_file() -> None:
    source = text("aifa-atom-author/SKILL.md")
    assert "Компоненты расчётного показателя выводи в файл всегда" in source
    assert "`derived_fact.expression`" in source
    assert "компоненты не приведены" in source


def test_author_returns_facts_as_strings_taken_from_the_same_values_as_the_cells() -> None:
    source = text("aifa-atom-author/SKILL.md")
    assert "`facts`" in source
    assert "массив строк" in source
    assert "из тех же значений, что и ячейки" in source


def test_author_names_an_operand_that_is_narrower_or_out_of_scale() -> None:
    source = text("aifa-atom-author/SKILL.md")
    assert "несоразмерна" in source
    assert "не прячь" in source


def test_root_answers_with_numbers_only_from_facts_or_the_artifact_table() -> None:
    source = text("aifa-planning/SKILL.md")
    assert "`payload.facts`" in source
    assert "Ключевые цифры" in source
    checking = text("aifa-planning/references/plans.md")
    assert "сравнение двух чисел — тоже расчёт" in checking
    assert "сверь каждое число" in checking


def test_root_does_not_attach_a_second_copy_of_the_published_file() -> None:
    source = text("aifa-planning/SKILL.md")
    assert "второй, непроверенный экземпляр" in source
    assert "`file_read` на него" in source


def test_task_plan_writes_go_one_at_a_time_with_the_version_from_the_last_reply() -> None:
    source = text("aifa-planning/references/plans.md")
    assert "строго по одной" in source
    assert "`stale expected_version`" in source
    assert "`attempt_id` текущей попытки" in source


def test_plan_author_keeps_quotes_inside_json_strings_valid() -> None:
    source = text("aifa-plan-author/SKILL.md")
    assert "нет неэкранированных" in source
    assert "«ёлочках»" in source


def test_root_without_an_accepted_plan_orders_a_new_one_instead_of_executing() -> None:
    source = text("aifa-planning/references/plans.md")
    assert "предложение не принято, плана нет" in source
    assert "не начинай исполнение без принятого плана" in source


def test_root_does_not_read_the_report_and_takes_engine_values_as_truth() -> None:
    source = text("aifa-planning/SKILL.md")
    assert "### Корень не читает отчёт" in source
    assert "— истина: корень их не сверяет" in source
    assert "`aifa_doc` до заказа плана и при приёмке не вызывай" in source


def test_step_acceptance_is_light_and_rests_on_the_step_result() -> None:
    source = text("aifa-planning/references/plans.md")
    assert "Приёмка лёгкая" in source
    assert "индекс артефактов не листай" in source
    assert 'task_plan(action="accept_step"' in source
    assert "Не пиши `step awaiting_review` и `review` отдельно" in source
    assert "`review` с `decision` `revise`/`blocked`" in source
    assert "операнды, определения показателей и ряды не сверяй" in source
    assert "принимай, только прочитав его входы" not in source


def test_plan_author_revises_only_the_step_with_a_gap_by_calculating_or_searching() -> None:
    source = text("aifa-plan-author/SKILL.md")
    assert "### Пересмотр при недостаче" in source
    assert "переделай только шаг с недостачей" in source
    assert "есть в «Рассчитываемых»" in source and "шагом расчёта через компоненты" in source
    assert "строго извлекаемый" in source and "web.search" in source
    assert "близким по названию показателем" in source


def test_root_returns_a_gap_to_the_planner_instead_of_improvising_retries() -> None:
    source = text("aifa-planning/references/plans.md")
    assert "Недостача:" in source
    assert "второй такой же вызов не делай" in source
    assert "закажи пересмотр плана" in source
    assert "показатель, ответ движка, число попыток" in source


def test_moves_no_longer_send_the_root_to_chase_a_missing_metric_itself() -> None:
    source = text("aifa-planning/references/moves.md")
    assert "следующим ходом рассчитай его" not in source
    assert "один раз" in source and "пересмотр плана" in source


def test_a_missing_metric_in_a_move_summary_is_a_gap_not_an_accepted_step() -> None:
    source = text("aifa-planning/references/plans.md")
    assert "строкой «не найден» в итоге хода, где остальные части получены" in source
    assert "у компонента формулы запрошенного расчёта" in source
    assert "Такой шаг не принимай `accept_step`" in source
    assert "пометка «в отчёте отсутствует» недостачу не закрывает" in source
    assert "только тогда `partial`" in source


def test_finalize_does_not_let_the_root_drop_a_requested_metric_before_a_revision() -> None:
    source = text("aifa-planning/SKILL.md")
    assert "«не найден» у показателя запроса — сначала недостача" in source
    assert "расчёт без него не выдавай" in source


def test_the_root_names_the_company_as_in_the_state_not_by_the_file_name() -> None:
    source = text("aifa-planning/SKILL.md")
    assert "Компанию называй как в `aifa_state`, а не по имени файла" in source


def test_plan_author_keeps_the_period_and_access_date_of_a_found_source_and_never_plans_around_a_gap() -> None:
    source = text("aifa-plan-author/SKILL.md")
    assert "дату обращения и тот же период, что у отчёта" in source
    assert "Расчёт без недостающего компонента не планируй" in source
