"""Список ячеек объявлен в ДВУХ местах — и они обязаны совпадать (#250).

ЗАЧЕМ ЭТОТ ФАЙЛ. 15.08.2026 я перенёс РСБУ: завёл реестры двух ячеек, собрал
справочник на 111 записей, притащил 26 документов эталонной разметки, поднял
`metric_schema.PROFILE_IDS` с восьми ячеек до десяти. И не проверил ВТОРОЙ
список — `extractors/_classifier.PROFILE_IDS`, который остался на восьми.

Результат: весь перенос лежал МЁРТВЫМ ГРУЗОМ. Реестр есть, справочник есть,
эталоны есть — а классификатор про РСБУ не знает, `_compose_profile_id` такую
ячейку не возвращает, и ни один документ туда не попадёт. Ни одна проверка не
покраснела: каждая сторона была внутренне согласована.

Это ровно та болезнь, которой в проекте уже пять случаев за 05.08: сделанное не
доезжает до места работы. Лечится не внимательностью, а сторожем на СТЫК.

ПОЧЕМУ СПИСКА ДВА, А НЕ ОДИН. Классификатор уезжает в станцию отдельным
скриптом навыка и не должен тянуть за собой загрузку восьми json-реестров ради
одного кортежа имён. Плата за это — обязанность держать списки равными, и
обязанность эта теперь проверяемая.

ПРОВЕРКА СТОРОЖА ОТКАТОМ: убрать любую ячейку из любого списка — тест назовёт
её и скажет, в каком из двух мест она пропала.
"""
from __future__ import annotations

from finengine.skills.extractors._classifier import PROFILE_IDS as CLASSIFIER_CELLS
from finengine.skills.extractors._classifier import _compose_profile_id
from finengine.skills.metric_schema import PROFILE_IDS as REGISTRY_CELLS


def test_both_lists_of_cells_match():
    """Реестр знает ровно те ячейки, что умеет назвать классификатор."""
    only_registry = sorted(set(REGISTRY_CELLS) - set(CLASSIFIER_CELLS))
    only_classifier = sorted(set(CLASSIFIER_CELLS) - set(REGISTRY_CELLS))
    assert not only_registry, (
        f"ячейки {only_registry} есть в реестре, но классификатор о них не знает — "
        "документ туда не попадёт НИКОГДА, и перенос ляжет мёртвым грузом")
    assert not only_classifier, (
        f"классификатор умеет назвать {only_classifier}, а реестра для них нет — "
        "прогон упадёт на загрузке ячейки")


def test_every_cell_is_reachable_from_the_composer():
    """Мало знать имя ячейки — до неё должна вести дорога.

    `_compose_profile_id` — единственное место, где стандарт и тип компании
    превращаются в имя ячейки. Ячейка, которую он не возвращает ни при каком
    сочетании, недостижима, даже если объявлена в обоих списках.
    """
    reachable = {
        _compose_profile_id(standard, company_type, language)
        for standard in ("ifrs", "rsbu", "us_gaap", "ind_as", "ind_gaap")
        for company_type in ("bank", "non_financial")
        for language in ("ru", "en")
    }
    unreachable = sorted(set(REGISTRY_CELLS) - reachable)
    assert not unreachable, (
        f"до ячеек {unreachable} не ведёт ни одно сочетание стандарт/тип/язык — "
        "они объявлены, но недостижимы")


def test_the_russian_statutory_cells_are_reachable():
    """Отдельно про РСБУ — из-за него сторож и появился."""
    assert _compose_profile_id("rsbu", "non_financial", "ru") == "rsbu_nf_ru"
    assert _compose_profile_id("rsbu", "bank", "ru") == "rsbu_bank_ru"
