"""Повтор батча второй стадии отличим от батча в разрезе прогона (21.08.2026).

ЦЕНА, КОТОРАЯ ЭТО КУПИЛА. Замер Северстали Q1-2025 на стенде 20.08 дал девять
записей второй стадии на ВОСЕМЬ батчей: `main_extract_batch_008` стояло дважды —
19,1 с (выход 1591 токен) и 35,5 с (выход 2192). Вход у обеих записей один.
Первым делом пришлось доказывать, что это не сбитая нумерация: два РАЗНЫХ батча
под одним номером означали бы, что один показатель спрошен дважды, а другой не
спрошен вовсе, — это хуже потерянного времени. Нумерация оказалась цела
(`enumerate(enum_batches, start=1)` по списку, который строится один раз, а
`_batch_items` — раздел; держат `test_the_split_is_a_partition_and_fits_the_cap`
и сосед в `test_the_batch_pace_has_something_to_pace.py`), а вторая запись —
переспросом.

Доказывать это пришлось потому, что ОТЛИЧИТЬ ПОВТОР ОТ БАТЧА БЫЛО НЕЧЕМ: имя
стадии у всех попыток было общим, а поле `attempt`, которое движок кладёт в
запись вызова, срезает проекция `run_stages` — `_STAGE_FIELDS` его не содержит.
Попыток четыре, то есть незамеченными могут пройти три лишних вызова на КАЖДЫЙ
батч, и время повтора читается как время батча.

🔴 ПОЧЕМУ СТОРОЖ ЗДЕСЬ ПРОГОННЫЙ, А НЕ ТЕКСТОВЫЙ (правка по разбору
проверяющего). До этой правки файл сверял исходник движка через `ast.unparse` и
называл это «проверкой достижимого кода». Это неправда: разбор в дерево снимает
комментарии, но НИЧЕГО не говорит о том, исполняется ли строка. Мутация
проверяющего, воспроизведена своими руками: оставить строку с суффиксом на
месте и дописать сразу под ней `stage_name = batch_stage` — движок снова зовёт
модель старым именем, повтор снова неотличим от батча, а все пять прежних
сторожей ЗЕЛЁНЫЕ и прогон не краснеет ни одним тестом (45/2174, как и без
мутации). Поэтому имя проверяется тем единственным способом, которым его можно
проверить, — прогоном движка на подделанном шлюзе модели.

ПРОВЕРКА СТОРОЖА ОТКАТОМ (мутации прогнаны 21.08.2026, каждая — красное):
* `stage_name = batch_stage` после присвоения с суффиксом (основная стадия) →
  красны `test_a_retried_main_batch_arrives_as_two_rows` и
  `test_a_batch_that_never_recovered_paid_for_every_attempt`;
* снять суффикс в сегментной стадии (`two_stage_extractor.py:1926`) → красен
  `test_a_retried_segment_batch_arrives_as_two_rows`;
* переименовать `diag["stage2_retry_count"]` в движке → красны прогонные
  проверки счётчика (имя ключа держится прогоном, а не списком в тесте);
* переименовать `segment_stage2_retry_count` → красен сегментный сторож.

Подделан ровно шлюз модели (`llm.complete_with_usage`) в основной стадии и
`_call_json_llm` в сегментной; имена стадий, номера попыток и ключи диагностики
приходят от движка.
"""
from __future__ import annotations

import asyncio
import json
import time

from finengine.skills import extract_text as et
from finengine.skills import two_stage_extractor as ts
from finengine.skills.extract_result import shard_losses
from finengine.skills.metric_schema import registry_for_profile

_DOC_WITH_TABLE = (
    "===PAGE 1===\n"
    "Отчёт о финансовом положении\n\n"
    "| Показатель | 2024 | 2023 |\n"
    "| --- | --- | --- |\n"
    "| Итого активы | 1 000 | 900 |\n"
)

_SEGMENT_TEXT = (
    "===PAGE 7===\n"
    "Информация по сегментам\n\n"
    "| Сегмент | Чистая прибыль |\n"
    "| --- | --- |\n"
    "| Розница | 40 |\n"
)
_SEGMENT_METRIC = {"enum": "NET_INCOME", "name_ru": "Чистая прибыль", "name_en": "Net income"}
_SEGMENT_EVIDENCE = {
    "metrics": {"NET_INCOME": {"tables": [], "quotes": [{"page": 7, "text": "Розница 40"}]}}
}
_SEGMENT_ROWS = {
    "metrics": {
        "NET_INCOME": {
            "rows": [
                {
                    "segment": "Розница",
                    "dimension": "operating_segment",
                    "values": {"FY 2024": 40.0},
                    "unit_text": "млн руб.",
                    "page": 7,
                }
            ]
        }
    }
}


def _run_with_one_batch_stumbling(monkeypatch, *, recovers: bool) -> dict:
    """Настоящий прогон, где ОДИН батч отвечает мусором на первой попытке.

    `recovers=True` — со второй попытки отвечает как надо (переспрос, таблица
    цела); `recovers=False` — мусор на все попытки (батч сорвался насовсем).
    Подделан ровно шлюз модели: имя стадии, номер попытки и ключи диагностики
    обязаны приходить от движка, иначе сторож стережёт собственную выдумку.
    """
    registry = registry_for_profile("ifrs_nf_ru")
    enums = [str(m["enum"]).upper() for m in registry["extractable"]]
    targets: dict[str, list[str]] = {}
    seen: dict[frozenset, int] = {}
    real_build = ts._build_main_extraction_prompt

    def _spy_build(**kw):
        text = real_build(**kw)
        targets[text] = [str(m["enum"]).upper() for m in kw["target_metrics"]]
        return text

    async def _fake_llm(*, prompt, system=None, model=None, response_format=None,
                        max_output=None, omit_max_output=None, **kw):
        asked = targets.get(prompt)
        if asked is None:
            return json.dumps({"metrics": {
                enum: {"tables": [], "quotes": [{"page": 1, "text": "Итого активы 1 000"}]}
                for enum in enums
            }}), {}
        # Попытки одного батча отличаются ПОРЯДКОМ показателей (`_retry_enum_order`),
        # состав тот же — поэтому счёт попыток идёт по составу, а не по тексту.
        key = frozenset(asked)
        seen[key] = seen.get(key, 0) + 1
        if enums[0] in asked and (not recovers or seen[key] == 1):
            return "не JSON, а извинения модели", {}
        return json.dumps({
            enum: {"values": {"2024": 1000.0}, "unit_text": "тыс. руб.", "page": 1}
            for enum in asked
        }), {}

    monkeypatch.setattr(ts, "_build_main_extraction_prompt", _spy_build)
    monkeypatch.setattr(ts.llm, "complete_with_usage", _fake_llm)

    diag: dict = {}

    async def _run() -> None:
        async for event in et.extract_stream_from_text(
            _DOC_WITH_TABLE,
            company_type="non_financial",
            metric_enums=set(enums),
            registry_override=registry,
        ):
            if event.get("type") == "done":
                diag.update(event.get("_diag") or {})

    asyncio.run(_run())
    return diag


def _main_batch_calls(diag: dict) -> list[dict]:
    return [
        request
        for request in diag.get("llm_requests") or ()
        if str(request.get("stage") or "").startswith("main_extract")
    ]


def _run_segment_with_one_batch_stumbling(monkeypatch) -> dict:
    """Тот же опыт в СЕГМЕНТНОЙ стадии: первый вызов батча — мусор, второй — JSON.

    Первая стадия отдана готовой (`compression_override`), поэтому в шлюз идут
    только вызовы батчей и считать их можно по порядку. Имя стадии приходит от
    движка: подделка получает его аргументом и кладёт в запись вызова как есть.
    """
    calls = {"count": 0}

    async def _fake_call_json_llm(*, stage: str, **_kw):
        calls["count"] += 1
        request = ts._llm_request_record(
            stage=stage, model_id="fake", started_at=time.time(), elapsed_s=0.0, usage={}
        )
        if calls["count"] == 1:
            return {}, request, "не JSON, а извинения модели"
        return _SEGMENT_ROWS, request, json.dumps(_SEGMENT_ROWS)

    monkeypatch.setattr(ts, "_call_json_llm", _fake_call_json_llm)

    async def _drain() -> dict:
        diag: dict = {}
        async for event in ts._extract_segment_breakdown_two_stage(
            pdf_text_for_prompt=_SEGMENT_TEXT,
            extractable=[_SEGMENT_METRIC],
            period_current="FY 2024",
            period_prev="FY 2023",
            company_display="ПСБ",
            effective_type="bank",
            model_id="fake",
            compression_override=_SEGMENT_EVIDENCE,
            segment_metrics_override={"NET_INCOME"},
        ):
            if event.get("type") == "segment_diag":
                diag = event.get("_diag_segment") or {}
        return diag

    return asyncio.run(_drain())


def test_a_retried_main_batch_arrives_as_two_rows(monkeypatch):
    """ГЛАВНОЕ: две попытки одного батча приезжают в разрез РАЗНЫМИ именами.

    Проверяется прогоном движка, а не текстом файла: имена берутся из
    `diag["llm_requests"]` — того самого списка, который проецирует `run_stages`
    и по которому читали замер Северстали.
    """
    diag = _run_with_one_batch_stumbling(monkeypatch, recovers=True)
    calls = _main_batch_calls(diag)
    assert len(calls) == diag["stage2_batch_count"] + 1 > 2, (
        f"опыт не удался: батчей {diag['stage2_batch_count']}, вызовов {len(calls)} — "
        "переспроса в прогоне нет, сторожить нечего")
    names = [str(call["stage"]) for call in calls]
    assert len(set(names)) == len(names), (
        f"повтор снова неотличим от батча — в разрезе два одинаковых имени: {names}")
    retried = [call for call in calls if int(call["attempt"]) > 1]
    assert len(retried) == 1, f"переспрошенных попыток {len(retried)}, ждали одну"
    batch_no = int(retried[0]["batch_no"])
    twins = [str(call["stage"]) for call in calls if int(call["batch_no"]) == batch_no]
    assert len(twins) == 2, twins
    assert all(name.startswith(f"main_extract_batch_{batch_no:03d}") for name in twins), (
        f"повтор потерял номер своего батча: {twins} — по имени его больше не "
        "отнести к батчу, а префикс `main_extract_batch_` ищут читатели разреза")


def test_a_retried_main_batch_reaches_the_run_journal(monkeypatch):
    """Тот же прогон: переспрос доехал до потерь, и он не путается со срывом.

    Ключи диагностики держатся ПРОГОНОМ: переименуй `stage2_retry_count` в
    движке — `batch_retries` молча станет нулём, и этот тест покраснеет.
    """
    diag = _run_with_one_batch_stumbling(monkeypatch, recovers=True)
    losses = shard_losses(diag)
    assert losses["batch_retries"] == 1, f"переспрос не доехал до потерь: {losses}"
    assert losses["batches_failed"] == 0, (
        "переспрос засчитан за сорвавшийся батч — таблица-то цела")
    assert losses["batches"] == diag["stage2_batch_count"] > 1


def test_a_batch_that_never_recovered_paid_for_every_attempt(monkeypatch):
    """Насовсем сорвавшийся батч виден ОБОИМИ числами, и они не складываются.

    `batch_retries` считает ЛИШНИЕ ВЫЗОВЫ (цена во времени и токенах),
    `batches_failed` — дыру в таблице. У сорвавшегося батча одно не заменяет
    другое: он даёт `attempts - 1` в первое и 1 во второе. Это записано в
    докстринге `shard_losses`, и здесь оно закреплено прогоном, иначе читатель
    журнала сложит два числа и получит несуществующие потери.
    """
    diag = _run_with_one_batch_stumbling(monkeypatch, recovers=False)
    attempts = int(diag["stage2_batch_attempts"])
    assert attempts > 1, "попытка одна — переспрашивать нечем, опыт не о том"
    losses = shard_losses(diag)
    assert losses["batches_failed"] == 1, f"сорвавшийся батч потерялся: {losses}"
    assert losses["batch_retries"] == attempts - 1, (
        f"лишние вызовы сорвавшегося батча не посчитаны: {losses}, попыток {attempts}")
    failed_no = int(diag["stage2_invalid_batches"][0]["batch_no"])
    names = [
        str(call["stage"]) for call in _main_batch_calls(diag)
        if int(call["batch_no"]) == failed_no
    ]
    assert len(names) == attempts and len(set(names)) == attempts, (
        f"{attempts} попыток сорвавшегося батча приехали под именами {names} — "
        "в разрезе они сольются в один батч, и его время прочитают как время батча")


def test_a_retried_segment_batch_arrives_as_two_rows(monkeypatch):
    """Сегментная стадия — та же правка, тот же прогон, свой счётчик.

    Правило, заведённое в одной из двух стадий, — дефект: стадии правятся
    порознь и расходятся молча.
    """
    segment_diag = _run_segment_with_one_batch_stumbling(monkeypatch)
    names = [
        str(request.get("stage"))
        for request in segment_diag.get("llm_requests") or ()
        if str(request.get("stage") or "").startswith("segment_extract_batch_")
    ]
    assert len(names) == 2, f"опыт не удался: вызовов сегментного батча {len(names)}, ждали два"
    assert len(set(names)) == 2, (
        f"повтор сегментного батча снова неотличим от батча: {names}")
    # Знаменатель у сегментных переспросов свой: `batches` — основная стадия.
    losses = shard_losses({"stage2_batch_count": 1, **segment_diag})
    assert losses["segment_batch_retries"] == 1, (
        f"переспрос сегментной стадии не доехал до потерь: {losses}")
    assert losses["segment_batches"] == 1, losses
    assert losses["batch_retries"] == 0, (
        f"сегментный переспрос засчитан основной стадии: {losses}")
