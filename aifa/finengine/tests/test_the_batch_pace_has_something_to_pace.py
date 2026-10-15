"""Темп второй стадии доезжает до семафора — и семафору ЕСТЬ что сдерживать.

ЧТО ИЗМЕНИЛОСЬ 20.08.2026 И ПОЧЕМУ ФАЙЛ ПЕРЕИМЕНОВАН.
Он назывался `test_the_batch_pace_has_nothing_to_pace.py` и был устроен как
ловушка: проверка `test_the_main_stage_is_one_batch_today` краснела В ОБЕ
СТОРОНЫ — и если сломается цепочка темпа, и если кто-то доведёт размер батча до
движка. Второе и случилось: `extraction_batch_size` теперь идёт настройкой
(`FINENGINE_EXTRACT_BATCH_SIZE`, боевое значение 10) от точки выбора схемы через
публичный вход до движка, вместе с числом попыток (4) и сегментным размером (5).
Значения — боевые у Finengine (`src/finengine/skills/extractors/_base.py`).

ЧТО БЫЛО ДО ПРАВКИ. Публичный вход `extract_stream_finengine_dev_two_stage`
размера батча не объявлял, `extract_text` передавал только темп — значит внутри
размер оставался `None`, ветка уходила в `_split_evenly(extractable, 1)`, и это
ровно ОДИН батч на весь состав ячейки. Шесть мест семафора стояли над одной
задачей: настройка `FINENGINE_EXTRACT_BATCH_JOBS` не делала ничего.

ЧТО СТАЛО, ПРОВЕРЕНО ПРОГОНОМ (`test_the_main_stage_is_sharded_now`):
77 извлекаемых показателей `ifrs_nf_ru` при размере 10 дают 8 батчей,
80 показателей `ifrs_bank_ru` — тоже 8. Одновременно идут 4 (замер пиковой
одновременности в `test_the_semaphore_holds_all_batches_back_together`).

ЧЕМ ЭТО МОЖЕТ УХУДШИТЬ КАЧЕСТВО — три ветки кода, а не гипотезы:
* в промпт батча раскладываются ТОЛЬКО его десять карточек, соседи по реестру
  модели больше не подсказка (`test_a_batch_prompt_carries_only_its_own_metrics`);
* соседи по реестру уезжают в РАЗНЫЕ батчи
  (`test_registry_neighbours_land_in_different_batches`). ⚠️ С 20.08 это
  свойство держит не `strategy="spread"`, а блочная раскладка `balanced`
  (`FINENGINE_EXTRACT_BATCH_STRATEGY`, умолчание `balanced`) — разбор и
  сторожа самой балансировки в
  `tests/test_the_heaviest_batch_sets_the_pace.py`;
* показатель без улик первой стадии не спрашивался вовсе — ⚠️ ОТМЕНЕНО
  20.08.2026 замером (потеря пяти показателей на ПСБ), выброс стал настройкой
  `FINENGINE_EXTRACT_ASK_WITHOUT_EVIDENCE` с умолчанием «спрашивать»; здесь
  осталось `test_a_metric_without_stage1_evidence_is_asked_anyway`, весь
  разбор — в `tests/test_a_metric_without_evidence_is_still_asked.py`. ⚠️ В
  СЕГМЕНТНОЙ стадии выброс остался: там и деление, и пропуск безулиочных стоят
  без охраны размером (`_extract_segment_breakdown_two_stage`, умолчание
  `extraction_batch_size: int = 5`), и настройка туда НЕ заведена.
Мерить по golden.

⚠️ «В СЕГМЕНТНОЙ БАТЧ ВСЕГДА ОДИН» — УТВЕРЖДЕНИЕ УСЛОВНОЕ, А НЕ БЕЗУСЛОВНОЕ
(оговорено 20.08.2026). Оно держится на ДВУХ обстоятельствах, и каждое
меняется без правки кода:
* размер — env-настройка `FINENGINE_EXTRACT_SEGMENT_BATCH_SIZE` (умолчание 5);
* состав — один из трёх встроенных наборов по пять показателей
  (`_SEGMENT_METRICS_IFRS_RU`, `_SEGMENT_METRICS_US_GAAP_EN`,
  `_SEGMENT_METRICS_BANK_RU`), но у `_extract_segment_breakdown_two_stage` есть
  `segment_metrics_override`, и он состав задаёт целиком.
Опусти размер или расширь состав — сегментная стадия шардируется и МОЛЧА
выбрасывает безулиочных: её ключ `segment_stage2_skipped_empty_evidence`
`shard_losses` не читает вовсе, значит потеря не видна ни в исходе, ни в
журнале. Здесь это не чинится (настройки в сегментную стадию не заведено);
здесь это названо, чтобы условное утверждение не читалось безусловным.
Условия держит `test_the_segment_stage_is_still_one_batch`.

⚠️ ЧЕГО ЗДЕСЬ НЕТ, СКОЛЬКО БЫ НИ КАЗАЛОСЬ. Строка вызова сборщика
`all_metrics=(target_metrics if extraction_batch_size is not None else
extractable)` выглядит рубильником состава промпта и им НЕ ЯВЛЯЕТСЯ: параметр
`all_metrics` в теле сборщика не читается ни разу — ни у нас, ни в upstream.
Перевод этой строки в любую сторону не меняет ничего, и её мутация проходит
зелёной не от нехватки сторожа. Знание держит
`test_the_all_metrics_argument_never_reaches_the_prompt`.

ПРОВЕРКА СТОРОЖЕЙ ОТКАТОМ — мутации, прогнанные 20.08.2026, см. отчёт:
* убрать `extra["extraction_batch_size"]` из `extract_text` — красен
  `test_the_main_stage_is_sharded_now` (1 батч вместо 8);
* создать `batch_sem` ВНУТРИ `_run_stage2_batch` — красен
  `test_the_semaphore_holds_all_batches_back_together` (пик 8 вместо 4);
* снять `sorted(...)` со сборки результата — красен
  `test_arrival_order_does_not_reorder_the_result` и
  `test_every_reader_of_batch_results_sorts_by_batch_no`;
* сборщик строит блоки из `all_metrics` — красен
  `test_the_all_metrics_argument_never_reaches_the_prompt`, а состав промпта
  ещё цел; добавить к этому `all_metrics=extractable` в вызове — краснеет и
  `test_a_batch_prompt_carries_only_its_own_metrics`;
* `strategy="contiguous"` в основной стадии — красны
  `test_registry_neighbours_land_in_different_batches` и
  `test_both_splitters_name_the_strategy`;
* убрать `strategy=` из сегментного деления (умолчание то же) — красен
  `test_both_splitters_name_the_strategy`;
* вернуть литерал `strategy="spread"` в основное деление (20.08) — красен
  `test_both_splitters_name_the_strategy`: ручка перестала быть ручкой.
"""
from __future__ import annotations

import ast
import asyncio
import inspect
import json
import re
from pathlib import Path

from finengine.config import Settings, settings
from finengine.skills import extract_text as et
from finengine.skills import two_stage_extractor as ts
from finengine.skills.bulk_extractor.segments_context import (
    _SEGMENT_METRICS_BANK_RU,
    _SEGMENT_METRICS_IFRS_RU,
    _SEGMENT_METRICS_US_GAAP_EN,
)
from finengine.skills.metric_schema import registry_for_profile

_DOC = "===PAGE 1===\nОтчёт о финансовом положении\nИтого активы 1 000\n"

# Документ для прогонов через настоящий движок: страница и таблица нужны, чтобы
# первая стадия построила `ReportTable`, а не пустой список.
_DOC_WITH_TABLE = (
    "===PAGE 1===\n"
    "Отчёт о финансовом положении\n\n"
    "| Показатель | 2024 | 2023 |\n"
    "| --- | --- | --- |\n"
    "| Итого активы | 1 000 | 900 |\n"
)


def _inner_fn() -> ast.AsyncFunctionDef:
    """Дерево внутренней реализации: до её строк исполнение доходит только
    после полного прогона документа, поэтому часть букв проверяем разбором."""
    tree = ast.parse(Path(ts.__file__).read_text(encoding="utf-8"))
    return next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.AsyncFunctionDef)
        and n.name == "_extract_stream_two_stage_full_context"
    )


class _RunReport:
    """Что вернул настоящий прогон: диагностика движка и замеры вокруг него."""

    def __init__(self) -> None:
        self.diag: dict = {}
        self.peak_concurrency = 0
        self.asked_batches: list[list[str]] = []
        self.asked_prompts: list[str] = []
        self.evidence_enums: set[str] | None = None


def _run_two_stage(
    monkeypatch,
    profile_id: str = "ifrs_nf_ru",
    *,
    delay_first_longest: bool = False,
    gather_returns_completion_order: bool = False,
    evidence_enums: set[str] | None = None,
) -> _RunReport:
    """Настоящий прогон двухстадийной схемы с поддельным только шлюзом LLM.

    Подделывать промпты нельзя — именно разбиение состава и сборка результата
    здесь и проверяются. Поэтому подменён ровно один слой: `complete_with_usage`.
    Какие показатели спрошены в батче, узнаём у настоящего сборщика промпта.
    """
    registry = registry_for_profile(profile_id)
    enums = [str(m["enum"]).upper() for m in registry["extractable"]]
    report = _RunReport()
    report.evidence_enums = enums if evidence_enums is None else sorted(evidence_enums)
    targets: dict[str, list[str]] = {}
    live = {"now": 0}
    real_build = ts._build_main_extraction_prompt

    def _spy_build(**kw):
        text = real_build(**kw)
        targets[text] = [str(m["enum"]).upper() for m in kw["target_metrics"]]
        return text

    async def _fake_llm(*, prompt, system=None, model=None, response_format=None,
                        max_output=None, omit_max_output=None, **kw):
        asked = targets.get(prompt)
        if asked is None:
            payload = {"metrics": {
                enum: {"tables": [], "quotes": [{"page": 1, "text": "Итого активы 1 000"}]}
                for enum in report.evidence_enums
            }}
            return json.dumps(payload), {}
        serial = len(report.asked_batches)
        report.asked_batches.append(list(asked))
        report.asked_prompts.append(prompt)
        live["now"] += 1
        report.peak_concurrency = max(report.peak_concurrency, live["now"])
        # Первый пришедший запрос отвечает последним: батчи возвращаются
        # вразнобой, а не в порядке отправки.
        await asyncio.sleep(0.05 - serial * 0.005 if delay_first_longest else 0)
        live["now"] -= 1
        return json.dumps({
            enum: {"values": {"2024": 1000.0}, "unit_text": "тыс. руб.", "page": 1}
            for enum in asked
        }), {}

    monkeypatch.setattr(ts, "_build_main_extraction_prompt", _spy_build)
    monkeypatch.setattr(ts.llm, "complete_with_usage", _fake_llm)
    if gather_returns_completion_order:
        monkeypatch.setattr(ts, "asyncio", _AsyncioReturningCompletionOrder())

    async def _run() -> None:
        async for event in et.extract_stream_from_text(
            _DOC_WITH_TABLE,
            company_type="bank" if "bank" in profile_id else "non_financial",
            metric_enums=set(enums),
            registry_override=registry,
        ):
            if event.get("type") == "done":
                report.diag.update(event.get("_diag") or {})

    asyncio.run(_run())
    return report


class _AsyncioReturningCompletionOrder:
    """`asyncio`, чей `gather` отдаёт результаты в порядке ЗАВЕРШЕНИЯ.

    Настоящий `gather` сохраняет порядок отправки — и сборка выглядит
    упорядоченной даже без сортировки. Ровно так сторож и зеленел бы «потому
    что вход случайно приходил отсортированным». Здесь порядок ломается
    намеренно: сортировка в сборке становится единственным, что его чинит.
    """

    def __getattr__(self, name: str):
        return getattr(asyncio, name)

    async def gather(self, *coros, **kw):
        results = await asyncio.gather(*coros, **kw)
        return list(reversed(results))


def _stage_order(diag: dict) -> list[str]:
    return [
        str(request.get("stage") or "")
        for request in diag.get("llm_requests") or []
        if str(request.get("stage") or "").startswith("main_extract_batch_")
    ]


# ── Половина ПЕРВАЯ: цепочка значений цела — от настройки до семафора.


def test_default_is_more_than_one():
    """Единица вернула бы заведомо последовательное поведение."""
    assert settings.extract_batch_jobs > 1, "темп снова последовательный"


def test_the_batch_size_default_is_the_fighting_one():
    """Размер батча — боевое значение Finengine, а не осторожная выдумка."""
    assert settings.extract_batch_size == 10
    assert settings.extract_batch_attempts == 4
    assert settings.extract_segment_batch_size == 5


def test_the_pace_does_not_exceed_the_upstream_one():
    """Темп — тоже upstream'овский (4), а не наш прежний 6.

    Шестёрка стояла в конфиге с 17.08 и была МЁРТВОЙ: состав ячейки уходил
    одним батчем, сдерживать было нечего. С шардированием она оживает — и
    оживает на 50 процентов выше темпа, на котором батчи гоняет Finengine
    (`two_stage_batch_jobs` по умолчанию 4). Превышение проверенного темпа
    стоит не медленнее, а дороже: упёршись в лимит ключа, прогон получит
    ретраи и сорвавшиеся батчи, каждый ценой в `extract_batch_size` пустых
    показателей. Поднять обратно — переменной окружения, без пересборки.
    """
    assert settings.extract_batch_jobs == 4, (
        "темп разошёлся с upstream'овским: превышение обязано быть решением "
        "владельца, а не умолчанием")


def _kwargs_at_engine(monkeypatch) -> dict:
    """Аргументы, с которыми station entrypoint зовёт единственный движок."""
    seen: dict = {}

    async def _fake_engine(*args, **kw):  # noqa: ANN002, ANN003
        seen.update(kw)
        return
        yield  # генератор

    monkeypatch.setattr(ts, "extract_stream_finengine_dev_two_stage", _fake_engine)

    async def _run() -> None:
        async for _ in et.extract_stream_from_text(_DOC, company_type="non_financial"):
            pass

    asyncio.run(_run())
    return seen


def test_setting_reaches_the_two_stage_engine(monkeypatch):
    """Первый пролёт: настройки → вызов движка. Размер батча здесь главный:
    без него темп ниже нечему сдерживать."""
    kw = _kwargs_at_engine(monkeypatch)
    assert kw.get("extraction_batch_jobs") == settings.extract_batch_jobs, (
        "точка выбора схемы не передала темп")
    assert kw.get("extraction_batch_size") == settings.extract_batch_size, (
        "точка выбора схемы не передала размер батча — состав ячейки снова "
        "уйдёт ОДНИМ вызовом, а места семафора будут стоять над одной "
        "задачей")
    assert kw.get("extraction_batch_attempts") == settings.extract_batch_attempts
    assert kw.get("segment_extraction_batch_size") == settings.extract_segment_batch_size


def test_public_entry_forwards_them_inward(monkeypatch):
    """Второй пролёт: публичный вход → внутренняя реализация."""
    seen: dict = {}

    async def _fake_inner(*args, **kw):  # noqa: ANN002, ANN003
        seen.update(kw)
        return
        yield

    monkeypatch.setattr(ts, "_extract_stream_two_stage_full_context", _fake_inner)

    async def _run() -> None:
        async for _ in ts.extract_stream_finengine_dev_two_stage(
            "doc", "non_financial",
            extraction_batch_jobs=7,
            extraction_batch_size=11,
            extraction_batch_attempts=3,
            segment_extraction_batch_size=2,
        ):
            pass

    asyncio.run(_run())
    assert seen.get("extraction_batch_jobs") == 7
    assert seen.get("extraction_batch_size") == 11
    assert seen.get("extraction_batch_attempts") == 3
    assert seen.get("segment_extraction_batch_size") == 2


def test_value_is_what_the_semaphore_gets():
    """Последний пролёт — разбором исходника. Семафор строится ИЗ ТОГО ЖЕ ключа
    диагностики, в который кладётся параметр: разойдись они — ручка крутила бы
    отчёт, а не работу."""
    sem_args = [
        ast.unparse(node.args[0])
        for node in ast.walk(_inner_fn())
        if isinstance(node, ast.Call) and ast.unparse(node.func).endswith("Semaphore")
        and node.args
    ]
    assert sem_args, "семафор батчей исчез — проверять стало нечего"
    assert all("stage2_batch_jobs" in a for a in sem_args), (
        f"семафор строится не из настройки, а из {sem_args}")


def test_segment_stage_gets_the_same_pace():
    """Сегментная стадия — второй потребитель того же значения."""
    passed = [
        ast.unparse(kw.value)
        for node in ast.walk(_inner_fn())
        if isinstance(node, ast.Call)
        for kw in node.keywords
        if kw.arg == "extraction_batch_jobs"
    ]
    assert passed, "сегментной стадии темп не передают вовсе"
    assert all("extraction_batch_jobs" in p for p in passed), (
        f"сегментной стадии уходит не наш параметр, а {passed}")


def test_nonsense_values_do_not_deadlock():
    """`Semaphore(0)` заблокировал бы прогон навсегда.

    Проверяем НАШ исходник, а не арифметику питона: прежняя редакция сторожа
    утверждала `max(1, int(bad or 1)) >= 1` — тождество, истинное независимо от
    того, что написано в движке.
    """
    assigned = [
        ast.unparse(node.value)
        for node in ast.walk(_inner_fn())
        if isinstance(node, ast.Assign)
        and any(ast.unparse(t).endswith("['stage2_batch_jobs']") for t in node.targets)
    ]
    assert assigned, "ключ темпа больше не присваивается — семафор берёт что-то другое"
    assert all(a.startswith("max(1,") for a in assigned), (
        f"темп кладётся в диагностику без нижней границы: {assigned}")


# ── Половина ВТОРАЯ: сдерживать ЕСТЬ ЧТО. Ровно этого файл и не проверял.


def test_the_main_stage_is_sharded_now(monkeypatch):
    """ГЛАВНАЯ ПРАВДА ЭТОГО ФАЙЛА. Батчей больше одного — на настоящем прогоне.

    Считаем не арифметикой рядом, а тем числом, которое движок сам положил в
    диагностику: `_batch_items` можно проверить и в отрыве, но именно отрыв и
    скрывал, что до движка размер не доезжает.
    """
    report = _run_two_stage(monkeypatch)
    total = len(registry_for_profile("ifrs_nf_ru")["extractable"])
    expected = -(-total // settings.extract_batch_size)
    assert report.diag["stage2_batch_size"] == settings.extract_batch_size, (
        "размер батча не доехал до движка")
    assert report.diag["stage2_batch_count"] == expected > 1, (
        f"батчей {report.diag['stage2_batch_count']}, ждали {expected} "
        f"({total} показателей по {settings.extract_batch_size})")
    assert report.diag["stage2_batch_attempts"] == settings.extract_batch_attempts
    assert not report.diag["stage2_invalid_batches"], report.diag["stage2_invalid_batches"]


def test_a_bank_cell_is_sharded_too(monkeypatch):
    """Банковская ячейка — другой состав и другой промпт-профиль."""
    report = _run_two_stage(monkeypatch, "ifrs_bank_ru")
    total = len(registry_for_profile("ifrs_bank_ru")["extractable"])
    assert report.diag["stage2_batch_count"] == -(-total // settings.extract_batch_size) > 1


def test_no_metric_is_lost_or_asked_twice(monkeypatch):
    """Разбиение — раздел, а не выборка: каждый показатель ровно в одном батче."""
    report = _run_two_stage(monkeypatch)
    asked = [enum for batch in report.asked_batches for enum in batch]
    expected = [str(m["enum"]).upper() for m in registry_for_profile("ifrs_nf_ru")["extractable"]]
    assert sorted(asked) == sorted(expected), "показатели потерялись или задвоились"


def test_the_semaphore_holds_all_batches_back_together(monkeypatch):
    """Семафор ОДИН на все батчи, а не свой на каждый.

    Замеряем пиковую одновременность внутри поддельного шлюза: свой семафор на
    батч дал бы восемь одновременных вызовов вместо четырёх, и настройка темпа
    снова ничего бы не значила — уже по другой причине.
    """
    report = _run_two_stage(monkeypatch)
    assert report.diag["stage2_batch_count"] > settings.extract_batch_jobs, (
        "батчей не больше, чем мест в семафоре — измерять сдерживание нечем")
    assert report.peak_concurrency == settings.extract_batch_jobs, (
        f"одновременно шло {report.peak_concurrency} вызовов при темпе "
        f"{settings.extract_batch_jobs}")


def test_arrival_order_does_not_reorder_the_result(monkeypatch):
    """Батчи возвращаются вразнобой — сборка обязана этого не заметить.

    ОТРИЦАТЕЛЬНЫЙ КОНТРОЛЬ ВСТРОЕН В САМ ТЕСТ. Настоящий `gather` отдаёт
    результаты в порядке ОТПРАВКИ, поэтому без подмены сторож зеленел бы и с
    выброшенной сортировкой — «вход случайно пришёл отсортированным». Здесь
    `gather` подменён на отдающий порядок ЗАВЕРШЕНИЯ, и сортировка в сборке
    остаётся единственным, что чинит порядок.
    """
    report = _run_two_stage(
        monkeypatch, delay_first_longest=True, gather_returns_completion_order=True
    )
    stages = _stage_order(report.diag)
    assert len(stages) == report.diag["stage2_batch_count"] > 1
    assert stages == sorted(stages), (
        f"порядок прихода протёк в диагностику прогона: {stages}")
    assert not report.diag["stage2_invalid_batches"], report.diag["stage2_invalid_batches"]


def test_every_reader_of_batch_results_sorts_by_batch_no():
    """Сортировка покрывает ВСЕ места, где читаются итоги батчей.

    Мест два — основная стадия и сегментная, — и разъехаться они могут молча:
    вторая правится отдельно от первой.
    """
    tree = ast.parse(Path(ts.__file__).read_text(encoding="utf-8"))
    reads = [
        ast.unparse(node.iter)
        for node in ast.walk(tree)
        if isinstance(node, ast.For) and "batch_results" in ast.unparse(node.iter)
    ]
    assert len(reads) == 2, f"мест чтения итогов батчей стало {len(reads)}: {reads}"
    for read in reads:
        assert read.startswith("sorted(") and "batch_no" in read, (
            f"итоги батчей читаются без упорядочивания по batch_no: {read}")


def test_the_segment_stage_is_still_one_batch():
    """У сегментной стадии размер свой (5), но сегментных показателей самое
    большее пять — там батч по-прежнему один, и ускорять нечего.

    ⚠️ ПРОВЕРЯЮТСЯ ОБА УСЛОВИЯ, А НЕ ИТОГ. «Батч один» — не свойство кода, а
    следствие умолчания размера и трёх встроенных наборов; и то и другое
    меняется снаружи (см. шапку файла). Последний assert — отрицательный
    контроль: при составе шире размера стадия делится, то есть утверждение
    именно условное.
    """
    assert Settings().extract_segment_batch_size == 5, (
        "умолчание сегментного размера уехало — «батч один» держалось на нём")
    for scope in (_SEGMENT_METRICS_IFRS_RU, _SEGMENT_METRICS_US_GAAP_EN,
                  _SEGMENT_METRICS_BANK_RU):
        assert len(scope) <= settings.extract_segment_batch_size
        assert len(ts._batch_items(sorted(scope), settings.extract_segment_batch_size,
                                   strategy="spread")) == 1
    assert "segment_metrics_override" in inspect.signature(
        ts._extract_segment_breakdown_two_stage).parameters, (
        "состав сегментной стадии больше не задаётся снаружи — оговорка в "
        "шапке про условность утверждения устарела")
    wider = [f"M{idx}" for idx in range(settings.extract_segment_batch_size + 1)]
    assert len(ts._batch_items(wider, settings.extract_segment_batch_size,
                               strategy="spread")) > 1, (
        "состав шире размера, а батч всё равно один — тогда утверждение "
        "безусловно, и оговорка в шапке лишняя")


def test_the_names_in_the_header_are_real():
    """Имена функций движка, названные в шапке, существуют.

    Куплено разбором 20.08.2026: шапка называла `_extract_segments_two_stage`,
    которой в модуле нет вовсе (она `_extract_segment_breakdown_two_stage`).
    Неверное имя переживает переписывание абзаца молча — читатель идёт искать
    его в код и не находит.
    """
    named = set(re.findall(r"`(_extract_[a-z0-9_]+)`", __doc__ or ""))
    assert named, "в шапке не осталось ссылок на функции движка — сторож ослеп"
    missing = sorted(name for name in named if not hasattr(ts, name))
    assert not missing, f"шапка называет несуществующие функции: {missing}"


def test_a_metric_without_stage1_evidence_is_asked_anyway(monkeypatch):
    """ЦЕНА ШАРДИРОВАНИЯ, КОТОРУЮ ЗАМЕРИЛИ И ОТКАЗАЛИСЬ ПЛАТИТЬ (20.08.2026).

    До этого дня сторож назывался `..._is_not_asked` и держал ОБРАТНОЕ: ветка
    с размером батча спрашивала только показатели с уликами первой стадии.
    Замер показал, во что это обходится: два прогона ПСБ, отличавшиеся только
    шардами, — 38 показателей против 34, пять потеряны поимённо (среди них
    работающие активы и чистая процентная маржа). Выброс отвязан от размера
    батча и стал настройкой `FINENGINE_EXTRACT_ASK_WITHOUT_EVIDENCE` с
    умолчанием «спрашивать».

    Здесь остаётся ровно то, что касается ТЕМПА: безулиочные не исчезают из
    состава, значит батчей от них не меньше. Всё остальное про эту ветку — в
    `tests/test_a_metric_without_evidence_is_still_asked.py` (10 проверок,
    включая выключенную настройку).
    """
    enums = [str(m["enum"]).upper() for m in registry_for_profile("ifrs_nf_ru")["extractable"]]
    starved = set(enums[:3])
    report = _run_two_stage(monkeypatch, evidence_enums=set(enums) - starved)
    asked = {enum for batch in report.asked_batches for enum in batch}
    assert starved <= asked, (
        f"показатель без улик выброшен из состава: {sorted(starved - asked)}")
    assert set(report.diag["stage2_asked_without_evidence"]) == starved, (
        "безулиочные не названы в диагностике — цена решения невидима")
    assert report.diag["stage2_skipped_empty_evidence"] == [], (
        "кого-то всё-таки выбросили при умолчании «спрашивать»")


# ── Половина ТРЕТЬЯ: качество дробления. Что попадает в промпт батча и как
# состав делится между батчами — это единственные две буквы, которыми
# шардирование меняет не скорость, а РЕЗУЛЬТАТ. До 20.08 не держалась ни одна.


def _prompt_builder_fn() -> ast.FunctionDef:
    """Дерево сборщика промпта основной стадии."""
    tree = ast.parse(Path(ts.__file__).read_text(encoding="utf-8"))
    return next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "_build_main_extraction_prompt"
    )


def _metric_blocks(prompt: str) -> list[dict]:
    """Показатели, РАЗЛОЖЕННЫЕ модели в промпте: раздел `## Metric Blocks`.

    Читаем именно этот раздел, а не весь текст: имена чужих показателей
    встречаются в промпте законно — реестр ссылается на соседей прямо в
    подсказках (`ASSET_PURCHASES.hint` называет `COMBINED_ASSET_PURCHASES`,
    `ASSET_SALES_PROCEEDS.hint` — `PPE`). Поиском по словам сторож краснел бы
    на этих ссылках, а не на смене состава.
    """
    body = prompt.split("## Metric Blocks\n", 1)[1].split("\n\n## Rules\n", 1)[0]
    return json.loads(body)


def test_a_batch_prompt_carries_only_its_own_metrics(monkeypatch):
    """СОСТАВ ПРОМПТА — первая из двух букв качества, и держится он ЗДЕСЬ.

    ЦЕНА ДЕФЕКТА. Пока батч был один, модель видела в промпте весь реестр
    ячейки: соседние показатели работали разметкой отчёта («если это строка
    „Итого активы“, то рядом — обязательства»). С дроблением модели
    раскладывают ровно десять карточек своего батча, и это ровно та потеря, о
    которой спрошен владелец. Проверяем не аргумент вызова, а ТЕКСТ, ушедший в
    шлюз: аргумент можно оставить прежним и всё равно раздуть промпт изнутри
    сборщика — что и требуется, чтобы вернуть соседей.

    ОТРИЦАТЕЛЬНЫЙ КОНТРОЛЬ (прогнан 20.08): `target_metrics = list(extractable)`
    в `_run_stage2_batch` — красны 4 проверки, включая эту; и двухстрочная
    мутация «(а) как её просят у владельца» — сборщик строит блоки из
    `all_metrics` плюс `all_metrics=extractable` в вызове — красна ровно эта.
    """
    report = _run_two_stage(monkeypatch)
    assert len(report.asked_prompts) == report.diag["stage2_batch_count"] > 1
    for asked, prompt in zip(report.asked_batches, report.asked_prompts):
        laid_out = [str(block.get("enum") or "").upper() for block in _metric_blocks(prompt)]
        assert sorted(laid_out) == sorted(asked), (
            f"в промпт батча разложено {len(laid_out)} показателей вместо "
            f"{len(asked)} своих (лишние: "
            f"{sorted(set(laid_out) - set(asked))[:5]}). Если это сделано "
            "НАМЕРЕННО — вернуть модели соседей как подсказку, — сторож надо "
            "переписать и замерить по golden: это смена качества, а не темпа")


def test_the_all_metrics_argument_never_reaches_the_prompt():
    """ЛОВУШКА, НА КОТОРОЙ УЖЕ ОДИН РАЗ ПОСКОЛЬЗНУЛИСЬ (20.08.2026).

    Вызов сборщика содержит строку, которая ВЫГЛЯДИТ рубильником качества:
    `all_metrics=(target_metrics if extraction_batch_size is not None else
    extractable)`. Её и назвали «единственной строкой, отвечающей за качество
    при дроблении», и предложили владельцу как однострочный выбор. На деле
    параметр `all_metrics` в теле сборщика НЕ ЧИТАЕТСЯ ни разу (ни у нас, ни в
    upstream): промпт целиком строится из `target_metrics` — блоки, список
    целей, строки схемы. Значит перевод этой строки в любую сторону не меняет
    НИЧЕГО, и мутация её проходит зелёной не потому, что сторожа нет, а потому,
    что менять нечего.

    Сторож держит именно это знание. Покраснеет он, когда `all_metrics` начнут
    читать, — и тогда строка 2214 действительно станет рубильником, о котором
    можно спрашивать владельца. Правьте вместе с
    `test_a_batch_prompt_names_only_its_own_metrics` и меряйте по golden.
    """
    fn = _prompt_builder_fn()
    assert any(a.arg == "all_metrics" for a in fn.args.kwonlyargs + fn.args.args), (
        "параметр `all_metrics` исчез из сборщика — вызов на его месте "
        "передаёт значение в никуда, проверьте строку вызова")
    reads = [
        node.lineno for node in ast.walk(fn)
        if isinstance(node, ast.Name) and node.id == "all_metrics"
    ]
    assert not reads, (
        f"`all_metrics` теперь читается в сборщике промпта (строки {reads}) — "
        "ветвление на вызове ожило и стало сменой качества: модель снова "
        "видит весь реестр. Это решение владельца, а не побочный эффект")


def test_registry_neighbours_land_in_different_batches(monkeypatch):
    """СТРАТЕГИЯ ДЕЛЕНИЯ — вторая буква качества, и она не случайная.

    `_batch_items(..., strategy="spread")` раскладывает состав через один:
    соседи по реестру гарантированно уезжают в РАЗНЫЕ батчи. Это осознанный
    размен — так каждый батч получает вперемешку баланс, отчёт о прибылях и
    денежные потоки, и срыв одного батча дырявит таблицу равномерно, а не
    выедает целый раздел. Платим тем самым соседством, о котором спрошен
    владелец: при `contiguous` десять соседних строк отчёта шли бы в один
    промпт и подсказывали друг друга.

    Проверяем НАБЛЮДАЕМОЕ свойство, а не равенство вызову `_batch_items` —
    иначе сторож проверял бы код самим собой: в батче не должно быть двух
    показателей, стоящих в реестре подряд.

    ОТРИЦАТЕЛЬНЫЙ КОНТРОЛЬ (прогнан 20.08): `strategy="contiguous"` в
    `_extract_stream_two_stage_full_context` — красна эта проверка. До неё
    мутация проходила зелёной: у стратегии не было сторожа вовсе.
    """
    report = _run_two_stage(monkeypatch)
    enums = [str(m["enum"]).upper() for m in registry_for_profile("ifrs_nf_ru")["extractable"]]
    place = {enum: idx for idx, enum in enumerate(enums)}
    assert len(report.asked_batches) > 1, "батч один — делить нечего"
    for batch in report.asked_batches:
        places = sorted(place[enum] for enum in batch)
        adjacent = [
            (enums[a], enums[b]) for a, b in zip(places, places[1:]) if b - a == 1
        ]
        assert not adjacent, (
            f"соседи по реестру попали в один батч: {adjacent[:3]}. Стратегия "
            "деления сменилась на contiguous — это смена качества: батч "
            "перестал быть срезом всех разделов отчёта")


def test_both_splitters_name_the_strategy():
    """Стратегию называют ОБА места деления — и оба берут её из НАСТРОЙКИ.

    Мест два — основная стадия и сегментная. До 20.08 оба просили литеральный
    `spread`; теперь оба обязаны просить `extraction_batch_strategy`, тот самый
    параметр, что доезжает из `FINENGINE_EXTRACT_BATCH_STRATEGY`. Проверка
    именно на имя параметра, а не «хоть что-нибудь названо»: литерал в одном из
    двух мест — это ручка, которая крутит половину работы, и замер владельца
    после `=spread` показал бы смесь двух стратегий вместо прежнего деления.

    Веса требуем там же. Ветка `balanced` без них бросает `ValueError`, то есть
    забытый `weights=` — не тихая деградация, а падение прогона на боевом
    умолчании.
    """
    tree = ast.parse(Path(ts.__file__).read_text(encoding="utf-8"))
    calls = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call) and ast.unparse(node.func).endswith("_batch_items")
    ]
    assert len(calls) == 2, f"мест деления состава стало {len(calls)}, а было два"
    for node in calls:
        strategy = [kw for kw in node.keywords if kw.arg == "strategy"]
        assert strategy, (
            f"деление на строке {node.lineno} полагается на умолчание "
            "`_batch_items` — стратегия обязана быть названа в месте вызова")
        assert ast.unparse(strategy[0].value) == "extraction_batch_strategy", (
            f"деление на строке {node.lineno} просит "
            f"{ast.unparse(strategy[0].value)} вместо настройки "
            "`extraction_batch_strategy` — стратегия перестала быть ручкой")
        assert [kw for kw in node.keywords if kw.arg == "weights"], (
            f"деление на строке {node.lineno} идёт без весов — на боевом "
            "умолчании `balanced` это `ValueError` посреди прогона")
