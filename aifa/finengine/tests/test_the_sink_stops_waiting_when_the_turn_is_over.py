"""Кончившийся ход не заставляет сток сидеть двадцать минут впустую.

ЦЕНА (19.08.2026, Банк «Санкт-Петербург»). Окно молчания стока — 1200 секунд,
и пока ход ИДЁТ это верно: комната считает молча, обрывать её нельзя. Но ход
там оборвался, извлечение отработало, а сток честно досидел двадцать минут,
прежде чем кто-нибудь узнал, что показатели уже готовы. Двадцать минут, за
которые ничего не могло произойти: кончившийся ход больше ничего не пришлёт.

ЧТО ЗДЕСЬ СТЕРЕЖЁТСЯ — ПОВЕДЕНИЕ СТОКА, а не текст: крутится настоящий
`station_run_sink.drain` на настоящей очереди, подделаны только часы (петля
asyncio с ручным временем не нужна — окно задаём секундами и ждём его честно,
поэтому числа маленькие).

ДВЕ ПОЛОВИНЫ ОДНОГО ПРАВИЛА, и обе обязательны:
  · ход кончился → окно сжимается;
  · ход ИДЁТ → окно прежнее, и сток НЕ смеет оборвать работающую комнату.

ПРОВЕРКА СТОРОЖЕЙ ОТКАТОМ (мутации прогнаны 21.08.2026, каждая покраснела):
  · снять применение `quiet_after_turn` в петле стока → падает
    `test_the_sink_gives_up_soon_after_the_turn_ends`;
  · ждать одним длинным куском вместо отрезков проверки → падает
    `test_the_turn_may_end_after_the_last_event` (сток спрашивает про конец
    хода однажды и на двадцать минут засыпает);
  · сжимать окно и при идущем ходе (`lambda: 0.05` без проверки `turn_over`) →
    падает `test_a_running_turn_keeps_the_full_window`.
"""
from __future__ import annotations

import asyncio

import pytest

from finengine.skills import station_run_sink as sink


async def _drain(*, quiet, timeout=30.0, events=(), gap=0.0):
    """Прогнать настоящий сток и вернуть, чем он кончился и за сколько."""
    queue: asyncio.Queue = asyncio.Queue()
    seen = {"abandoned": 0, "done": 0}

    async def _feed():
        for ev in events:
            await asyncio.sleep(gap)
            await queue.put(ev)

    loop = asyncio.get_running_loop()
    started = loop.time()
    feeder = asyncio.create_task(_feed())
    await sink.drain(
        nonce="n" * 32, queue=queue, owner_id=1, timeout=timeout,
        on_done=lambda run: seen.__setitem__("done", seen["done"] + 1),
        on_failed=lambda msg: None,
        on_abandoned=lambda run: seen.__setitem__("abandoned", seen["abandoned"] + 1),
        quiet_after_turn=quiet,
    )
    await feeder
    return seen, loop.time() - started


@pytest.mark.asyncio
async def test_the_sink_gives_up_soon_after_the_turn_ends():
    """Ход кончился — сток закрывается по короткому окну, а не по длинному."""
    # ⚠️ КОЛБЭК ПРИНИМАЕТ ПРИЗНАК «ЧТО-ТО УЖЕ ПРИЕХАЛО» (23.08.2026). Здесь
    # сжатие проверяется как таковое, поэтому признак игнорируется; правило
    # «строки идут — окно не сжимаем» живёт у вызывающего и накрыто своим
    # сторожем (`test_a_run_that_already_sent_rows_is_given_the_full_window`).
    seen, spent = await _drain(
        quiet=lambda _rows: 0.15,
        timeout=30.0,
        events=[{"type": "metric", "enum": "REVENUE"}],
    )
    assert seen["abandoned"] == 1, "сток не доложил, что прогон замолчал"
    assert spent < 5.0, (
        f"сток просидел {spent:.1f} с при окне 0,15 с — сжатие не применилось, "
        "и пользователь снова ждёт двадцать минут на готовом результате")


@pytest.mark.asyncio
async def test_the_turn_may_end_after_the_last_event(monkeypatch):
    """Самый злой случай: станция замолчала РАНЬШЕ, чем кончился ход.

    Сток к этому моменту уже вошёл в ожидание на всё длинное окно. Если он
    ждёт одним куском, ответ `quiet_after_turn()` он спросит только через
    двадцать минут — то есть сжатие не сработает ровно там, где нужно.
    Проверяется, что сток пересматривает окно ОТРЕЗКАМИ.
    """
    monkeypatch.setattr(sink, "_RECHECK_SEC", 0.05)
    turn_over = {"yes": False}

    async def _end_the_turn():
        await asyncio.sleep(0.20)      # ход кончается ПОСЛЕ последнего события
        turn_over["yes"] = True

    queue: asyncio.Queue = asyncio.Queue()
    seen = {"abandoned": 0}
    await queue.put({"type": "metric", "enum": "REVENUE"})
    ender = asyncio.create_task(_end_the_turn())
    loop = asyncio.get_running_loop()
    started = loop.time()
    await sink.drain(
        nonce="n" * 32, queue=queue, owner_id=1, timeout=30.0,
        on_done=lambda run: None,
        on_failed=lambda msg: None,
        on_abandoned=lambda run: seen.__setitem__("abandoned", 1),
        # Сигнатура колбэка приняла признак «что-то уже приехало» (23.08):
        # сжимать окно можно, только пока НИЧЕГО не пришло. Здесь строк нет,
        # поэтому признак всегда False — проверяется именно сжатие.
        quiet_after_turn=lambda _rows: 0.05 if turn_over["yes"] else None,
    )
    spent = loop.time() - started
    await ender
    assert seen["abandoned"] == 1
    assert spent < 5.0, (
        f"сток просидел {spent:.1f} с: он уснул на всё длинное окно и не "
        "заметил, что ход кончился. Сжатие спрашивают отрезками, а не однажды")


@pytest.mark.asyncio
async def test_a_running_turn_keeps_the_full_window():
    """Пока ход идёт, комнату не обрывают: `None` — не сжимать.

    Обратная половина правила. Без неё «ускорение» оборвало бы работающее
    извлечение на первой же паузе между событиями.
    """
    done = asyncio.Event()

    async def _late_terminal(queue):
        await asyncio.sleep(0.25)
        await queue.put({"type": "done", "summary": {}})
        done.set()

    queue: asyncio.Queue = asyncio.Queue()
    seen = {"abandoned": 0, "done": 0}
    await queue.put({"type": "metric", "enum": "REVENUE"})
    task = asyncio.create_task(_late_terminal(queue))
    await sink.drain(
        nonce="n" * 32, queue=queue, owner_id=1, timeout=5.0,
        on_done=lambda run: seen.__setitem__("done", seen["done"] + 1),
        on_failed=lambda msg: None,
        on_abandoned=lambda run: seen.__setitem__("abandoned", seen["abandoned"] + 1),
        quiet_after_turn=lambda _rows: None,    # ход ИДЁТ
    )
    await task
    assert seen["done"] == 1 and seen["abandoned"] == 0, (
        "сток оборвал идущий прогон и не дождался терминала, приехавшего "
        "через четверть секунды")


def test_the_window_outlives_the_longest_legitimate_silence():
    """🔴 ЖИВАЯ РЕГРЕССИЯ 22.08, И ЭТОТ СТОРОЖ КУПЛЕН ЕЮ.

    Первая редакция ставила окно в 60 секунд, исходя из «кончившийся ход больше
    ничего не пришлёт». Про МОДЕЛЬ это верно, про СТАНЦИЮ — нет: комната живёт
    своей жизнью и молчит минутами, пока идёт первая стадия.

    Прогон владельца по Северстали (нонс e606e6ce): ход отцепился в 13:32:19,
    станция законно молчала 3,5 минуты, окно истекло, сток снял подписку — и 75
    строк, а следом ещё 52, приехали в пустоту. Пользователь увидел готовую
    таблицу в рабочей области и НИ ОДНОГО слова в чате.

    Окно обязано быть шире самого долгого ЗАКОННОГО молчания. Самое долгое
    измерено: 216 секунд первой стадии (замер 20.08, та же Северсталь).
    """
    from finengine.api import station_turn as st

    САМОЕ_ДОЛГОЕ_ЗАКОННОЕ_МОЛЧАНИЕ = 216.0   # первая стадия, замер 20.08
    assert st._QUIET_AFTER_TURN_SEC > САМОЕ_ДОЛГОЕ_ЗАКОННОЕ_МОЛЧАНИЕ * 1.5, (
        f"окно {st._QUIET_AFTER_TURN_SEC} с против измеренных "
        f"{САМОЕ_ДОЛГОЕ_ЗАКОННОЕ_МОЛЧАНИЕ} с сжатия — сток снова оборвёт "
        "работающую станцию, и результат приедет в пустоту")
    assert st._QUIET_AFTER_TURN_SEC < 1200.0, (
        "окно доросло до прежних двадцати минут — сжатие перестало что-либо "
        "экономить")


@pytest.mark.asyncio
async def test_without_the_callback_the_sink_behaves_as_before():
    """Воркерный путь колбэка не передаёт — его поведение не меняется."""
    queue: asyncio.Queue = asyncio.Queue()
    seen = {"abandoned": 0}
    await queue.put({"type": "metric", "enum": "REVENUE"})
    await sink.drain(
        nonce="n" * 32, queue=queue, owner_id=1, timeout=0.2,
        on_done=lambda run: None,
        on_failed=lambda msg: None,
        on_abandoned=lambda run: seen.__setitem__("abandoned", 1),
    )
    assert seen["abandoned"] == 1
