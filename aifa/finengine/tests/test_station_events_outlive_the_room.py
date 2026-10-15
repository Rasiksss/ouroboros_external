"""Сырьё хода станции переживает комнату (#249).

ЗАЧЕМ ЭТОТ ФАЙЛ. Комната станции живёт ~18 минут и уносит с собой канал
прогресса, вывод скриптов и шаг, на котором упало. Три журнала прогонов
(`extract_runs`, `analysis_runs`, `chat_runs`) пишутся ТОЛЬКО на успехе — их
зовёт `finalize_*`. Значит про удачный ход известно всё, а про упавший —
ничего, и это ровно наоборот тому, что нужно.

15.08 это дважды стоило разбора: ход «Ак Барс» упал на шаге «собрать карточку
анализа», к моменту разбора комната истекла, и причина отказа оказалась
невосстановимой. Владелец потребовал сначала завести сохранение прогонов на
бэкенде, и он прав: без этого любой дефект чинится только «поймай его живым».

ПРОВЕРКА СТОРОЖА ОТКАТОМ: убрать вызов `log_station_events` из
`api/station_progress_ingest.ingest` — первый тест покраснеет.
"""
from __future__ import annotations


import pytest

from finengine.api import station_progress_ingest as ingest_mod
from finengine.skills import run_logger


@pytest.fixture
def sink(tmp_path, monkeypatch):
    """Журнал в отдельном хранилище: настоящее общее на стенд, писать нельзя.

    ⚠️ Сырьё станции переехало в общее хранилище и лежит теперь ПО НОНСУ, а не
    одной кучей: разбор одного хода стал одним чтением вместо перебора файла.
    """
    from finengine.config import settings
    from finengine.store import journals

    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr(settings, "journals_database_url", "")
    journals._cached = None
    yield tmp_path
    journals._cached = None


def _lines(_sink, nonce: str = "n-dead"):
    return run_logger.прочитать(run_logger.СЫРЬЁ_СТАНЦИИ, nonce)


@pytest.mark.asyncio
async def test_events_survive_when_nobody_is_listening(sink):
    """ГЛАВНОЕ: подписчика нет — событие всё равно на диске.

    Это и есть случай упавшего хода: хендлер уже закончился, доставлять некому,
    и раньше событие исчезало совсем.
    """
    batch = ingest_mod.ProgressBatch(nonce="n-dead", events=[
        {"type": "plan_step", "index": 5, "status": "error", "note": "карточка не собрана"},
        {"type": "error", "message": "station model-bridge error: 503"},
    ])
    res = await ingest_mod.ingest(batch, authorization="")
    assert res["reason"] == "no_consumer", "тест проверяет не тот случай"

    got = _lines(sink)
    assert len(got) == 2, f"сырьё не осело: {got}"
    assert got[0]["nonce"] == "n-dead"
    assert got[0]["event"]["status"] == "error"
    assert "503" in got[1]["event"]["message"], (
        "текст отказа станции потерян — по такой записи дефект не найти")


@pytest.mark.asyncio
async def test_sink_does_not_break_delivery(sink):
    """Журнал не вправе помешать ходу, ради которого пишется."""
    q = ingest_mod.register("n-live")
    try:
        batch = ingest_mod.ProgressBatch(nonce="n-live", events=[{"type": "metric", "enum": "X"}])
        res = await ingest_mod.ingest(batch, authorization="")
        assert res["accepted"] == 1, "событие не доставлено подписчику"
        assert q.get_nowait()["enum"] == "X"
    finally:
        ingest_mod.unregister("n-live", q)
    assert len(_lines(sink, "n-live")) == 1, "и при живом подписчике запись обязана быть"


@pytest.mark.asyncio
async def test_unwritable_sink_never_kills_the_run(tmp_path, monkeypatch):
    """Отказ ЗАПИСИ проглатывается: ход дороже журнала.

    ⚠️ Отказ имитируем НЕПИШУЩИМ ХРАНИЛИЩЕМ, а не подменой самой функции.
    Подмена проверяла бы обещание, которого код не даёт: сторож стоит внутри
    записи, и заменить её целиком значит выкинуть сторож вместе с ней. Первая
    редакция этого теста делала именно так и краснела — на сценарии, который в
    бою невозможен.

    ⚠️ ПОСЛЕ ПЕРЕЕЗДА ЖУРНАЛА (26.08) здесь подменялся ПУТЬ ФАЙЛА, которого
    запись больше не касается: тест зеленел, ничего не проверяя. Теперь
    непишущим делается сам каталог данных.

    ⚠️ СТОРОЖЕЙ ТЕПЕРЬ ДВА — в `store/journals.append` и здесь, в
    `log_station_events`. Снятие ОДНОГО тест не краснит, и это честно: пока
    цел второй, свойство держится. Краснеет снятие обоих — проверено.
    """
    # Файл вместо каталога: mkdir по такому пути падает как настоящий отказ ФС
    # (ровно то, что бывает при полном диске или правах).
    from finengine.config import settings
    from finengine.store import journals

    blocker = tmp_path / "blocked"
    blocker.write_text("не каталог", encoding="utf-8")
    monkeypatch.setattr(settings, "data_dir", blocker / "sub")
    monkeypatch.setattr(settings, "journals_database_url", "")
    journals._cached = None
    q = ingest_mod.register("n-ok")
    try:
        batch = ingest_mod.ProgressBatch(nonce="n-ok", events=[{"type": "metric"}])
        # Не должно бросить наружу.
        res = await ingest_mod.ingest(batch, authorization="")
        assert res["accepted"] == 1
    finally:
        ingest_mod.unregister("n-ok", q)


def test_sink_is_a_separate_file_from_run_summaries():
    """Поток событий и итоги прогонов не смешиваются в один файл."""
    assert run_logger.station_events_path() != run_logger.runs_path()
    assert run_logger.station_events_path() != run_logger.chat_runs_path()
