"""Журнал помнит период каждой строки, а не только год прогона (#205 п.б).

ЗАЧЕМ ЭТОТ ФАЙЛ. 15.08 бенчмарк по Ак Барсу дал F1=0,00% — ноль истинных
попаданий на 110 строках. Я записал это как «движок пишет периодом ГОД, а не
дату отчётности». 16.08 сверил движок с журналом на пяти прогонах стенда:

    документ                    движок посчитал   журнал записал
    Абрау-Дюрсо (год)           FY 2024           2024
    Детский мир (полугодие)     6M 2023           2023
    Ак Барс (полугодие)         Q2 2025           2025
    БСПб (полугодие)            FY 2025           2025

Движок период ЗНАЛ. Голый год писал ЖУРНАЛ: проекция `run_logger.log_run`
роняла `period_values`, а `period_current` брался из `pick_report_years`, где
период сведён к году регуляркой `(20\\d{2})`.

ЦЕПОЧКА ДО НУЛЯ. `snap_bench._events_from_run` читает журнал → у каждой строки
период «2025» → скорер нормализует голый год в КОНЕЦ года, 31.12.2025 →
эталон полугодия ждёт 30.06.2025 → ключ сверки включает период → промах у
КАЖДОЙ строки. Ноль был оценкой ЖУРНАЛА, а не движка, и я целые сутки
разбирал не тот дефект.

ПОЧЕМУ НЕ ХВАТИТ ОДНОЙ ПАРЫ «ТЕКУЩИЙ/ПРОШЛЫЙ». У отчёта две оси периода:
балансовая строка живёт ДАТОЙ («31.12.2021»), строка ОПУ — ОТРЕЗКОМ
(«FY 2021»); это доказано в #205 п.(а). Пара лейблов не выражает обе, поэтому
период хранится ПОСТРОЧНО.

ПРОВЕРКА СТОРОЖА ОТКАТОМ: убрать `period_values` из проекции в
`run_logger.log_run` — покраснеет `test_interim_period_survives_the_journal`.
"""
from __future__ import annotations


import pytest

from finengine.skills import run_logger


@pytest.fixture
def journal(tmp_path, monkeypatch):
    """Журнал в отдельном хранилище: настоящее общее на стенд, писать в него нельзя.

    ⚠️ Раньше подменялся ПУТЬ ФАЙЛА (`_LOG_PATH`). Журналы прогонов переехали в
    общее хранилище 26.08, и подменять надо каталог данных: где именно лежит
    запись — дело хранилища, а не этого теста.
    """
    from finengine.config import settings
    from finengine.store import journals

    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr(settings, "journals_database_url", "")
    journals._cached = None
    yield tmp_path
    journals._cached = None


def _metric(enum: str, periods: list[tuple[str, float]], **extra) -> dict:
    return {
        "enum": enum, "found": True, "unit": "млн руб.",
        "value_current": periods[0][1] if periods else None,
        "value_prev": periods[1][1] if len(periods) > 1 else None,
        "period_values": [{"period": p, "value": v} for p, v in periods],
        **extra,
    }


def _write(journal, metrics: list[dict], **run_extra) -> dict:
    run_logger.log_run(
        doc_id="d1", filename="ak-bars-bank-Q2-2025.pdf", arch="aifa_station",
        model_id=None, company_type="bank", company_name="Ак Барс",
        # Ровно то, что кладёт `pick_report_years`: голый год.
        period_current="2025", period_prev="2024",
        user_id=1, started_at=0.0, metrics=metrics, **run_extra)
    записи = run_logger.прочитать(run_logger.ЖУРНАЛ_ИЗВЛЕЧЕНИЙ)
    assert len(записи) == 1, f"в журнале {len(записи)} записей вместо одной"
    return записи[0]


def test_interim_period_survives_the_journal(journal):
    """ГЛАВНОЕ: у полугодия в журнале остаётся 30.06, а не голый год."""
    row = _write(journal, [
        _metric("TOTAL_ASSETS", [("30.06.2025", 800.0), ("31.12.2024", 750.0)]),
        _metric("SALES_REVENUE", [("6M 2025", 60.0), ("6M 2024", 55.0)]),
    ])
    got = {m["enum"]: [pv["period"] for pv in m["period_values"]]
           for m in row["metrics"]}
    assert got["TOTAL_ASSETS"] == ["30.06.2025", "31.12.2024"], (
        "дата баланса не сохранилась — бенчмарк снова сведёт её к концу года "
        "и выдаст ноль вместо оценки")
    assert got["SALES_REVENUE"] == ["6M 2025", "6M 2024"], (
        "отрезок ОПУ не сохранился")


def test_both_axes_of_the_period_are_kept(journal):
    """Обе оси в одной записи: дата у запаса, отрезок у потока.

    Пара «текущий/прошлый» этого не выразила бы — ради этого период и хранится
    построчно.
    """
    row = _write(journal, [
        _metric("TOTAL_ASSETS", [("31.12.2021", 194818.0)]),
        _metric("SALES_REVENUE", [("FY 2021", 157968.0)]),
    ])
    periods = {m["enum"]: m["period_values"][0]["period"] for m in row["metrics"]}
    assert periods == {"TOTAL_ASSETS": "31.12.2021", "SALES_REVENUE": "FY 2021"}


def test_empty_values_do_not_reach_the_journal(journal):
    """Периоды без числа не храним: строка «период есть, значения нет» — шум.

    Скореру она ничего не даёт, а вес записи растит на каждом ненайденном
    показателе, которых в прогоне бывает треть.
    """
    row = _write(journal, [
        _metric("EBITDA", [("FY 2024", 100.0)]) | {
            "period_values": [{"period": "FY 2024", "value": 100.0},
                              {"period": "FY 2023", "value": None}]},
    ])
    assert [pv["period"] for pv in row["metrics"][0]["period_values"]] == ["FY 2024"]


def test_the_run_still_carries_its_legacy_fields(journal):
    """Старые поля на месте: их читают леджер и разбор инцидентов.

    Правка ДОБАВЛЯЕТ период, а не заменяет собой то, на чём стоят прежние
    читатели журнала.
    """
    row = _write(journal, [_metric("TOTAL_ASSETS", [("30.06.2025", 800.0)])])
    assert row["period_current"] == "2025" and row["period_prev"] == "2024"
    m = row["metrics"][0]
    assert m["value_current"] == 800.0
    assert set(m) >= {"enum", "value_current", "value_prev", "unit",
                      "is_calculated", "dimension", "segment", "period_values"}


def test_a_metric_without_periods_is_written_anyway(journal):
    """Показатель без period_values не теряется — просто с пустым списком.

    Такие приезжают с расчётного пути; выбросить их значило бы занизить
    знаменатель «найдено из всего».
    """
    row = _write(journal, [{"enum": "ROE", "value_current": 0.22,
                            "is_calculated": True}])
    assert row["metrics"][0]["period_values"] == []
    assert row["total"] == 1
