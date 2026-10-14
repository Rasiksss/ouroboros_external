"""Показатель, которому первая стадия не нашла улик, всё равно спрашивается.

ЦЕНА ДЕФЕКТА, ИЗМЕРЕННАЯ, А НЕ ПРЕДПОЛОЖЕННАЯ (20.08.2026). Включение
шардирования второй стадии включало ЗАОДНО и выброс безулиочных показателей:
одна ветка кода, два решения. Два прогона ПСБ (`ifrs_bank_ru`), отличавшиеся
ТОЛЬКО шардами — тот же код, тот же реестр, тот же документ, — дали 38
показателей без шардов против 34 с шардами. Пять потеряны поимённо:
EXTREMELY_LIQUID_ASSETS, INTERBANK_DEPOSITS, INTEREST_EARNING_ASSETS,
NET_INTEREST_MARGIN, NET_OTHER_RESERVES — работающие активы и чистая процентная
маржа, то есть не мелочь на краю реестра.

ПОЧЕМУ БЕЗУЛИОЧНЫЕ ПОДМЕШАНЫ К СОСЕДЯМ, А НЕ СОБРАНЫ В СВОЙ БАТЧ. Единственный
канал содержимого отчёта в промпте второй стадии — поле `evidence` внутри
карточки показателя, и собирает его `_evidence_bundle(target_metrics, ...)`
(`_build_main_extraction_prompt`); ни таблиц, ни текста страницы в промпте
больше нет (`finengine2_prompts.build_extraction_prompt`). Значит батч из ОДНИХ
безулиочных уехал бы к модели без единой таблицы отчёта: определения есть,
читать нечего, и найти он мог бы только выдуманное. До шардирования эти
показатели находились ровно потому, что ехали в одном промпте с соседями, у
которых улики были. Держит это знание
`test_a_starved_metric_looks_at_its_neighbours_tables` — он краснеет, если в
промпте батча с безулиочным не окажется ни одной чужой таблицы С ТЕКСТОМ.
Проверяется именно содержимое: поле `evidence` в карточке есть ВСЕГДА (это
`{"tables": [...], "quotes": [...]}`, пусть и с пустыми списками), поэтому
проверка на его наличие не проверяла бы ничего.

ЧЕМ ЭТО МОЖЕТ УХУДШИТЬ КАЧЕСТВО. Первая стадия отсеивала не только свои
промахи, но и показатели, которых в отчёте НЕТ. Спросив их, мы даём модели
шанс выдумать число по чужой таблице. Отследить это в замере можно поимённо:
`shard_losses(diag)['metrics_asked_without_evidence']` — список тех, у кого
улик не было; сколько из них подтвердилось по golden, а сколько оказалось
выдумкой, считается по этому списку без нового замерщика.

ГДЕ ЭТОТ СПИСОК ЛЕЖИТ, ЧТОБЫ ЗАМЕР ЕГО НАШЁЛ. Сверяет с golden снимок
бенчмарка (`benchmarks/snap_bench.py`), а читает он `data/extract_runs.jsonl`.
Значит журнал прогона — обязательное место, и до 20.08 его там НЕ БЫЛО:
`log_extract_run` собирал диагностику руками из шести полей, потерь среди них
не значилось. Имена ехали в stderr станции и в сводку хода, то есть к
человеку, а не к замеру, и план «проверить, не выдумывает ли модель» был
неисполним той обвязкой, которой его обещали исполнить. Цепочку держат
`test_the_run_journal_carries_the_starved_list` и
`test_the_live_path_hands_the_losses_to_the_journal`.

ПРОВЕРКА СТОРОЖЕЙ ОТКАТОМ — мутации, прогнанные 20.08.2026, см. отчёт.
"""
from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path

from finengine.config import Settings, settings
from finengine.skills import extract_result
from finengine.skills import extract_text as et
from finengine.skills import two_stage_extractor as ts
from finengine.skills.extract_result import shard_losses
from finengine.skills.metric_schema import registry_for_profile

_PROFILE = "ifrs_nf_ru"

# Страница с таблицей: первой стадии нужно построить `ReportTable`, а не пустой
# список — иначе улик не будет ни у кого и разделять станет нечего.
_DOC_WITH_TABLE = (
    "===PAGE 1===\n"
    "Отчёт о финансовом положении\n\n"
    "| Показатель | 2024 | 2023 |\n"
    "| --- | --- | --- |\n"
    "| Итого активы | 1 000 | 900 |\n"
)


class _RunReport:
    def __init__(self) -> None:
        self.diag: dict = {}
        self.asked_batches: list[list[str]] = []
        self.asked_prompts: list[str] = []


def _enums() -> list[str]:
    return [str(m["enum"]).upper() for m in registry_for_profile(_PROFILE)["extractable"]]


def _starved() -> set[str]:
    """Три показателя, которым поддельная первая стадия улик не даёт.

    Берём начало реестра, а не случайные: обогащение по синонимам
    (`_enrich_compression_with_alias_table_matches`) может ВЕРНУТЬ улику
    показателю, чьё имя совпало со строкой таблицы, — и тогда сторож проверял
    бы не то, что думает. Что эти трое остались голодными, проверяется в самих
    тестах равенством со списком из диагностики.
    """
    return set(_enums()[:3])


def _run(monkeypatch, *, batch_size: int | None, ask: bool) -> _RunReport:
    """Настоящий прогон двухстадийной схемы; подделан ровно шлюз LLM.

    Первая стадия отвечает уликами на все показатели, КРОМЕ `_starved()` — так
    воспроизводится её промах, ради которого всё и сделано.
    """
    registry = registry_for_profile(_PROFILE)
    enums = _enums()
    fed = [enum for enum in enums if enum not in _starved()]
    report = _RunReport()
    targets: dict[str, list[str]] = {}
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
                enum: {"tables": ["T001"],
                       "quotes": [{"page": 1, "text": "Итого активы 1 000"}]}
                for enum in fed
            }}), {}
        report.asked_batches.append(list(asked))
        report.asked_prompts.append(prompt)
        return json.dumps({
            enum: {"values": {"2024": 1000.0}, "unit_text": "тыс. руб.", "page": 1}
            for enum in asked
        }), {}

    monkeypatch.setattr(ts, "_build_main_extraction_prompt", _spy_build)
    monkeypatch.setattr(ts.llm, "complete_with_usage", _fake_llm)

    docs_dir = Path(settings.docs_dir)
    docs_dir.mkdir(parents=True, exist_ok=True)
    doc_id = f"noevidence-{uuid.uuid4().hex[:8]}"
    (docs_dir / f"{doc_id}.txt").write_text(_DOC_WITH_TABLE, encoding="utf-8")
    stub = et._StubIndex(doc_id, et._StubDoc(company_name="", filename=doc_id))

    async def _go() -> None:
        async for event in ts.extract_stream_finengine_dev_two_stage(
            doc_id,
            "non_financial",
            index=stub,
            metric_enums=set(enums),
            registry_override=registry,
            include_segments=False,
            include_consolidated_group=False,
            extraction_batch_size=batch_size,
            extraction_batch_jobs=settings.extract_batch_jobs,
            extraction_ask_without_evidence=ask,
        ):
            if event.get("type") == "done":
                report.diag.update(event.get("_diag") or {})

    asyncio.run(_go())
    assert report.asked_batches, "вторая стадия не спросила ничего — мерить нечего"
    return report


def _asked(report: _RunReport) -> set[str]:
    return {enum for batch in report.asked_batches for enum in batch}


def _metric_blocks(prompt: str) -> list[dict]:
    body = prompt.split("## Metric Blocks\n", 1)[1].split("\n\n## Rules\n", 1)[0]
    return json.loads(body)


# ── Настройка: живая, с нашим умолчанием, и доезжает до движка.


def test_asking_is_the_default_and_it_is_ours():
    """Умолчание НАШЕ, а не upstream'овское, и куплено замером (см. шапку).

    Значение читается у чистого `Settings()`, а не у общего объекта: тот мог
    быть подкручен переменной окружения стенда, и сторож зеленел бы на чужой
    настройке.
    """
    assert Settings().extract_ask_without_evidence is True, (
        "умолчание вернулось к upstream'овскому выбросу безулиочных — это "
        "решение владельца, а не побочный эффект правки")


def test_the_live_path_hands_the_setting_to_the_engine(monkeypatch):
    """Точка выбора схемы → движок. Без этого звена настройка крутила бы
    только `Settings`, а прогон шёл бы по умолчанию движка."""
    seen: dict = {}

    async def _fake_engine(*args, **kw):  # noqa: ANN002, ANN003
        seen.update(kw)
        return
        yield

    monkeypatch.setattr(ts, "extract_stream_finengine_dev_two_stage", _fake_engine)
    monkeypatch.setattr(settings, "extract_ask_without_evidence", False)

    async def _go() -> None:
        async for _ in et.extract_stream_from_text(_DOC_WITH_TABLE, company_type="non_financial"):
            pass

    asyncio.run(_go())
    assert seen.get("extraction_ask_without_evidence") is False, (
        f"боевой путь не передал настройку движку: {sorted(seen)}")


def test_the_public_entry_forwards_it_inward(monkeypatch):
    """Публичный вход → внутренняя реализация. Параметр объявить мало."""
    seen: dict = {}

    async def _fake_inner(*args, **kw):  # noqa: ANN002, ANN003
        seen.update(kw)
        return
        yield

    monkeypatch.setattr(ts, "_extract_stream_two_stage_full_context", _fake_inner)

    async def _go() -> None:
        async for _ in ts.extract_stream_finengine_dev_two_stage(
            "doc", "non_financial", extraction_ask_without_evidence=False,
        ):
            pass

    asyncio.run(_go())
    assert seen.get("extraction_ask_without_evidence") is False, (
        "публичный вход объявил параметр и не пробросил — ручка мертва")


# ── (а) Без шардов поведение прежнее: выбрасывать нечего и незачем.


def test_without_shards_nobody_is_dropped(monkeypatch):
    """Ветка без размера батча безулиочных не знает вовсе.

    Это и есть та схема, на которой пять показателей находились. Сторож держит
    её неизменной: правка касается ТОЛЬКО ветки шардирования.
    """
    report = _run(monkeypatch, batch_size=None, ask=True)
    assert report.diag["stage2_batch_count"] == 1
    assert _asked(report) == set(_enums()), "без шардов состав ячейки уходит целиком"
    assert report.diag["stage2_skipped_empty_evidence"] == []
    assert report.diag["stage2_asked_without_evidence"] == []


def test_without_shards_the_setting_changes_nothing(monkeypatch):
    """ОТРИЦАТЕЛЬНЫЙ КОНТРОЛЬ к предыдущему: выключенная настройка на этой
    ветке тоже никого не выбрасывает — иначе ручка чинила бы одну схему и
    ломала вторую."""
    report = _run(monkeypatch, batch_size=None, ask=False)
    assert _asked(report) == set(_enums())
    assert report.diag["stage2_skipped_empty_evidence"] == []


# ── (б) С шардами безулиочные едут в батч, а не в мусор.


def test_a_starved_metric_is_asked_with_shards_on(monkeypatch):
    """ГЛАВНАЯ ПРАВДА ФАЙЛА: спрошены все, включая безулиочных."""
    report = _run(monkeypatch, batch_size=settings.extract_batch_size, ask=True)
    starved = _starved()
    assert report.diag["stage2_batch_count"] > 1, "шарды не включились — проверять нечего"
    assert _asked(report) == set(_enums()), (
        f"не спрошены: {sorted(set(_enums()) - _asked(report))[:5]}")
    assert set(report.diag["stage2_asked_without_evidence"]) == starved, (
        "диагностика не назвала безулиочных — цена решения снова невидима")
    assert report.diag["stage2_skipped_empty_evidence"] == [], (
        "показатели выброшены и одновременно объявлены спрошенными")


def test_a_starved_metric_looks_at_its_neighbours_tables(monkeypatch):
    """ЗАЧЕМ ПОДМЕШИВАНИЕ, А НЕ ОТДЕЛЬНЫЙ БАТЧ.

    Своих улик у безулиочного нет по определению. Работает он только если в
    его промпте есть улики СОСЕДЕЙ — те самые таблицы, по которым он и
    находился до шардирования. Собери мы безулиочных в отдельный батч — здесь
    было бы пусто, и такой батч мог бы только выдумывать.

    ⚠️ ПРОВЕРЯЕТСЯ СОДЕРЖИМОЕ УЛИКИ, А НЕ НАЛИЧИЕ ПОЛЯ. До 20.08 здесь стояло
    `if b.get("evidence")` — и это было не проверкой вовсе:
    `_evidence_for_metric` возвращает `{"tables": [...], "quotes": [...]}`
    ВСЕГДА, даже когда оба списка пусты. Словарь с двумя пустыми списками
    истинен, значит условие выполнялось для каждой карточки, и assert
    вырождался в «в батче есть хоть один не-безулиочный». Мутация, на которой
    прежняя версия зеленела: заставить `_evidence_for_metric` отдавать
    `{"tables": [], "quotes": []}` всем — в промпте не остаётся ни таблицы, ни
    цитаты, а файл проходил целиком.
    """
    report = _run(monkeypatch, batch_size=settings.extract_batch_size, ask=True)
    starved = _starved()
    checked = 0
    for asked, prompt in zip(report.asked_batches, report.asked_prompts):
        if not (set(asked) & starved):
            continue
        checked += 1
        blocks = _metric_blocks(prompt)
        by_enum = {str(b.get("enum") or "").upper(): b for b in blocks}
        # Отрицательный контроль внутри проверки: у самого безулиочного улик
        # быть не должно — иначе «чужая таблица» проверялась бы на показателе,
        # которому первая стадия что-то всё-таки нашла.
        for enum in set(asked) & starved:
            own = by_enum[enum].get("evidence") or {}
            assert not own.get("tables") and not own.get("quotes"), (
                f"{enum} объявлен безулиочным, а улики в карточке есть: {own}")
        neighbour_tables = [
            table
            for enum, block in by_enum.items()
            if enum not in starved
            for table in ((block.get("evidence") or {}).get("tables") or [])
        ]
        assert neighbour_tables, (
            f"в промпте батча {asked[:3]}… нет ни одной чужой таблицы — "
            "безулиочному показателю не на что смотреть")
        # Ссылка на таблицу без её текста — тоже нечего читать: содержимое
        # отчёта приезжает в промпт полем `text` внутри ссылки
        # (`include_table_text=True` в `_build_main_extraction_prompt`).
        # Блоки разобраны ИЗ промпта, поэтому непустой `text` здесь и означает,
        # что строки таблицы физически уехали к модели.
        assert any((table.get("text") or "").strip() for table in neighbour_tables), (
            f"чужие таблицы в батче {asked[:3]}… названы ссылкой, но без строк — "
            "читать безулиочному по-прежнему нечего")
    assert checked, "безулиочные не попали ни в один промпт — проверять нечего"


# ── (в) Цена видна числом в исходе, а не только в диагностике.


def test_the_price_is_named_in_the_outcome(monkeypatch):
    """Счётчик потерь везёт число И имена: по именам замер отличит
    подтвердившиеся значения от выдуманных."""
    report = _run(monkeypatch, batch_size=settings.extract_batch_size, ask=True)
    losses = shard_losses(report.diag)
    assert losses["metrics_asked_without_evidence_total"] == len(_starved())
    assert set(losses["metrics_asked_without_evidence"]) == _starved()
    assert losses["metrics_unasked_total"] == 0, (
        "спрошенные безулиочные посчитаны ещё и как не спрошенные")


# ЗАКРЫТО (Срез 13.5): `test_the_station_prints_the_price` удалён. Он проверял,
# что `aifa-extract/scripts/extract.py` печатает `metrics_asked_without_evidence_
# total` в stderr — диагностику ИМЕННО прежнего прямого вызова
# `two_stage_extractor` станционным скриптом. Новый тонкий `extract.py` этот
# движок больше не вызывает вовсе (граф работает через `read.value`/`locate`,
# `graph_atoms.py`), а сама концепция «спросить без улик, подмешав к соседям» —
# по докстринге `read_value.py` — сознательно не перенесена в атомы ещё в
# Срезе 5a: показатель без единого кандидата туда не отправляется вовсе, без
# отдельного счётчика цены. Проверять нечего: у диагностики, которую защищал
# этот тест, нет эквивалента в текущей архитектуре, не по недосмотру, а по уже
# принятому в Срезе 5a решению.


# ── (в2) Поимённый список доезжает до ЗАМЕРА, а не только до человека.


def test_the_run_journal_carries_the_starved_list(monkeypatch):
    """Единственный способ отличить находку от выдумки — сверить имена с golden.

    Сверяет снимок бенчмарка (`benchmarks/snap_bench.py`), а читает он
    `data/extract_runs.jsonl`. Пока список ехал только в stderr станции и в
    сводку хода, план «проверить, не выдумывает ли модель числа безулиочным»
    был неисполним заявленной обвязкой: имён в журнале не было вовсе.
    """
    seen: dict = {}
    monkeypatch.setattr(extract_result.run_logger, "log_run",
                        lambda **kw: seen.update(kw))
    losses = {"batches": 8, "batches_failed": 0,
              "metrics_unasked_total": 0, "metrics_unasked": [],
              "metrics_asked_without_evidence_total": 2,
              "metrics_asked_without_evidence": ["NET_INTEREST_MARGIN",
                                                 "INTERBANK_DEPOSITS"]}
    extract_result.log_extract_run(
        arch="aifa_station", doc_id="d", filename="f", model_id=None,
        company_type="bank", company_name="c", main_year="2024",
        prev_year="2023", user_id=1, started_at=0.0,
        metrics=[{"enum": "NET_INCOME"}], segment_rows=[], group_rows=[],
        cell="ifrs_bank_ru", role="other", unit="", versions={},
        losses=losses)
    assert seen["diag"].get("losses") == losses, (
        "потери шардирования не доехали до журнала — замеру не с чем сверять "
        "имена спрошенных без улик")


def test_an_empty_losses_block_never_reaches_the_journal(monkeypatch):
    """ОТРИЦАТЕЛЬНЫЙ КОНТРОЛЬ. У схемы без батчей `shard_losses` отдаёт пустой
    словарь; запись «losses: {}» читалась бы как «батчи были, потерь нет»."""
    seen: dict = {}
    monkeypatch.setattr(extract_result.run_logger, "log_run",
                        lambda **kw: seen.update(kw))
    extract_result.log_extract_run(
        arch="finengine_dev_two_stage", doc_id="d", filename="f", model_id=None,
        company_type="bank", company_name="c", main_year="2024",
        prev_year="2023", user_id=1, started_at=0.0,
        metrics=[{"enum": "NET_INCOME"}], segment_rows=[], group_rows=[],
        cell="ifrs_bank_ru", role="other", unit="", versions={}, losses={})
    assert "losses" not in seen["diag"]


# ⚠️ УДАЛЁН Срезом 13.5.4: `test_the_live_path_hands_the_losses_to_the_journal`.
# Держал ПОЛОВИНУ цепочки в `station_turn.py` (`losses=run.result.get(…)`,
# передача из конверта СТАРОГО двухстадийного движка в
# `extract_result.finalize_extraction`) — та половина жила в физически
# недостижимом теле `_finish_run` (код после безусловного `return`),
# удалённом тем же срезом. Вторая половина (`finalize_extraction` сама
# принимает `losses` и отдаёт их журналу) остаётся рабочей и проверена
# другими тестами этого файла (`test_the_run_journal_carries_the_starved_
# list`, `test_an_empty_losses_block_never_reaches_the_journal`) — им
# конверт подаётся напрямую, не через недостижимый станционный путь.
# Тот же пробел уже зафиксирован в `Nedodelano.md` («Срез 13.5.4 —
# посекундный разрез/потери шардирования утеряны на каноническом пути»).


# ── (г) Настройка живая: выключил — вернулось поведение upstream.


def test_switching_it_off_brings_the_upstream_drop_back(monkeypatch):
    """ОТРИЦАТЕЛЬНЫЙ КОНТРОЛЬ КО ВСЕМУ ФАЙЛУ.

    Без этой проверки все предыдущие зеленели бы и с намертво вшитым «всегда
    спрашивать»: настройка выглядела бы живой, не будучи ею.
    """
    report = _run(monkeypatch, batch_size=settings.extract_batch_size, ask=False)
    starved = _starved()
    assert not (_asked(report) & starved), (
        "настройка выключена, а безулиочные всё равно уехали в промпт")
    assert set(report.diag["stage2_skipped_empty_evidence"]) == starved
    assert report.diag["stage2_asked_without_evidence"] == []
    assert set(shard_losses(report.diag)["metrics_unasked"]) == starved
