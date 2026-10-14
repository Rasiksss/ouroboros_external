"""Ошибка станции остаётся в логе сервера, а не только на экране.

ЗАЧЕМ ЭТОТ ФАЙЛ (09.08.2026, куплен живым прогоном по Лукойлу). Пользователь
увидел в плане красный шаг «Прочитать отчёт и собрать показатели (77 шт.) —
ОШИБКА, 11:22». В логе бэкенда за полтора часа не было ни одной строки об этом:
ручка `/internal/station-progress` проносила события в очередь SSE и молчала.
Разбирать было нечего — станция исключения не бросила, ход закрылся как
успешный, и единственным следом ошибки остался чип на экране пользователя.

Логировать надо ДО проверки подписчика: если хендлер уже завершился, доставлять
событие некому — и именно тогда оно исчезает совсем.

ПРОВЕРКА СТОРОЖА ОТКАТОМ: убрать цикл логирования из `ingest` — тесты обязаны
покраснеть.
"""
from __future__ import annotations

import asyncio
import logging

from finengine.api import station_progress_ingest as I


def _post(events: list[dict], nonce: str = "n1") -> dict:
    return asyncio.run(I.ingest(I.ProgressBatch(nonce=nonce, events=events)))


def test_step_error_is_logged_even_without_a_consumer(caplog):
    """Главный случай: подписчика уже нет, и без лога ошибка исчезла бы совсем."""
    with caplog.at_level(logging.ERROR, logger=I.log.name):
        _post([{"type": "plan_step", "index": 2, "status": "error",
                "note": "не удалось собрать показатели"}])
    assert any("не удалось собрать показатели" in r.getMessage() for r in caplog.records), (
        f"ошибка шага не попала в лог: {[r.getMessage() for r in caplog.records]}"
    )


def test_error_event_is_logged(caplog):
    with caplog.at_level(logging.ERROR, logger=I.log.name):
        _post([{"type": "error", "error": "station analyze failed"}])
    assert any("station analyze failed" in r.getMessage() for r in caplog.records)


def test_normal_progress_does_not_spam_the_log(caplog):
    """Штатный прогресс в ERROR не пишем — иначе лог перестанут читать."""
    with caplog.at_level(logging.ERROR, logger=I.log.name):
        _post([{"type": "metric", "enum": "SALES_REVENUE", "value": 100},
               {"type": "plan_step", "index": 1, "status": "done"}])
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]


def test_error_is_logged_when_a_consumer_exists(caplog):
    """Доставили на экран — всё равно пишем: экран пользователь закроет."""
    q = I.register("n2")
    try:
        with caplog.at_level(logging.ERROR, logger=I.log.name):
            res = _post([{"type": "plan_step", "status": "error", "note": "упало"}],
                        nonce="n2")
        assert res["accepted"] == 1, "событие не доставлено подписчику"
        assert any("упало" in r.getMessage() for r in caplog.records)
    finally:
        I.unregister("n2", q)
