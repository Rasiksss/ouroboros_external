"""Журнал прогонов хранит РАЗРЕЗ строки, а не только её значение.

ДЕФЕКТ, НАЙДЕННЫЙ 14.08.2026 при попытке измерить #163 по журналу.

`run_logger.log_run` получает СКЛЕЕННЫЙ список `[*плоские, *сегментные,
*групповые]` — так его собирает `extract_result.log_extract_run`, и это
намеренно: качество считается по всему, что извлеклось. Но проекция строки в
запись журнала роняла `dimension` и `segment`. В файле оставались строки,
неотличимые друг от друга: выручка компании и выручка одного сегмента лежали
рядом одинаковыми.

ЦЕНА НЕ В ЖУРНАЛЕ, А В БЕНЧМАРКЕ. `benchmarks/snap_bench.py::_events_from_run`
переигрывает журнал в события скорера и подставляет КАЖДОЙ строке
`"dimension": ""` — потому что взять настоящее значение неоткуда. То есть
EBITDA одного сегмента приезжает скореру как EBITDA КОМПАНИИ, рядом с
настоящей. Это единственный путь, которым мы снимаем бенчмарк с живых прогонов.

ЗАМЕР по 25 прогонам стенда (14.08): 403 строки из 2916 — сегментные и
групповые, это 13%; они есть в 19 прогонах из 25. У Русгидро 22 сегментных и 24
групповых строки на 122 плоских, у Башнефти 26 сегментных.

ПОЧЕМУ ЭТОГО НЕ ЛОВИЛ НИ ОДИН ТЕСТ. Существующие проверки журнала смотрят на
паспорт версий и на число записей — то есть на поля ЗАПИСИ, а не на поля СТРОКИ.
Потерю признака внутри строки не видел никто, а на экране она не проявляется
вовсе: пользователю таблицу рисует другой код, по своим спискам.

ПРОВЕРКА СТОРОЖА ОТКАТОМ: убрать `dimension`/`segment` из проекции в
`run_logger.log_run` — оба теста обязаны покраснеть.
"""
from __future__ import annotations

import json

from finengine.skills import run_logger


def _write_run(tmp_path, monkeypatch, metrics):
    # ⚠️ Журнал прогонов переехал в общее хранилище (26.08): подменяем каталог
    # данных, а не путь файла.
    from finengine.store import journals as _journals

    monkeypatch.setattr(run_logger.settings, "data_dir", tmp_path)
    monkeypatch.setattr(run_logger.settings, "journals_database_url", "")
    _journals._cached = None
    run_logger.log_run(
        doc_id="d1", filename="rep.pdf", arch="test", model_id=None,
        company_type="non_financial", company_name="ПАО Тест",
        period_current="2024", period_prev="2023", user_id=7,
        started_at=0.0, metrics=metrics,
        diag={"cell": "ifrs_nf_ru", "segments": 1, "group": 1})
    записи = run_logger.прочитать(run_logger.ЖУРНАЛ_ИЗВЛЕЧЕНИЙ)
    assert len(записи) == 1, f"в журнале {len(записи)} записей вместо одной"
    return записи[0]


def test_segment_row_stays_distinguishable_from_the_company_row(tmp_path, monkeypatch):
    """Три строки с ОДНИМ enum и разными значениями обязаны остаться различимы.

    Это и есть боевая форма записи: движок отдаёт показатель по компании, по
    сегменту и по составу группы. Без разреза в журнале лежат три «выручки»,
    и выбрать среди них компанийскую можно только угадав.
    """
    rec = _write_run(tmp_path, monkeypatch, [
        {"enum": "SALES_REVENUE", "value_current": 1000.0, "found": True},
        {"enum": "SALES_REVENUE", "value_current": 400.0, "found": True,
         "dimension": "operating_segment", "segment": "Вино"},
        {"enum": "SALES_REVENUE", "value_current": 600.0, "found": True,
         "dimension": "consolidated_group", "segment": "ООО «Дочка»"},
    ])
    rows = rec["metrics"]
    assert len(rows) == 3, "журнал потерял строки"

    flat = [m for m in rows if not m.get("dimension")]
    assert len(flat) == 1, (
        f"плоских строк {len(flat)} вместо одной — разрез не сохранён, и "
        f"бенчмарк посчитает сегменты как показатели компании"
    )
    assert flat[0]["value_current"] == 1000.0, "компанийское значение потеряно"

    by_dim = {m.get("dimension") for m in rows}
    assert by_dim == {"", "operating_segment", "consolidated_group"}
    seg = next(m for m in rows if m.get("dimension") == "operating_segment")
    assert seg["segment"] == "Вино", "имя сегмента потеряно — строка обезличена"


def test_a_plain_row_carries_an_empty_dimension_not_a_missing_one(tmp_path, monkeypatch):
    """У плоской строки признак ПУСТОЙ, а не отсутствующий.

    Читатели журнала (`snap_bench._events_from_run`) отличают «разреза нет» от
    «поля нет вовсе» только если поле есть всегда. Пропуск ключа вернул бы
    прежнюю неоднозначность через чёрный ход.
    """
    rec = _write_run(tmp_path, monkeypatch, [
        {"enum": "TOTAL_ASSETS", "value_current": 5000.0, "found": True},
    ])
    row = rec["metrics"][0]
    assert "dimension" in row and row["dimension"] == ""
    assert "segment" in row and row["segment"] == ""


# ── ВТОРАЯ ПОЛОВИНА ДЕФЕКТА (закрыта 19.08.2026) ────────────────────────────
#
# 14.08 починили ИСТОЧНИК: журнал стал хранить разрез. В шапке этого файла
# тогда же написали, что `snap_bench._events_from_run` подставляет каждой
# строке `dimension: ""` — «потому что взять настоящее значение неоткуда».
# Взять его стало откуда в тот же день, а потребителя не тронули. Пять дней
# спустя это стоило 17 пунктов F1.
#
# ЗАМЕР 19.08, Россельхозбанк, ОДИН И ТОТ ЖЕ прогон:
#     разрез стёрт   → P=63,50%  EFP=48  F1=74,04%
#     разрез передан → P=93,55%  EFP=4   F1=91,10%
# Движок отдал по FEE_AND_COMMISSION_INCOME десять правильно размеченных строк
# (плоскую, сегментный TOTAL и восемь разбивок по видам комиссий). Стерев
# разрез, обвязка предъявляла скореру десять безымянных предсказаний одного
# показателя за один период — девять уходили в «лишние».
#
# ПРОВЕРКА СТОРОЖА ОТКАТОМ: вернуть `"segment": ""`/`"dimension": ""` в
# `benchmarks/snap_bench.py::_events_from_run` — покраснеет
# `test_the_benchmark_harness_passes_the_dimension_through`.

def _events(tmp_path, metrics, **run_over):
    """Прогнать записи журнала через боевой писатель событий обвязки."""
    import importlib.util
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location(
        "snap_bench_under_test", root / "benchmarks" / "snap_bench.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    out = tmp_path / "events.ndjson"
    run = {"period_current": "Q1 2026", "period_prev": "Q1 2025",
           "metrics": metrics, **run_over}
    n, legacy = mod._events_from_run(run, out)
    rows = [json.loads(l) for l in out.read_text(encoding="utf-8").splitlines()]
    return [r for r in rows if r.get("type") == "metric"], n, legacy


def test_the_benchmark_harness_passes_the_dimension_through(tmp_path):
    """Разрез доезжает до скорера. Без этого замер меряет обвязку, а не движок.

    Ключ скорера — `dataset|документ|enum|период|сегмент`. С пустым сегментом
    строка разбивки сталкивается с плоской по одному ключу, и скорер, не имея
    чем их различить, объявляет лишними все, кроме одной.
    """
    events, _n, _legacy = _events(tmp_path, [
        {"enum": "FEE_AND_COMMISSION_INCOME", "value_current": 5835.0, "found": True,
         "dimension": "", "segment": "",
         "period_values": [{"period": "Q1 2026", "value": 5835.0}]},
        {"enum": "FEE_AND_COMMISSION_INCOME", "value_current": 1287.0, "found": True,
         "dimension": "product", "segment": "Комиссия по банковским картам",
         "period_values": [{"period": "Q1 2026", "value": 1287.0}]},
    ])
    assert len(events) == 2
    flat = [e for e in events if not e["dimension"]]
    seg = [e for e in events if e["dimension"]]
    assert len(flat) == 1 and len(seg) == 1, (
        "обвязка снова обезличила строки: скорер увидит два предсказания одного "
        "показателя за один период и одно из них объявит лишним")
    assert seg[0]["segment"] == "Комиссия по банковским картам", (
        "имя сегмента не доехало — ключ скорера снова совпадёт с плоской строкой")


def test_a_metric_that_was_not_found_is_not_called_an_old_journal(tmp_path):
    """«Периода нет» и «показателя не нашли» — разные вещи.

    Счётчик старых записей считал сюда и ненайденные показатели: у них нет
    значения, а значит и периода. На Россельхозбанке это дало предупреждение
    «47 строк без периода, оценка занижена до нуля» при полностью исправном
    свежем прогоне. Ложная тревога хуже молчания: она заставляет перепроверять
    верный замер вместо того, чтобы им пользоваться.
    """
    _events_out, _n, legacy = _events(tmp_path, [
        {"enum": "PPE", "value_current": None, "value_prev": None, "found": False},
        {"enum": "GOODWILL", "value_current": None, "value_prev": None, "found": False},
    ])
    assert legacy == 0, (
        f"ненайденные показатели посчитаны старой записью ({legacy}) — обвязка "
        f"будет ругаться на исправный прогон")

    # А вот это ДЕЙСТВИТЕЛЬНО старая запись: значение есть, периода нет.
    _events_out, _n, legacy = _events(tmp_path, [
        {"enum": "TOTAL_ASSETS", "value_current": 5000.0, "found": True},
    ])
    assert legacy == 1, "настоящая старая запись перестала распознаваться"
