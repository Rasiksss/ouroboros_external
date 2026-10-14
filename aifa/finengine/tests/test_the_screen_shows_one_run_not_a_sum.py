"""Экран показывает таблицу ОДНОГО извлечения, а не сумму попыток.

🔴 ЖИВОЙ ПРОГОН ВЛАДЕЛЬЦА 40981f96 (24.08.2026). На экране 88 строк при 68
уникальных: двадцать показателей ровно ПО ДВА РАЗА, строки 0–19 и 20–39 совпадают
до значения. Модель запустила извлечение дважды.

ПОЧЕМУ СЛОЖИЛОСЬ. Извлечение идёт с `--no-final` — терминал ставит РАСЧЁТ, в
самом конце цепочки. Значит между двумя извлечениями терминала нет, дренаж не
закрывается, и строки обоих прогонов копятся в одном списке.

⚠️ Я УЖЕ ПИСАЛ ЭТУ ПРАВКУ 22.08 И ОТКАТИЛ ЕЁ КАК ГИПОТЕТИЧЕСКУЮ. Рассуждение
было такое: «дренаж возвращается на первом `done`, второй `start` до него не
дойдёт». Для схемы, где терминал ставит само извлечение, это верно — но у нас
его ставит расчёт. Откат по рассуждению вместо замера стоил владельцу прогона
с задвоенной таблицей.

⚠️ ЭТО НЕ РАЗРЕШЕНИЕ ДУБЛЯМ. Дубль остаётся дефектом, его считает
`grounding_bus.duplicate_runs` и отбивает `station_guard`. Здесь чинится другое:
чтобы экран показывал таблицу ОДНОГО прогона. Смешанная таблица врёт молча —
знаменатель «найдено 18 из 20» к ней уже не подходит.

ПРОВЕРКА СТОРОЖЕЙ ОТКАТОМ (мутации прогнаны 24.08.2026, каждая покраснела):
  · не чистить списки на втором `start` → падает `test_a_second_extraction…`;
  · «чистить на каждом `start`» тесты НЕ красит, и я это проверил: условие
    `(metrics or …)` — экономия лишней записи в лог, а не защита. Выдавать её
    за проверку не буду.
"""
from __future__ import annotations

import asyncio

import pytest

from finengine.skills import station_run_sink as sink


def _metric(enum: str, value: float) -> dict:
    return {"type": "metric", "enum": enum, "name_ru": enum, "found": True,
            "period_values": [{"period": "Q1 2025", "value": value}]}


async def _drain(events: list[dict]) -> list[dict]:
    queue: asyncio.Queue = asyncio.Queue()
    собрано: dict = {}

    async def _feed():
        for e in events:
            await queue.put(e)

    task = asyncio.create_task(_feed())
    await sink.drain(
        nonce="n" * 32, queue=queue, owner_id=1, timeout=5.0,
        on_done=lambda run: собрано.setdefault("run", run),
        on_failed=lambda msg: None,
        on_abandoned=lambda run: собрано.setdefault("run", run),
    )
    await task
    return list(собрано["run"].metrics)


@pytest.mark.asyncio
async def test_a_second_extraction_starts_the_table_over():
    """Второй `start` — граница: предыдущая таблица кончилась."""
    rows = await _drain([
        {"type": "start", "company_name": "Северсталь"},
        _metric("SALES_REVENUE", 178733.0),
        _metric("TOTAL_ASSETS", 867864.0),
        # второе извлечение на том же нонсе — терминала между ними нет
        {"type": "start", "company_name": "Северсталь"},
        _metric("SALES_REVENUE", 178733.0),
        _metric("TOTAL_ASSETS", 867864.0),
        {"type": "done", "result": {"cell": "ifrs_nf_ru"}},
    ])
    имена = [m.get("enum") for m in rows]
    assert имена == ["SALES_REVENUE", "TOTAL_ASSETS"], (
        f"на экран уехало {len(rows)} строк вместо двух: {имена} — таблица снова "
        "сумма попыток, как в прогоне 40981f96 (88 строк при 68 уникальных)")


@pytest.mark.asyncio
async def test_a_start_after_rows_resets_them_whichever_start_it_is():
    """Правило — «шапка ПОСЛЕ строк», а не «шапка вторая».

    ⚠️ ЧЕСТНО ПРО ФОРМУЛИРОВКУ. Сперва я назвал этот тест «первый `start`
    сохраняет пришедшее до него» — и мутация показала, что название лгало:
    условие `(metrics or seg_rows or grp_rows)` различает не «первый/второй»,
    а «есть ли уже строки». Строка, приехавшая ДО шапки, сбрасывается тоже, и
    это правильно: шапка задаёт периоды таблицы, а строка без них — сирота.

    Само условие — не защита, а экономия: без него на каждом первом `start`
    писалась бы строка в лог «начинаю заново» при пустых списках. Мутация
    «чистить всегда» этот тест не красит, и я не выдаю её за проверку.
    """
    rows = await _drain([
        _metric("SALES_REVENUE", 178733.0),          # строка ДО шапки — сирота
        {"type": "start", "company_name": "Северсталь"},
        _metric("TOTAL_ASSETS", 867864.0),
        {"type": "done", "result": {"cell": "ifrs_nf_ru"}},
    ])
    assert len(rows) == 1 and rows[0]["enum"] == "TOTAL_ASSETS", (
        f"получено {[m.get('enum') for m in rows]}")


@pytest.mark.asyncio
async def test_a_normal_single_run_is_untouched():
    """Обычный прогон — один `start`, все строки на месте."""
    rows = await _drain([
        {"type": "start", "company_name": "Северсталь"},
        _metric("SALES_REVENUE", 178733.0),
        _metric("TOTAL_ASSETS", 867864.0),
        _metric("NET_INCOME", 21072.0),
        {"type": "done", "result": {"cell": "ifrs_nf_ru"}},
    ])
    assert len(rows) == 3
