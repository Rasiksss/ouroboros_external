"""Канал, не увидевший ни одного события, закрывается «не запускался».

ДЕФЕКТ (найден 20.08.2026 разбором базы прогонов). Каналы взводятся на КАЖДЫЙ
ход ОБОИХ видов (`api/main.py`: `station_turn.arm` для извлечения и сразу за ним
для разбора), а запускают в ходе обычно один. `arm` пишет строку `running` в
`store/runs`, и если разбор в этом ходе не запускали, строка оставалась
`running` НАВСЕГДА: `disarm` гасил подписку и в базу не заглядывал.

В базе так накопилось 12 строк вида `analyze`, ВСЕ в состоянии `running`, ни
одна никогда не закрывалась.

ЦЕНА. По такой строке нельзя отличить «разбор не запускали» от «разбор завис» —
а это ровно тот вопрос, который владелец задал про два прогона Северстали. Хуже
того, шум растёт линейно от числа ходов: любой будущий счётчик «сколько прогонов
идёт прямо сейчас» врал бы тем сильнее, чем дольше живёт стенд.

ПОЧЕМУ ЧЕТВЁРТЫЙ СТАТУС, А НЕ ОДИН ИЗ ТРЁХ. `failed` означает, что прогон
сломался (его пошли бы разбирать), `done` — что он дал результат (его пошли бы
искать). Записать «не запускался» любым из них значит соврать, а оставить
`running` — соврать молча. Цена нового значения: его обязан знать каждый, кто
читает статус; на сегодня читателей два — ручки `/runs/*` и `latest_for_doc`, и
второй такие строки не увидит вовсе (у них нет `doc_id`: связь нонс→документ
ставится, только когда комната забирает текст).

ПРОВЕРКА СТОРОЖЕЙ ОТКАТОМ:
  * убрать `finish_best_effort` из ветки `not saw_life` в `station_turn.disarm`
    → `test_an_unused_analysis_channel_is_closed_as_never_started`;
  * записывать там `STATUS_FAILED` (или `STATUS_DONE`) вместо
    `STATUS_NOT_STARTED` → он же: «не запускался» и «упал» обязаны быть
    различимы;
  * закрывать «не запускался» и тот канал, который события ВИДЕЛ →
    `test_a_channel_that_saw_life_is_not_called_never_started`;
  * распространить «не запускался» на «Стоп» (убрать `not stopped` из условия)
    → `test_a_stopped_turn_is_not_called_never_started`;
  * снять правило про `STATUS_NOT_STARTED` в `runs._merged_status` ИЛИ убрать
    его из `runs._TERMINAL` →
    `test_a_late_terminal_beats_the_conclusion_drawn_from_silence`.
"""
from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import create_engine, text

OWNER = 7
NONCE = "5e5e5e5e5e5e5e5e5e5e5e5e5e5e5e5e"
GW_KEY = f"agent:aifa-station:openresponses:fe-{NONCE}"
DOC = "doc-severstal"

_START = {"type": "start", "company_name": "Северсталь", "total_extractable": 110,
          "period_current": "2024", "period_prev": "2023", "unit": "млн руб."}


@pytest.fixture
def db(tmp_path, monkeypatch):
    """Хранилище исходов на временной файловой базе — тем же кодом, что в проде."""
    from finengine.api import internal_docs, station_progress_ingest, station_turn
    from finengine.config import settings
    from finengine.skills import grounding_bus, run_logger
    from finengine.store import runs

    path = tmp_path / "runs.db"
    monkeypatch.setattr(settings, "runs_database_url", f"sqlite+pysqlite:///{path}")
    monkeypatch.setattr(runs, "_cached", None)
    monkeypatch.setattr(run_logger, "_STATION_EVENTS_PATH", tmp_path / "station_events.jsonl")
    monkeypatch.setattr(run_logger, "_LOG_PATH", tmp_path / "extract_runs.jsonl")
    monkeypatch.delenv("FINENGINE_OPENCLAW_GATEWAY_TOKEN", raising=False)

    station_progress_ingest._bus.clear()
    station_turn._detached.clear()
    internal_docs.reset_scopes()
    grounding_bus._worker_docs.clear()
    try:
        yield path
    finally:
        monkeypatch.setattr(runs, "_cached", None)
        station_progress_ingest._bus.clear()
        station_turn._detached.clear()
        grounding_bus._worker_docs.clear()


def _rows(path) -> dict[str, dict]:
    """Строки прогонов глазами наблюдателя: вид → статус и признак закрытия."""
    engine = create_engine(f"sqlite+pysqlite:///{path}")
    with engine.connect() as conn:
        return {r["kind"]: dict(r) for r in conn.execute(text(
            "SELECT nonce, kind, status, finished_at, doc_id FROM station_run")).mappings()}


def _turn_that_only_extracted(*, feed_start: bool = True, stopped: bool = False) -> None:
    """Ход как в бою: взведены ОБА канала, запущено только извлечение."""
    from finengine.api import station_progress_ingest as ingest
    from finengine.api import station_turn

    async def scenario() -> None:
        cell: dict = {}
        station_turn.arm(nonce=NONCE, gw_key=GW_KEY, doc_ids=[DOC], owner_id=OWNER,
                         project_id="p1", cell=cell)
        station_turn.arm(nonce=station_turn.analysis_nonce_of(NONCE), gw_key=GW_KEY,
                         doc_ids=[DOC], owner_id=OWNER, project_id="p1", cell=cell,
                         kind=station_turn.KIND_ANALYZE)
        extract = station_turn.channels_of(cell)[station_turn.KIND_EXTRACT]
        if feed_start:
            await ingest.ingest(ingest.ProgressBatch(nonce=NONCE, events=[_START]),
                                authorization="")
            for _ in range(50):
                await asyncio.sleep(0)
                if extract.saw_life:
                    break
            assert extract.saw_life, "дренаж не забрал старт — сценарий проверяет не то"
        station_turn.disarm(cell, stopped=stopped)

    asyncio.run(scenario())


def test_an_unused_analysis_channel_is_closed_as_never_started(db):
    """Разбор в ходе не запускали — строка закрыта и НЕ похожа ни на «идёт», ни на «упал»."""
    from finengine.store import runs

    _turn_that_only_extracted()
    stored = _rows(db)

    assert "analyze" in stored, "канал разбора не завёл строки — сценарий не тот"
    analyze = stored["analyze"]
    assert analyze["status"] == runs.STATUS_NOT_STARTED, (
        f"канал разбора остался в состоянии {analyze['status']!r}: «разбор не "
        "запускали» неотличимо от «разбор завис» — именно этот вопрос владелец "
        "задал про два прогона Северстали"
    )
    assert analyze["status"] not in (runs.STATUS_RUNNING, runs.STATUS_FAILED)
    assert analyze["finished_at"] > 0, (
        "строка закрыта по статусу, но не по времени — «последний прогон» её "
        "по-прежнему считает незавершённой"
    )
    assert not analyze["doc_id"], (
        "у невостребованного канала появился документ — такая строка полезет в "
        "выдачу «последний прогон по документу»"
    )


def test_a_channel_that_saw_life_is_not_called_never_started(db):
    """Извлечение шло — его строку «не запускался» закрывать нельзя.

    Иначе вывод из тишины затирал бы работающий прогон, и вместо одного
    неразличимого состояния мы получили бы два.
    """
    from finengine.store import runs

    _turn_that_only_extracted()

    extract = _rows(db)["extract"]
    assert extract["status"] != runs.STATUS_NOT_STARTED, (
        "прогон, приславший события, объявлен незапускавшимся — он отцеплен и "
        "продолжает работать"
    )
    assert extract["status"] == runs.STATUS_RUNNING
    assert extract["finished_at"] == 0, "идущий прогон помечен завершённым"


def test_a_late_terminal_beats_the_conclusion_drawn_from_silence(db):
    """Прогон переживает ход: приехавший исход сильнее вывода из тишины.

    «Не запускался» — вывод из ОТСУТСТВИЯ событий к концу хода. Если события
    приедут позже (отцепка, #276), они обязаны победить: иначе доставленный
    результат хоронится тем, что его в своё время не увидели.
    """
    from finengine.store import runs

    _turn_that_only_extracted(feed_start=False)
    assert _rows(db)["extract"]["status"] == runs.STATUS_NOT_STARTED

    # Сперва в другую сторону: «идёт» закрытую строку НЕ воскрешает. Без этого
    # четвёртый статус был бы терминальным только на словах.
    runs.finish_best_effort(NONCE, status=runs.STATUS_RUNNING)
    assert _rows(db)["extract"]["status"] == runs.STATUS_NOT_STARTED, (
        "закрытая строка снова «идёт» — вернулось ровно то состояние, ради "
        "устранения которого заведён статус")

    runs.finish_best_effort(NONCE, status=runs.STATUS_DONE,
                            payload={"metrics_found": 96}, finished_at=1)

    assert _rows(db)["extract"]["status"] == runs.STATUS_DONE, (
        "терминал не смог закрыть строку, объявленную незапускавшейся — "
        "результат прогона похоронен")

    # И обратно: вывод из тишины НЕ смеет понизить приехавший исход.
    runs.finish_best_effort(NONCE, status=runs.STATUS_NOT_STARTED, finished_at=2)
    assert _rows(db)["extract"]["status"] == runs.STATUS_DONE


def test_a_stopped_turn_is_not_called_never_started(db):
    """«Стоп» — не «не запускался», даже если событий ещё не было.

    Кнопка гасит канал в любой момент, в том числе до первого события уже
    запущенного прогона: комната работает, события просто не доехали. Назвать
    такой прогон незапускавшимся значит завести вторую ложь взамен первой. Чем
    закрывать оборванный «Стопом» прогон — отдельный вопрос, и этот сторож
    держит границу, а не решает его.
    """
    from finengine.store import runs

    _turn_that_only_extracted(feed_start=False, stopped=True)

    for kind, row in _rows(db).items():
        assert row["status"] != runs.STATUS_NOT_STARTED, (
            f"{kind}: прогон, от которого пользователь отказался кнопкой, "
            "объявлен незапускавшимся — комната в этот момент могла работать"
        )
