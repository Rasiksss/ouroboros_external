"""Оборванный прогон оставляет след в журнале (#205, прогон Совкомбанка).

ЗАЧЕМ ЭТОТ ФАЙЛ. 15.08.2026 владелец прогнал Совкомбанк, увидел таблицу на
экране — и спросил, почему прогона нет в журнале. Разбор по логу:

    20:46:33  последнее из 110 событий-показателей
    20:46:36  «канал отцеплён, вид=extract nonce=eccf1fbd — прогон дорабатывает»
    21:01:34  «bg mailbox idle nonce=eccf1fbd — закрываю»

То есть комната отдала ВСЕ показатели и замолчала, терминального события не
пришло, дренаж закрылся по простою. Таблица на экране БЫЛА: строки рисует
`on_event` по мере поступления. А журнал пишется только из терминального
события — и записи не появилось.

ЦЕНА. Журнал — источник для бенчмарка и для разбора инцидентов. Прогон,
который выдал 110 показателей и умер, невидим ровно там, где его разбирают:
`snap_bench.py` читает журнал, глазами тоже смотрят туда. Второй раз за два дня
прогон владельца оказалось нечем разобрать.

⚠️ ЧЕГО ПОЧИНКА НЕ ДЕЛАЕТ. Она НЕ выдаёт оборванный прогон за состоявшийся:
ни Excel, ни раскладки роли, ни события фронту, ни отметки «готово» на шине.
Поле `error` заполнено, `diag.aborted` — правда, ячейка и роль пустые (они
приезжают в терминальном событии, то есть ровно в том, чего не было).

ПРОВЕРКА СТОРОЖА ОТКАТОМ: убрать вызов `_abandon` из ветки простоя в
`station_run_sink.drain` — `test_silent_run_is_written_to_the_journal` обязан
покраснеть.
"""
from __future__ import annotations

import asyncio

import pytest

from finengine.skills import extract_result, station_run_sink


class _Journal:
    """Перехват записи журнала: проверяем ЧТО пишется, а не что файл создан."""

    def __init__(self) -> None:
        self.rows: list[dict] = []

    def log_run(self, **kw) -> None:
        self.rows.append(kw)


@pytest.fixture
def journal(monkeypatch):
    jr = _Journal()
    monkeypatch.setattr(extract_result.run_logger, "log_run", jr.log_run)
    monkeypatch.setattr(extract_result, "document_filename", lambda d: "sovcombank-2024.pdf")
    monkeypatch.setattr(station_run_sink.grounding_bus, "worker_doc", lambda n: "doc-1")
    monkeypatch.setattr(station_run_sink.grounding_bus, "run_failed",
                        lambda *a, **k: None)
    monkeypatch.setattr(station_run_sink.grounding_bus, "run_started",
                        lambda *a, **k: None)
    return jr


def _metric(enum: str, cur: float, prev: float) -> dict:
    return {"type": "metric", "enum": enum, "found": True,
            "value_current": cur, "value_prev": prev,
            "period_values": [{"period": "31.12.2024", "value": cur},
                              {"period": "31.12.2023", "value": prev}]}


def _drain_until_idle(queue, **kw) -> None:
    """Прогнать дренаж до ветки простоя.

    Отдельного таймаута простоя у дренажа нет: он ждёт следующее событие
    ровно столько, сколько осталось от времени жизни. Поэтому короткое
    `timeout` — это и есть «очередь замолчала», а не подкрутка теста.
    """
    asyncio.run(station_run_sink.drain(
        nonce="n1", queue=queue, owner_id=1, timeout=0.3,
        on_done=lambda run: None, on_failed=lambda msg: None, **kw))


def test_silent_run_is_written_to_the_journal(journal):
    """ГЛАВНОЕ: комната отдала показатели и затихла → строка в журнале ЕСТЬ."""
    q = asyncio.Queue()
    q.put_nowait({"type": "start", "company_name": "ПАО «Совкомбанк»"})
    q.put_nowait(_metric("TOTAL_ASSETS", 4_100_000, 3_300_000))
    q.put_nowait(_metric("SALES_REVENUE", 500_000, 420_000))
    # Терминального события НЕТ — очередь просто кончается.
    _drain_until_idle(q)

    assert len(journal.rows) == 1, (
        "оборванный прогон опять невидим в журнале — разбирать его будет нечем")
    row = journal.rows[0]
    assert row["filename"] == "sovcombank-2024.pdf"
    assert len(row["metrics"]) == 2
    assert row["error"], "прогон записан БЕЗ причины отказа — сойдёт за успешный"
    assert row["diag"]["aborted"] is True


def test_the_row_does_not_pretend_the_run_succeeded(journal):
    """Оборванный прогон не притворяется полным: ячейки и роли у него нет."""
    q = asyncio.Queue()
    q.put_nowait(_metric("TOTAL_ASSETS", 1.0, 2.0))
    _drain_until_idle(q)

    row = journal.rows[0]
    assert row["company_type"] == "", (
        "тип компании выдуман — он приезжает в терминальном событии, "
        "которого у этого прогона не было")
    assert row["versions"] == {}, "паспорт правил выдуман"


def test_periods_are_taken_from_the_rows_that_did_arrive(journal):
    """Периоды берутся из приехавших строк, а не из воздуха."""
    q = asyncio.Queue()
    q.put_nowait(_metric("TOTAL_ASSETS", 1.0, 2.0))
    _drain_until_idle(q)

    row = journal.rows[0]
    assert row["period_current"] == "2024"
    assert row["period_prev"] == "2023"


def test_a_run_without_a_single_row_is_not_written(journal):
    """Пустой прогон в журнал НЕ пишем: строка ни о чём — это шум.

    Нонсов взводится пул, комнат может не быть ни одной; писать по строке на
    каждый невзведённый нонс значило бы засорить журнал, по которому считают
    бенчмарк.
    """
    q = asyncio.Queue()
    q.put_nowait({"type": "status", "stage": "table_survey"})
    _drain_until_idle(q)

    assert journal.rows == []
