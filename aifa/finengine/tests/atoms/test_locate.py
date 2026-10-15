"""`locate` — критерий готовности атома (docs/build-plan/
15a-srez-05a-locate-read-value-yadro.md, карточка `locate`):

1. Правильная страница/область входит в кандидаты минимум для 98% фактов
   `tests/golden/extraction/` (замер на реальном golden-наборе — вне этой
   сессии, см. `tests/golden/extraction/README.md`; здесь — точные примеры
   того же свойства на единичных случаях).
2. Ни один кандидат не ссылается на несуществующую страницу или диапазон
   (гарантируется контрактом `SourceLocator`/`CandidateLocator`).
3. Отсутствующий концепт может вернуть пустой набор кандидатов.
4. Пакетный и поконцептный режим дают эквивалентные логические кандидаты;
   один сбой концепта не стирает остальные.
5. `locate` не возвращает финансовое значение и не подменяет `read.value`.

Единственная модельная граница (`_call_json_llm`) подменяется дублем — тот же
приём, каким пользуются другие модельные атомы этого репозитория.
"""
from __future__ import annotations

import asyncio

from finengine.contracts import CandidateLocator
from finengine.skills import two_stage_extractor
from finengine.skills.atoms import locate as locate_module
from finengine.skills.atoms.locate import locate_batch

_DOCUMENT_TEXT = (
    "===PAGE 1===\n"
    "REVENUE STATEMENT\n"
    "| Item | 2024 |\n"
    "| Revenue | 500 000 |\n"
    "| Cost of goods sold | 300 000 |\n"
)

_METRICS = [
    {"enum": "REVENUE", "name_en": "Revenue", "name_ru": "Выручка", "aliases_en": [], "aliases_ru": []},
    {
        "enum": "COST_OF_SALES", "name_en": "Cost of sales", "name_ru": "Себестоимость",
        "aliases_en": ["Cost of goods sold"], "aliases_ru": [],
    },
    # Ни имя, ни псевдонимы этого показателя не пересекаются со словами
    # `_DOCUMENT_TEXT` — специально для теста «цитата без таблицы», где
    # случайное срабатывание алиас-поиска исказило бы число кандидатов.
    {
        "enum": "EFFECTIVE_TAX_RATE", "name_en": "Effective tax rate", "name_ru": "Эффективная налоговая ставка",
        "aliases_en": [], "aliases_ru": [],
    },
]


def _patch_call_json_llm(monkeypatch, response: dict):
    async def _fake(**kwargs):  # noqa: ARG001
        return response, {"stage": kwargs.get("stage")}, "{}"
    monkeypatch.setattr(two_stage_extractor, "_call_json_llm", _fake)


def test_missing_concept_returns_empty_candidates_without_asking_the_model(monkeypatch):
    calls = []

    async def _fake(**kwargs):
        calls.append(kwargs)
        return {}, {}, "{}"
    monkeypatch.setattr(two_stage_extractor, "_call_json_llm", _fake)

    result = locate_batch(
        ["UNKNOWN_CONCEPT"], document_id="doc-1", document_version="v1",
        document_text=_DOCUMENT_TEXT, metrics=_METRICS,
    )
    assert result == {"UNKNOWN_CONCEPT": []}
    assert calls == [], "показатель, которого нет в реестре, не должен доходить до модели вовсе"


def test_one_physical_call_for_a_batch_of_concepts(monkeypatch):
    calls = []

    async def _fake(**kwargs):
        calls.append(kwargs)
        return {"metrics": {
            "REVENUE": {"tables": [{"table_id": "0", "location": "revenue table"}], "quotes": []},
            "COST_OF_SALES": {"tables": [], "quotes": []},
        }}, {}, "{}"
    monkeypatch.setattr(two_stage_extractor, "_call_json_llm", _fake)

    result = locate_batch(
        ["REVENUE", "COST_OF_SALES"], document_id="doc-1", document_version="v1",
        document_text=_DOCUMENT_TEXT, metrics=_METRICS,
    )
    assert len(calls) == 1, "локатор обязан спросить модель ОДИН раз на весь батч концептов, не по одному"
    assert set(result) == {"REVENUE", "COST_OF_SALES"}


def test_a_large_batch_is_split_into_shards_and_the_results_are_joined(monkeypatch):
    """Живой прогон 08.10.2026 (ЛУКОЙЛ, 58 показателей): один вызов считался
    ~372 с при пределе клиента 360 с и не завершился ни разу. Шард отвечает
    только за свои показатели, а вызывающий получает общий результат."""
    monkeypatch.setattr(locate_module.settings, "locate_batch_size", 1)
    calls = []

    async def _fake(**kwargs):
        calls.append(kwargs["prompt"])
        asked = "COST_OF_SALES" if '"COST_OF_SALES"' in kwargs["prompt"] else "REVENUE"
        return {"metrics": {asked: {"tables": [{"table_id": "0", "location": "revenue table"}], "quotes": []}}}, {}, "{}"
    monkeypatch.setattr(two_stage_extractor, "_call_json_llm", _fake)

    result = locate_batch(
        ["REVENUE", "COST_OF_SALES"], document_id="doc-1", document_version="v1",
        document_text=_DOCUMENT_TEXT, metrics=_METRICS,
    )
    assert len(calls) == 2, "два показателя при размере шарда 1 — два вызова модели"
    assert sum('"COST_OF_SALES"' in prompt for prompt in calls) == 1, "шард спрашивает только свои показатели"
    assert all(_DOCUMENT_TEXT in prompt for prompt in calls), "каждый шард читает весь отчёт"
    assert [c.origin for c in result["REVENUE"]] == ["model"]
    assert "model" in [c.origin for c in result["COST_OF_SALES"]]


def test_shards_run_concurrently_within_the_extract_jobs_limit(monkeypatch):
    monkeypatch.setattr(locate_module.settings, "locate_batch_size", 1)
    in_flight = []
    peak = []

    async def _fake(**kwargs):  # noqa: ARG001
        in_flight.append(1)
        peak.append(len(in_flight))
        await asyncio.sleep(0.01)
        in_flight.pop()
        return {"metrics": {}}, {}, "{}"
    monkeypatch.setattr(two_stage_extractor, "_call_json_llm", _fake)

    monkeypatch.setattr(locate_module.settings, "extract_batch_jobs", 4)
    locate_batch(
        ["REVENUE", "COST_OF_SALES", "EFFECTIVE_TAX_RATE"], document_id="doc-1", document_version="v1",
        document_text=_DOCUMENT_TEXT, metrics=_METRICS,
    )
    assert max(peak) == 3, "шарды обязаны идти одновременно, иначе время вызова не сократится"

    peak.clear()
    monkeypatch.setattr(locate_module.settings, "extract_batch_jobs", 1)
    locate_batch(
        ["REVENUE", "COST_OF_SALES", "EFFECTIVE_TAX_RATE"], document_id="doc-1", document_version="v1",
        document_text=_DOCUMENT_TEXT, metrics=_METRICS,
    )
    assert max(peak) == 1, "одновременность ограничена общим темпом извлечения"


def test_candidate_references_a_real_table_from_the_document(monkeypatch):
    _patch_call_json_llm(monkeypatch, {"metrics": {
        "REVENUE": {"tables": [{"table_id": "0", "location": "revenue table"}], "quotes": []},
    }})
    result = locate_batch(
        ["REVENUE"], document_id="doc-1", document_version="v1",
        document_text=_DOCUMENT_TEXT, metrics=_METRICS,
    )
    candidates = result["REVENUE"]
    assert len(candidates) == 1
    candidate = candidates[0]
    assert isinstance(candidate, CandidateLocator)
    assert candidate.concept == "REVENUE"
    assert candidate.origin == "model"
    assert candidate.locator.page == 1
    assert "Revenue" in candidate.locator.text_fragment
    assert not hasattr(candidate, "value"), "locate не имеет права нести финансовое значение"


def test_alias_match_is_tagged_as_alias_origin_not_model(monkeypatch):
    """Модель не назвала ни одной таблицы для COST_OF_SALES, но алиас
    «Cost of goods sold» реально встречается в тексте отчёта — детерминированное
    совпадение добавляется как кандидат, но не выдаётся за модельный вывод."""
    _patch_call_json_llm(monkeypatch, {"metrics": {
        "COST_OF_SALES": {"tables": [], "quotes": []},
    }})
    result = locate_batch(
        ["COST_OF_SALES"], document_id="doc-1", document_version="v1",
        document_text=_DOCUMENT_TEXT, metrics=_METRICS,
    )
    candidates = result["COST_OF_SALES"]
    assert candidates, "алиас есть буквально в тексте — кандидат обязан появиться даже без модели"
    assert all(c.origin == "alias_match" for c in candidates)


def test_ad_hoc_metric_can_be_split_between_a_table_title_and_its_row(monkeypatch):
    """В пояснениях ЛУКОЙЛ слово «Выручка» вынесено в название таблицы,
    а строка содержит только продолжение «по посреднической деятельности»."""
    document_text = """===PAGE 24===
Таблица 12. Выручка
тыс. руб.
| Наименование показателя | за январь-июнь 2026 г. | за январь-июнь 2025 г. |
| --- | --- | --- |
| Выручка | 1 453 957 053 | 1 398 538 772 |
| по посреднической деятельности | 20 036 818 | 19 651 110 |
| по прочей деятельности | 3 361 639 | 5 342 972 |
"""
    concept = "Выручка по посреднической деятельности"
    metric = {
        "enum": concept,
        "name_ru": concept,
        "name_en": "Revenue from intermediary activities",
        "aliases_ru": [concept, "посредническая выручка"],
        "aliases_en": ["intermediary revenue"],
    }
    _patch_call_json_llm(monkeypatch, {"metrics": {
        concept.upper(): {"tables": [], "quotes": []},
    }})

    result = locate_batch(
        [concept], document_id="doc-lukoil", document_version="v1",
        document_text=document_text, metrics=[metric], language="ru",
    )

    assert len(result[concept]) == 1
    candidate = result[concept][0]
    assert candidate.origin == "alias_match"
    assert candidate.locator.page == 24
    assert "20 036 818" in candidate.locator.text_fragment


def test_long_table_candidate_keeps_the_matching_bottom_row(monkeypatch):
    """Regression: LUKOIL form row 2500 was below the old 4000-char cut."""
    from finengine.skills.metric_schema import registry_for_profile

    filler = "\n".join(
        f"| Показатель {index} | {2100 + index} | 1 000 000 | 900 000 |"
        for index in range(90)
    )
    document_text = (
        "===PAGE 4===\n"
        "Единица измерения тыс. руб.\n"
        "| Наименование показателя | Код | За январь-июнь 2026 г. | За январь-июнь 2025 г. |\n"
        "| --- | --- | --- | --- |\n"
        f"{filler}\n"
        "| Совокупный финансовый результат | 2500 | 361 959 175 | 327 736 355 |\n"
        "| Базовая прибыль на акцию, руб. коп. | 2900 | 522,41 | 473,02 |\n"
    )
    metric = {
        "enum": "СОВОКУПНЫЙ ФИНАНСОВЫЙ РЕЗУЛЬТАТ",
        "name_ru": "Совокупный финансовый результат",
        "name_en": "Total comprehensive income",
        "aliases_ru": [],
        "aliases_en": [],
    }
    eps = next(row for row in registry_for_profile("rsbu_nf_ru")["extractable"] if row["enum"] == "EPS")
    _patch_call_json_llm(monkeypatch, {"metrics": {
        metric["enum"]: {"tables": [], "quotes": []},
        "EPS": {"tables": [], "quotes": []},
    }})

    result = locate_batch(
        [metric["enum"], "EPS"], document_id="doc-lukoil", document_version="v1",
        document_text=document_text, metrics=[metric, eps], language="ru",
    )

    fragment = result[metric["enum"]][0].locator.text_fragment
    assert "За январь-июнь 2026 г." in fragment
    assert "Совокупный финансовый результат" in fragment
    assert "361 959 175" in fragment
    assert len(fragment) <= 4000

    assert "Базовая прибыль (убыток) на акцию" in eps["aliases_ru"]
    eps_fragment = result["EPS"][0].locator.text_fragment
    assert "Базовая прибыль на акцию" in eps_fragment
    assert "522,41" in eps_fragment


def test_rsbu_row_codes_locate_statutory_balance_totals(monkeypatch):
    """РСБУ-коды строк — устойчивый адрес, когда подпись итога не похожа на имя метрики."""
    from finengine.skills.metric_schema import registry_for_profile

    document_text = """===PAGE 2===
| Наименование показателя | Код | 30 июня 2026 | 31 декабря 2025 |
| Итого по разделу II | 1200 | 1 117 811 693 | 890 870 289 |
| БАЛАНС (сумма строк 1100+1200) | 1600 | 2 833 875 551 | 2 724 370 748 |
| Итого по разделу III | 1300 | 1 290 565 821 | 1 110 183 683 |
| Итого по разделу V | 1500 | 1 499 720 323 | 1 548 797 608 |
"""
    enums = ["TOTAL_ASSETS", "TOTAL_EQUITY", "TOTAL_CURRENT_ASSETS", "TOTAL_CURRENT_LIABILITIES"]
    registry = registry_for_profile("rsbu_nf_ru")
    metrics = [row for row in registry["extractable"] if row["enum"] in enums]
    _patch_call_json_llm(monkeypatch, {"metrics": {
        enum: {"tables": [], "quotes": []} for enum in enums
    }})

    result = locate_batch(
        enums, document_id="doc-rsbu", document_version="v1",
        document_text=document_text, metrics=metrics, language="ru",
    )

    expected_codes = {
        "TOTAL_ASSETS": "1600", "TOTAL_EQUITY": "1300",
        "TOTAL_CURRENT_ASSETS": "1200", "TOTAL_CURRENT_LIABILITIES": "1500",
    }
    for enum, code in expected_codes.items():
        assert result[enum], f"{enum} не найден по строке РСБУ {code}"
        assert code in result[enum][0].locator.text_fragment


def test_rsbu_row_code_locates_general_dividends_not_nci_dividends(monkeypatch):
    """Строка 4322 ОДДС — все уплаченные дивиденды, не только NCI."""
    from finengine.skills.metric_schema import registry_for_profile

    document_text = """===PAGE 9===
| Наименование показателя | Код | Январь-июнь 2026 | Январь-июнь 2025 |
| собственникам (участникам) в связи с выкупом у них акций или их выходом | 4321 | 0 | 0 |
| на уплату дивидендов и иных платежей по распределению прибыли | 4322 | (454 149 477) | (334 309 325) |
"""
    registry = registry_for_profile("rsbu_nf_ru")
    metric = next(row for row in registry["extractable"] if row["enum"] == "DIVIDENDS")
    _patch_call_json_llm(monkeypatch, {"metrics": {
        "DIVIDENDS": {"tables": [], "quotes": []},
    }})

    result = locate_batch(
        ["DIVIDENDS"], document_id="doc-rsbu", document_version="v1",
        document_text=document_text, metrics=[metric], language="ru",
    )

    assert result["DIVIDENDS"]
    assert "4322" in result["DIVIDENDS"][0].locator.text_fragment


def test_quote_without_a_table_becomes_a_quote_candidate(monkeypatch):
    _patch_call_json_llm(monkeypatch, {"metrics": {
        "EFFECTIVE_TAX_RATE": {"tables": [], "quotes": [{"page": 1, "text": "The effective tax rate was 20%."}]},
    }})
    result = locate_batch(
        ["EFFECTIVE_TAX_RATE"], document_id="doc-1", document_version="v1",
        document_text=_DOCUMENT_TEXT, metrics=_METRICS,
    )
    candidates = result["EFFECTIVE_TAX_RATE"]
    assert len(candidates) == 1
    assert candidates[0].locator.table_or_section is None
    assert candidates[0].locator.text_fragment == "The effective tax rate was 20%."
    assert candidates[0].locator.page == 1
