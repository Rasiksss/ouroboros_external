"""Сорвавшийся батч называет ПРИЧИНУ, а прогон — честный процент выполненного.

ТРЕБОВАНИЕ ВЛАДЕЛЬЦА 21.08.2026, дословно:

> «В какой момент ответила мусором и почему? как часто? Сказать честно сколько
> процентов выполнено от запланированного и что не смог извлечь. Но нужно
> логировать почему не смог, чтобы потом на этом ошибки исправлять свои.»

ЧТО БЫЛО. В журнал ехало ЧИСЛО сорвавшихся батчей. По нему нельзя отличить
«модель вернула мусор» от «кончились деньги» — то есть нельзя понять, чинить
нам промпт или пополнить счёт. Текст ошибки провайдера при этом В ДИАГНОСТИКЕ
ЛЕЖАЛ (`stage2_invalid_batches[*].error`), но наружу не выходил и в журнал не
попадал: `log_extract_run` кладёт выборку, а не всю диагностику.

⚠️ ПОЧЕМУ ЭТО ПЕРВЕЕ ПОРОГА. Владелец спросил «как часто такое?» — считать
оказалось нечем: причина никуда не писалась, а логи бэкенда живут до
перезапуска контейнера. Порог частичного провала (сколько срывов из девяти
делают прогон отказом) назначать НЕ НА ЧЕМ, пока не накопится статистика
причин. Сначала запись, потом неделя наблюдений, потом порог.

ПРОВЕРКА СТОРОЖЕЙ ОТКАТОМ (мутации прогнаны 21.08.2026, каждая покраснела):
  · `failure_cause` → `return "иное"` всегда → падают три параметризации
    `test_the_cause_is_named_by_the_error_text` и
    `test_causes_are_counted_next_to_the_number`;
  · порядок проверок перевёрнут (сначала json, потом деньги) → падает
    `test_money_beats_json_when_both_words_are_in_the_error`;
  · `batches_done_pct` убран из выдачи → падает
    `test_the_run_says_how_much_of_the_plan_it_did`.
"""
from __future__ import annotations

import pytest

from finengine.skills.extract_result import failure_cause, shard_losses


@pytest.mark.parametrize("текст, причина", [
    ("HTTP 402: insufficient credits", "нет_денег"),
    ("Provider returned 401 Unauthorized", "ключ"),
    ("429 rate limit exceeded, retry later", "лимит_скорости"),
    ("read timed out after 240s", "таймаут"),
    ("Expecting value: line 1 column 1 (char 0)", "не_разобран_json"),
    ("connection reset by peer", "обрыв"),
    ("empty response from model", "модель_молчала"),
])
def test_the_cause_is_named_by_the_error_text(текст, причина):
    assert failure_cause(текст) == причина


def test_an_unknown_error_is_called_unknown_and_not_guessed():
    """Неузнанная причина обязана быть ВИДНА.

    Свалить её в «не_разобран_json» значило бы спрятать целый класс отказов:
    статистика показала бы разбор, а чинить надо было бы другое.
    """
    assert failure_cause("Пришелец съел ответ") == "иное"
    assert failure_cause("") == "без_текста"
    assert failure_cause(None) == "без_текста"


def test_money_beats_json_when_both_words_are_in_the_error():
    """Порядок проверок — это приоритет, а не оформление.

    «402 … invalid json» — про деньги: ответа не было вовсе, а мусором его
    сделал наш же разбор пустоты. Отнеси такое к разбору — и будешь чинить
    промпт там, где надо пополнить счёт.
    """
    assert failure_cause("402 insufficient credits; invalid json in body") == "нет_денег"


def test_causes_are_counted_next_to_the_number():
    losses = shard_losses({
        "stage2_batch_count": 9,
        "stage2_invalid_batches": [
            {"batch_no": 3, "error": "HTTP 402 insufficient credits"},
            {"batch_no": 7, "error": "402 more credits required"},
            {"batch_no": 8, "error": "Expecting value: line 1"},
        ],
    })
    assert losses["batches_failed"] == 3
    assert losses["batches_failed_causes"] == {"нет_денег": 2, "не_разобран_json": 1}, (
        "причины не посчитаны — по одному числу «сорвалось 3» нельзя понять, "
        "чинить промпт или пополнять счёт")


def test_the_run_says_how_much_of_the_plan_it_did():
    """Честный процент, а не «таблица неполна»."""
    losses = shard_losses({
        "stage2_batch_count": 9,
        "stage2_invalid_batches": [{"batch_no": 3, "error": "timeout"}],
    })
    assert losses["batches_done_pct"] == pytest.approx(88.9, abs=0.05), (
        f"выполнено {losses.get('batches_done_pct')} % при восьми удачных из "
        "девяти — пользователю сказали бы неверную долю")


def test_a_clean_run_is_a_hundred_percent_and_names_no_causes():
    losses = shard_losses({"stage2_batch_count": 9, "stage2_invalid_batches": []})
    assert losses["batches_done_pct"] == 100.0
    assert losses["batches_failed_causes"] == {}


def test_a_scheme_without_batches_stays_silent():
    """Схема без шардирования потерь не считает — и полей не заводит."""
    assert shard_losses({"stage2_batch_count": 0}) == {}
    assert shard_losses(None) == {}
