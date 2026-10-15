"""Сравнительное значение берётся с ОСИ САМОЙ СТРОКИ (#205, 19.08.2026).

ДЕФЕКТ, НАЙДЕННЫЙ НА ПРОГОНЕ ВЛАДЕЛЬЦА ПО РОССЕЛЬХОЗБАНКУ. У промежуточного
отчёта по МСФО две оси сравнения, и это не наша особенность, а устройство
отчётности:

    баланс   31.03.2026  против  31.12.2025   ← конец прошлого ГОДА
    ОПУ      Q1 2026     против  Q1 2025      ← тот же квартал год назад

Эталон разметки устроен так же: из 98 строк 68 датированы (31.03.2026 /
31.12.2025) и лишь 30 подписаны интервалами (Q1 2026 / Q1 2025).

Движок спрашивает сравнительное значение ОДНИМ периодом на весь прогон.
Балансовая строка честно отвечает `None`: значения за первый квартал прошлого
года у неё нет и быть не может. Итог на экране — `value_prev` заполнен у 18
строк из 110: пустая вторая колонка и отсутствие дельты у двух третей таблицы,
при том что число лежит в самой строке.

⚠️ ЧЕГО ЭТОТ ТЕСТ НЕ ПРО. Замер этим дефектом НЕ портится: скорер сверяет
тройки (показатель, период, значение) из `period_values`, и по всем четырём
периодам прогон попал (TP: 30/36, 29/32, 14/15, 14/15). Это потеря ПРИ ПОКАЗЕ,
и бенчмарк её не видит вовсе. Два разных списка дефектов, и путать их нельзя.

ПРОВЕРКА СТОРОЖЕЙ ОТКАТОМ:
  * убрать вызов `fill_row_comparative` из `station_run_sink.drain` →
    `test_the_sink_fills_the_comparative_for_every_row`;
  * вернуть в `fill_row_comparative` выбор «второго по списку» вместо
    ближайшего предшествующего → `test_the_nearest_earlier_period_wins`;
  * убрать `period_prev_label` из `station_turn._ROW_KEYS` →
    `test_the_screen_carries_the_label_of_the_comparative`.
"""
from __future__ import annotations

import asyncio

from finengine.skills import extract_result


def _row(enum, cur, pvs, **over):
    return {"type": "metric", "enum": enum, "value_current": cur,
            "value_prev": None,
            "period_values": [{"period": p, "value": v} for p, v in pvs], **over}


def test_a_balance_row_compares_with_the_year_end():
    """Живая строка прогона РСХБ: 908 529 лежит в строке и не доезжает до колонки."""
    row = _row("CASH_EQUIVALENTS", 605868.0,
               [("31.03.2026", 605868.0), ("31.12.2025", 908529.0)])

    assert extract_result.fill_row_comparative(row) is True
    assert row["value_prev"] == 908529.0, (
        "сравнительное значение баланса потеряно — вторая колонка пуста, "
        "хотя число лежит в этой же строке")
    assert row["period_prev_label"] == "31.12.2025", (
        "не сказано, С ЧЕМ сравнение: у баланса это конец года, а колонка "
        "подписана кварталом — подпись обязана быть построчной")


def test_a_profit_and_loss_row_keeps_its_own_axis():
    """У ОПУ сравнительный — тот же квартал год назад, и он уже заполнен."""
    row = _row("INTEREST_INCOME_ER", 172441.0,
               [("Q1 2026", 172441.0), ("Q1 2025", 199063.0)])
    row["value_prev"] = 199063.0

    assert extract_result.fill_row_comparative(row) is False, (
        "правило вмешалось туда, где движок и так справился")
    assert row["value_prev"] == 199063.0


def test_the_nearest_earlier_period_wins():
    """Из трёх периодов берётся СОСЕДНИЙ по времени, а не второй по списку.

    Годовой отчёт часто несёт три колонки. «Второй элемент списка» дал бы
    сравнение через год — молча и правдоподобно.
    """
    # ⚠️ ПОРЯДОК ВХОДА ПОДОБРАН ТАК, ЧТОБЫ ОТЛИЧАТЬ ПРАВИЛО ОТ СЛУЧАЙНОСТИ.
    # Первая редакция этого теста шла в порядке 2024, 2026, 2025 — и проходила
    # даже со снятой сортировкой: сосед по списку случайно совпадал с соседом по
    # времени. Поймано отрицательным контролем. Здесь сразу за текущим периодом
    # стоит САМЫЙ СТАРЫЙ, и «второй по списку» даст 300 вместо 400.
    row = _row("TOTAL_ASSETS", 500.0,
               [("31.12.2026", 500.0), ("31.12.2024", 300.0), ("31.12.2025", 400.0)])

    assert extract_result.fill_row_comparative(row) is True
    assert row["value_prev"] == 400.0, (
        "взят не ближайший предшествующий период — сравнение уехало через год")
    assert row["period_prev_label"] == "31.12.2025"


def test_a_single_period_row_is_left_alone():
    """Одна колонка в отчёте — сравнивать не с чем, и выдумывать нельзя."""
    row = _row("PERPETUAL_BONDS", 53080.0, [("31.03.2026", 53080.0)])
    assert extract_result.fill_row_comparative(row) is False
    assert row["value_prev"] is None


def test_a_row_without_a_current_value_is_left_alone():
    """Показатель не найден — второй колонке взяться неоткуда."""
    row = _row("GOODWILL", None, [])
    assert extract_result.fill_row_comparative(row) is False


def test_the_sink_fills_the_comparative_for_every_row():
    """ПРАВИЛО СТОИТ НА ПУТИ КАЖДОЙ СТРОКИ, а не у одного потребителя.

    Через сток проходит один и тот же объект строки: он уезжает событием на
    экран, ложится в список прогона (а оттуда в раскладку роли, книгу Excel и
    сохранённый экран) и попадает в журнал. Правка в любом другом месте
    накрыла бы часть потребителей.
    """
    from finengine.skills import station_run_sink

    seen: list[dict] = []

    async def scenario():
        queue: asyncio.Queue = asyncio.Queue()
        queue.put_nowait(_row("CASH_EQUIVALENTS", 605868.0,
                              [("31.03.2026", 605868.0), ("31.12.2025", 908529.0)]))
        queue.put_nowait({"type": "done", "result": {"company_name": "РСХБ"}})
        await station_run_sink.drain(
            nonce="n1", queue=queue, owner_id=1, timeout=2.0,
            on_event=lambda ev: seen.append(ev),
            on_done=lambda run: seen.append({"done": run.metrics}),
            on_failed=lambda m: None)

    asyncio.run(scenario())

    live = [e for e in seen if e.get("type") == "metric"]
    assert live and live[0]["value_prev"] == 908529.0, (
        "живое событие уехало на экран без сравнительного значения")
    done = next(e for e in seen if "done" in e)
    assert done["done"][0]["value_prev"] == 908529.0, (
        "в списке прогона строка без сравнительного — значит и Excel, и "
        "раскладка роли, и сохранённый экран получат пустую вторую колонку")


def test_the_screen_carries_the_label_of_the_comparative():
    """Подпись сравнительного периода доезжает до сохранённого экрана."""
    from finengine.api import station_turn

    assert "period_prev_label" in station_turn._ROW_KEYS, (
        "подпись сравнительного периода не едет на экран: у баланса и у ОПУ "
        "он разный, и одна подпись колонки соврёт про две трети строк")
