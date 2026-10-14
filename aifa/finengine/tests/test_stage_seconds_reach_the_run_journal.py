"""Посекундный разрез стадий доезжает от движка до журнала прогона (Б7, 19.08.2026).

ЗАЧЕМ. Оба движка считают время КАЖДОГО вызова модели и кладут его в
терминальное событие (`done['_diag']['llm_requests'][*]['elapsed_s']`). На боевом
пути его не забирал никто: станционный скрипт до `_diag` не дотрагивался,
замерщик A/B выбрасывал. Из-за этого «сколько стоит стадия сжатия» упиралось в
переизобретение замера, а предложения по ускорению обосновывались оценками.

Проверяется ЦЕПОЧКА, а не наличие поля: диагностика движка → конверт прогона →
терминальное событие → сток → журнал. Ровно на цепочках этот проект уже дважды
терял правки (#252, #253): значение объявлено, а до потребителя не доехало.

⚠️ ГДЕ ПРОВЕРКА ИДЁТ ПО ИСХОДНИКУ — исходник берётся ЧЕРЕЗ `ast.unparse`, то
есть без комментариев. Иначе сторож зеленел бы от объяснительной записки: такое
в этом проекте уже ловили.
"""
from __future__ import annotations

import ast
from pathlib import Path

from finengine.skills import extract_result, station_run_sink


def _code(path: Path) -> str:
    """Исходник без комментариев: их снимает разбор в дерево и обратная сборка."""
    return ast.unparse(ast.parse(path.read_text(encoding="utf-8")))


_DIAG = {
    "llm_requests": [
        {"stage": "main_compression", "elapsed_s": 214.3, "tokens_in": 136484,
         "tokens_out": 7199, "tokens_cached": 0, "generation_id": "gen-1"},
        {"stage": "main_extract_1", "elapsed_s": 88.1, "tokens_in": 41000,
         "tokens_out": 12000, "tokens_cached": 128, "generation_id": "gen-2"},
    ],
    "calc_s": 0.14,
    "wall_s": 407.9,
    "llm_s": 302.4,
}


def test_every_stage_keeps_its_seconds():
    """Главное свойство: сколько вызовов было — столько записей со временем."""
    stages = extract_result.run_stages(_DIAG)
    assert [c["stage"] for c in stages["calls"]] == ["main_compression", "main_extract_1"]
    assert [c["elapsed_s"] for c in stages["calls"]] == [214.3, 88.1]
    assert [c["tokens_in"] for c in stages["calls"]] == [136484, 41000]
    assert [c["tokens_out"] for c in stages["calls"]] == [7199, 12000]


def test_every_stage_keeps_the_moment_it_started():
    """Начало вызова едет вместе с его длиной — иначе перекрытие не разложить.

    С 20.08 сегментное сжатие идёт ОДНОВРЕМЕННО с основным, и сумма `elapsed_s`
    больше не равна времени прогона. Пока стадии стояли в очередь, равенство
    было диагнозом; теперь неравенство говорит лишь «что-то перекрылось».
    Отрезок вызова — это `started_at` + `elapsed_s`, и только по отрезкам видно,
    ЧТО с чем перекрылось. Без начала следующий замер скорости мерить нечем.
    """
    stages = extract_result.run_stages({
        "llm_requests": [
            {"stage": "main_compression", "started_at": 1000.0, "elapsed_s": 229.0},
            {"stage": "segment_compression", "started_at": 1000.2, "elapsed_s": 83.0},
        ],
    })
    assert [c["started_at"] for c in stages["calls"]] == [1000.0, 1000.2], (
        f"время старта вызовов не доехало до разреза: {stages['calls']}")


def test_totals_travel_but_absent_ones_are_not_invented():
    """У полноконтекстной схемы `wall_s`/`llm_s` нет вовсе. Пустое поле в
    журнале читалось бы как «стадия заняла ноль»."""
    full = extract_result.run_stages(_DIAG)
    assert (full["wall_s"], full["llm_s"], full["calc_s"]) == (407.9, 302.4, 0.14)
    lean = extract_result.run_stages({"llm_requests": [], "calc_s": 0.5})
    assert lean["calc_s"] == 0.5
    assert "wall_s" not in lean and "llm_s" not in lean


def test_no_diag_is_not_a_crash():
    """Старая полка станции разреза не присылает — прогон обязан дойти до конца."""
    assert extract_result.run_stages(None) == {"calls": []}
    assert extract_result.run_stages({}) == {"calls": []}


def test_journal_records_the_stages(monkeypatch):
    """Последний пролёт: `log_extract_run` → строка журнала."""
    seen: dict = {}

    def _fake_log_run(**kw):
        seen.update(kw)

    monkeypatch.setattr(extract_result.run_logger, "log_run", _fake_log_run)
    stages = extract_result.run_stages(_DIAG)
    extract_result.log_extract_run(
        arch="aifa_station", doc_id="d", filename="f", model_id=None,
        company_type="bank", company_name="c", main_year="2024", prev_year="2023",
        user_id=1, started_at=0.0, metrics=[{"enum": "REVENUE"}], segment_rows=[],
        group_rows=[], cell="ifrs_bank_ru", role="other", unit="", versions={},
        stages=stages)
    assert seen["diag"].get("stages") == stages, (
        "секунды стадий не доехали до журнала — мерить снова нечем")


def test_absent_stages_do_not_become_an_empty_record(monkeypatch):
    """`stages: {}` в журнале читается как «стадий не было», а означает
    «прогон пришёл со старой полки»."""
    seen: dict = {}

    def _fake_log_run(**kw):
        seen.update(kw)

    monkeypatch.setattr(extract_result.run_logger, "log_run", _fake_log_run)
    extract_result.log_extract_run(
        arch="aifa_station", doc_id="d", filename="f", model_id=None,
        company_type="bank", company_name="c", main_year="2024", prev_year="2023",
        user_id=1, started_at=0.0, metrics=[{"enum": "REVENUE"}], segment_rows=[],
        group_rows=[], cell="ifrs_bank_ru", role="other", unit="", versions={})
    assert "stages" not in seen["diag"]


def test_the_sink_keeps_stages_out_of_the_prompt():
    """Сводка уезжает в промпт следующего хода. Секунды там означали бы, что
    модель начнёт пересказывать их пользователю — а это внутренняя величина."""
    assert "stages" in station_run_sink._HEAVY_FOR_SUMMARY, (
        "разрез стадий уехал в сводку хода, то есть в промпт")
    code = _code(Path(station_run_sink.__file__))
    assert "if k not in _HEAVY_FOR_SUMMARY" in code, (
        "сводка строится не по этому списку — исключение перестало работать")


# ⚠️ УДАЛЕНЫ Срезом 13.5.4: `test_the_turn_takes_stages_from_the_heavy_part`,
# `test_the_station_puts_stages_into_the_envelope`,
# `test_seconds_are_not_printed_to_the_model`.
#
# Все три проверяли посрочный разрез стадий (`run_stages(engine_diag)`,
# `_diag`/`stage2_*`) на ПУТИ СТАРОГО двухстадийного движка: конверт
# `run_block`/`envelope`, которые эти тесты искали текстом, — форма
# `station-skills/aifa-extract/scripts/extract.py`, снесённая переводом
# станции на тонкую прослойку над `CaseRuntime`/`PlanGraph` (Срез 13.5.1–3,
# ДО этой сессии). `station_turn._finish_run`'s dead body, откуда
# `test_the_turn_takes_stages_from_the_heavy_part` брало вторую половину
# цепочки, физически удалён этим же срезом как недостижимый код.
#
# `extract_result.run_stages` САМА ПО СЕБЕ остаётся рабочей чистой функцией
# (тесты выше, до этой отметки, зелёные и не тронуты) — удалён только мост
# «диагностика движка → конверт → журнал», которого для канонического пути
# не существует физически.
#
# РЕАЛЬНЫЙ ПРОБЕЛ, НЕ ЗАМОЛЧАННЫЙ: посекундный разрез вызовов модели
# (сколько заняла каждая стадия) не имеет аналога на каноническом пути —
# graph atoms не эмитят такую диагностику наружу станционного скрипта вовсе.
# Зафиксировано в `Nedodelano.md` («Срез 13.5.4 — посекундный разрез стадий
# утерян на каноническом пути») вместе с пробелом видимости потерь
# шардирования — оба требуют одной и той же работы: инструментация atoms
# `read.value`/`locate`, вне мандата этого среза.
