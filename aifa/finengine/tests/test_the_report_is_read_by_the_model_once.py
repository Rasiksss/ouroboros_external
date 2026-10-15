"""Один и тот же отчёт модель читает ОДИН раз, а не каждый прогон.

ЦЕНА (замер 20.08.2026). Прогон Северстали — 318 секунд, из них **216 на первой
стадии**: модель читает отчёт целиком и отбирает таблицы, где искать каждый
показатель. Ответ при одном и том же вопросе один и тот же. Отчёт при этом
обрабатывают несколькими ходами подряд — извлекли, посчитали, разобрали — и
каждый ход платил эти 216 секунд заново.

Решение владельца 21.08: «давай делать кэш разумеется нужен».

## Что здесь стережётся

Ключ — ОТПЕЧАТОК ВСЕГО ВОПРОСА (системная часть + промпт), а не документа.
Соблазн ключевать по `doc_id` велик и неверен: у одного отчёта вопрос РАЗНЫЙ
при другой ячейке, другом составе показателей и после любой нашей правки
правил. Ключ по документу отдал бы вчерашний ответ на сегодняшний вопрос — и
молча, потому что оба выглядят правдоподобно.

## Чего в кэше нет намеренно

**Неудач.** Кладётся только РАЗОБРАННЫЙ ответ: положи мы сырой, переспрос
получил бы из кэша тот же мусор и три попытки стали бы одной.

**Падений.** Отказ кэша — не отказ прогона: сжатие просто считается заново.
Поменять ускорение на надёжность значило бы сделать ровно обратное задуманному.

ПРОВЕРКА СТОРОЖЕЙ ОТКАТОМ (мутации прогнаны 21.08.2026, каждая покраснела):
  · отпечаток считать только по `prompt`, без системной части → падает
    `test_a_rule_change_invalidates_the_cache`;
  · роут отдаёт запись любому нонсу (снять проверку живого хода) → падает
    `test_a_dead_nonce_gets_nothing`;
  · клиент моста роняет исключение вместо промаха → падает
    `test_a_broken_cache_never_breaks_the_run`.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from finengine.api import internal_compression_cache as cache
from finengine.api import internal_docs
from finengine.llm import station_bridge
from finengine.skills.two_stage_extractor import _prompt_fingerprint

_NONCE = "c" * 32


@pytest.fixture(autouse=True)
def _clean():
    cache.clear()
    yield
    cache.clear()


@pytest.fixture()
def client():
    import finengine.api.main as m

    internal_docs.register_nonce_docs(_NONCE, ["doc-1"])
    return TestClient(m.app)


def test_the_same_question_is_answered_from_memory(client):
    """Главный случай: положили — второй прогон берёт готовое."""
    fp = _prompt_fingerprint("отчёт целиком", "правила извлечения")
    assert client.get("/internal/compression-cache",
                      params={"nonce": _NONCE, "fingerprint": fp}).json() == {"hit": False}

    client.post("/internal/compression-cache",
                json={"nonce": _NONCE, "fingerprint": fp, "payload": '{"metrics":{"REVENUE":{}}}'})
    got = client.get("/internal/compression-cache",
                     params={"nonce": _NONCE, "fingerprint": fp}).json()
    assert got["hit"] is True and "REVENUE" in got["payload"], (
        "второй прогон того же отчёта снова заплатит 216 секунд")


def test_a_rule_change_invalidates_the_cache():
    """Правка ПРАВИЛ обязана обесценить кэш — иначе поедет старый ответ.

    Системная часть промпта несёт правила извлечения. Считай отпечаток без
    неё — и после правки правил движок молча возьмёт вчерашнюю раскладку.
    """
    отчёт = "текст отчёта, слово в слово тот же"
    было = _prompt_fingerprint(отчёт, "правила: версия 1")
    стало = _prompt_fingerprint(отчёт, "правила: версия 2")
    assert было != стало, (
        "отпечаток не заметил правки правил — кэш отдаст ответ, посчитанный "
        "по старым правилам, и никто этого не увидит")


def test_a_different_metric_set_is_a_different_question():
    """Тот же отчёт, другой состав показателей — другой вопрос, другой ключ."""
    правила = "правила"
    assert _prompt_fingerprint("отчёт + просят выручку", правила) != _prompt_fingerprint(
        "отчёт + просят весь реестр", правила)


def test_a_dead_nonce_gets_nothing(client):
    """Отвечаем живому ходу, а не случайному прохожему."""
    fp = _prompt_fingerprint("отчёт", "правила")
    client.post("/internal/compression-cache",
                json={"nonce": _NONCE, "fingerprint": fp, "payload": "{}"})
    r = client.get("/internal/compression-cache",
                   params={"nonce": "z" * 32, "fingerprint": fp})
    assert r.status_code == 403, (
        f"кэш ответил {r.status_code} на незнакомый нонс — пропуск не проверяется")


def test_the_store_does_not_grow_without_bound(client):
    """Сжатие крупного отчёта — сотни килобайт; потолок обязан держать."""
    for i in range(cache._MAX_ENTRIES + 5):
        client.post("/internal/compression-cache",
                    json={"nonce": _NONCE, "fingerprint": f"fp-{i}", "payload": "x"})
    assert cache.entries() <= cache._MAX_ENTRIES, (
        f"в кэше {cache.entries()} записей при потолке {cache._MAX_ENTRIES}")


def test_a_silent_daemon_is_asked_once_and_then_left_alone(monkeypatch, tmp_path):
    """Демона нет — спрашиваем ОДИН раз, дальше молчим.

    Тот же налог мы уже платили с методикой: старый демон + новая полка = 20
    секунд на каждый разбор. Здесь было бы хуже — сжатий за прогон несколько.
    Живой демон опрашивает комнату каждые 0,1 с, так что двух секунд ему хватает
    с запасом; мёртвому не хватит никакого времени, и ждать его дважды глупо.
    """
    import time as _t

    monkeypatch.setattr(station_bridge, "_cache_dead", False)
    monkeypatch.setenv("FE_RPC_DIR", str(tmp_path))
    начало = _t.monotonic()
    assert station_bridge.compression_cached("n", "fp") == ""
    первая = _t.monotonic() - начало
    начало = _t.monotonic()
    assert station_bridge.compression_cached("n", "fp2") == ""
    вторая = _t.monotonic() - начало
    assert первая < 5.0, f"первая проба ждала {первая:.1f} с — слишком долго"
    assert вторая < 0.1, (
        f"вторая проба ждала {вторая:.1f} с — кэш не выключился после молчания, "
        "и каждое сжатие будет платить это время ни за что")


def test_a_broken_cache_never_breaks_the_run(monkeypatch):
    """Отказ кэша — промах, а не исключение.

    Иначе ускорение куплено надёжностью: беда в кэше уронила бы извлечение,
    которое прекрасно посчиталось бы и без него.
    """
    monkeypatch.setenv("FE_RPC_DIR", "/несуществующий/каталог")
    assert station_bridge.compression_cached("n", "fp") == ""
    station_bridge.remember_compression("n", "fp", "payload")  # не бросает


def test_nothing_is_asked_without_a_nonce():
    """Вне станции нонса нет — кэш не спрашивают вовсе."""
    assert station_bridge.compression_cached("", "fp") == ""
    assert station_bridge.compression_cached("n", "") == ""
