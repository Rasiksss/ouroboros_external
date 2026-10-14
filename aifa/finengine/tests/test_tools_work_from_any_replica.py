"""Контекст хода и тикеты доступны с любой реплики.

🔴 ЗАЧЕМ. Ход регистрирует раннер `/chat/turns` на ОДНОЙ реплике, а вызовы
тулов приходят от гейтвея на ЛЮБУЮ. Пока и контекст, и тикеты жили словарями в
памяти процесса, второй воркер отвечал:

  * 403 «ход не зарегистрирован» — на КАЖДЫЙ вызов тула. Ход оставался без
    инструментов вовсе, то есть модель не могла ни извлечь, ни посчитать;
  * 404 «unknown ticket» — на поллинг. Результат долгого тула (извлечение идёт
    минутами) терялся целиком, и это выглядело как молчание, а не как отказ.

Вторая реплика изображается тем же приёмом, что у прогонов: локальный словарь
чистится, а общее состояние остаётся — ровно то, что видит соседний воркер.
"""
from __future__ import annotations

import asyncio

import pytest

from finengine.api import internal_tools as it
from finengine.store import bus as B
from finengine.store import cache as C


@pytest.fixture(autouse=True)
def чисто(tmp_path, monkeypatch):
    from finengine.config import settings

    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr(settings, "state_database_url", "")
    monkeypatch.setattr(settings, "redis_url", "")
    from finengine.store import state as S
    S._cached = None
    C._cached = None
    B.reset_bus()
    it.reset_sessions()
    yield
    it.reset_sessions()
    S._cached = None
    C._cached = None


def test_the_turn_context_is_visible_to_another_replica():
    """🔴 ГЛАВНОЕ. Мутация: вернуть словарь в памяти — тест краснеет."""
    it.register_session("gw-1", user_id=7, project_id="p1", run_id="r1")

    it._tickets.clear()          # «эта реплика хода не начинала»
    ctx = it._ctx_get("gw-1")

    assert ctx is not None, "чужая реплика не видит ход — тулы получат 403"
    assert ctx["user_id"] == 7 and ctx["project_id"] == "p1"


def test_an_unknown_turn_is_still_refused():
    """Незарегистрированный ход по-прежнему отказ, а не пустой контекст."""
    assert it._ctx_get("нет-такого") is None


def test_an_expired_turn_is_refused():
    """Истёкший ход не отдаётся — как и раньше."""
    from finengine.store import state as S

    it.register_session("gw-2", user_id=7, project_id="p1", run_id="r1")
    запись = S.get(S.GATEWAY_SESSION, owner="_", key="gw-2")
    запись["exp"] = 1.0          # давно истёк по стенным часам
    S.put(S.GATEWAY_SESSION, owner="_", key="gw-2", doc=запись)

    assert it._ctx_get("gw-2") is None


def test_the_deadline_is_wall_clock_not_a_process_counter():
    """🔴 СРОК — ПО ОБЩИМ ЧАСАМ, А НЕ ПО СЧЁТЧИКУ ПРОЦЕССА.

    Прежний код хранил `exp` от `time.monotonic()` — счётчика ОДНОГО процесса,
    отсчитываемого от его старта. У соседней реплики он свой, и одна и та же
    запись читалась бы то как вечная, то как всегда просроченная.

    Здесь подложен срок 5×10⁸ секунд. По СТЕННЫМ часам это 1985 год — давно
    прошёл, ход обязан быть отклонён. По СЧЁТЧИКУ ПРОЦЕССА это без малого
    шестнадцать лет непрерывной работы, то есть «ещё далеко впереди» на любой
    живой машине. Мутация «сравнивать с monotonic» краснит.

    ⚠️ Число подобрано, а не взято наугад: первая редакция ставила 4000 секунд
    и НЕ различала ничего — счётчик процесса обгоняет их через час работы
    машины, и обе ветки одинаково считали срок истёкшим.
    """
    from finengine.store import state as S

    it.register_session("gw-clock", user_id=7, project_id="p1", run_id="r1")
    запись = S.get(S.GATEWAY_SESSION, owner="_", key="gw-clock")
    запись["exp"] = 5e8          # 1985 год по стенным часам; 16 лет для счётчика
    S.put(S.GATEWAY_SESSION, owner="_", key="gw-clock", doc=запись)

    assert it._ctx_get("gw-clock") is None, (
        "срок считается счётчиком процесса — на соседней реплике он бессмыслен")


def test_appends_to_the_verbatim_mark_accumulate():
    """Дописки признака дословности складываются, а не затирают друг друга.

    ⚠️ ЧЕСТНО О ГРАНИЦАХ ЭТОЙ ПРОВЕРКИ. Настоящую гонку двух ПАРАЛЛЕЛЬНЫХ
    тулов тест в одном процессе не воспроизводит, и мутация «писать `put` по
    своему снимку вместо `mutate`» его НЕ КРАСНИТ: при последовательных
    вызовах снимок каждый раз свежий. Приписать сторожу это различение значило
    бы соврать зелёным тестом.

    Что проверяется на самом деле: дописки накапливаются через общее
    хранилище, то есть переезд из словаря в памяти ничего не потерял. Само
    свойство «одновременная правка не теряется» охраняется там, где оно
    реализовано, — `test_state_is_one_mechanism_not_five.py`, и там же названа
    граница его проверки.
    """
    it.register_session("gw-3", user_id=7, project_id="p1", run_id="r1")

    it._append_verbatim("gw-3", "первая часть. ")
    it._append_verbatim("gw-3", "вторая часть.")

    assert it.verbatim_text_of("gw-3") == "первая часть. вторая часть."


def test_the_turn_registration_does_not_lose_the_verbatim_mark():
    """Повторная регистрация хода СЛИВАЕТ, а не заменяет.

    Раннер зовёт `register_session` дважды: на старте и когда агент сообщит
    `session_id`. Второй вызов не должен стирать признак дословности — иначе
    модель допишет пересказ поверх готовой таблицы.
    """
    it.register_session("gw-4", user_id=7, project_id="p1", run_id="r1")
    it._append_verbatim("gw-4", "канон")
    it.register_session("gw-4", user_id=7, project_id="p1", run_id="r1",
                        session_id="s-1")

    assert it.verbatim_text_of("gw-4") == "канон"
    assert it._ctx_get("gw-4")["session_id"] == "s-1"


@pytest.mark.asyncio
async def test_a_ticket_is_pollable_from_another_replica():
    """🔴 ИСХОД ТИКЕТА ОБЩИЙ, ЗАДАЧА — ЛОКАЛЬНАЯ.

    Мутация: читать локальный словарь вместо общей записи — тест краснеет
    четырёхсотым «unknown ticket», то есть результат долгого тула теряется.
    """
    it._записать_тикет("t-1", run_id="", done=False)

    assert it._тикет("t-1") == {"run_id": "", "done": False}

    it._записать_тикет("t-1", done=True, out={"ok": True, "result": "готово"})
    it._tickets.clear()          # «эта реплика тикета не начинала»
    запись = it._тикет("t-1")

    assert запись["done"] is True
    assert запись["out"] == {"ok": True, "result": "готово"}


@pytest.mark.asyncio
async def test_a_foreign_ticket_is_cancelled_through_the_bus():
    """«Стоп» на чужой реплике доходит до владельца задачи тикета."""
    отменена = asyncio.Event()

    async def _долгий():
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            отменена.set()
            raise

    задача = asyncio.create_task(_долгий())
    it._tickets["t-2"] = {"task": задача, "run_id": "", "exp": 1e18}
    слушатель = asyncio.create_task(it._слушать_тикеты())
    await asyncio.sleep(0.05)

    # Изображаем чужую реплику: у неё тикета в словаре нет.
    свой = it._tickets.pop("t-2")
    it._погасить_тикет("t-2")
    it._tickets["t-2"] = свой

    await asyncio.wait_for(отменена.wait(), timeout=3.0)
    слушатель.cancel()


@pytest.mark.asyncio
async def test_a_ticket_result_is_written_even_when_the_tool_fails():
    """Отказ тула тоже исход: поллинг обязан получить ответ, а не ждать вечно."""
    async def _падает(*_a, **_k):
        raise RuntimeError("тул сломался")

    прежний = it._execute_tool
    it._execute_tool = _падает
    try:
        await it._исполнить_под_тикет("t-3", "fe_classify_report", {}, {}, "gw")
    finally:
        it._execute_tool = прежний

    запись = it._тикет("t-3")
    assert запись["done"] is True
    assert запись["out"]["ok"] is False and "тул сломался" in запись["out"]["error"]


# ───────── работа уезжает туда, где живёт история хода ─────────


@pytest.mark.asyncio
async def test_the_tool_runs_where_the_turn_lives():
    """🔴 ТУЛ ИСПОЛНЯЕТСЯ У ВЛАДЕЛЬЦА ХОДА, А НЕ ТАМ, КУДА ПРИШЁЛ ЗАПРОС.

    Причина не в удобстве: тул дописывает историю сессии, а `Session` —
    in-process объект раннера. Исполненный на чужой реплике, он допишет в СВОЮ
    копию, которую никто не сохранит: следующий ход не увидит ни вызова, ни
    результата, и модель либо повторит работу, либо ответит без данных. Отказа
    при этом не будет ни одного — только молча пустая история.

    Здесь ход «живёт» на этой реплике (прогон в реестре), а работа приходит
    сообщением, как от соседа. Мутация: не проверять, чей это ход, и брать
    любую работу — тест краснеет: работу возьмёт реплика без истории.
    """
    from finengine.skills import run_orchestrator as ro

    исполнено: list[str] = []

    async def _тул(tool, args, ctx, gw=""):
        исполнено.append(tool)
        return {"ok": True, "result": "готово"}

    async def _раннер():
        yield {"type": "start"}
        await asyncio.sleep(5)

    run_id = ro.start_run(_раннер, kind="chat", meta={})
    слушатель = asyncio.create_task(it._слушать_тикеты())
    прежний = it._execute_tool
    it._execute_tool = _тул
    try:
        await asyncio.sleep(0.05)
        await B.get_bus().publish(it._ТИКЕТЫ_КАНАЛ, {
            "cmd": "run_tool", "ticket": "t-4", "tool": "fe_classify_report",
            "args": {}, "ctx": {"run_id": run_id, "user_id": 7, "project_id": "p"},
            "gateway_session": "gw",
        })
        for _ in range(300):
            await asyncio.sleep(0.01)
            if исполнено:
                break
    finally:
        it._execute_tool = прежний
        слушатель.cancel()
        ro.cancel_run(run_id)

    assert исполнено == ["fe_classify_report"], "владелец хода не взял свою работу"


@pytest.mark.asyncio
async def test_work_for_a_foreign_turn_is_not_taken():
    """И наоборот: чужую работу реплика не берёт.

    Взять её значило бы исполнить тул там, где истории хода нет, — то самое,
    от чего защищает предыдущий сторож, только с другой стороны.
    """
    исполнено: list[str] = []

    async def _тул(tool, args, ctx, gw=""):
        исполнено.append(tool)
        return {"ok": True}

    слушатель = asyncio.create_task(it._слушать_тикеты())
    прежний = it._execute_tool
    it._execute_tool = _тул
    try:
        await asyncio.sleep(0.05)
        await B.get_bus().publish(it._ТИКЕТЫ_КАНАЛ, {
            "cmd": "run_tool", "ticket": "t-5", "tool": "fe_classify_report", "args": {},
            "ctx": {"run_id": "чужой-прогон"}, "gateway_session": "gw",
        })
        await asyncio.sleep(0.3)
    finally:
        it._execute_tool = прежний
        слушатель.cancel()

    assert исполнено == [], "реплика взяла чужую работу — история хода не там"


@pytest.mark.asyncio
async def test_a_start_for_a_foreign_turn_is_forwarded_not_executed_here():
    """🔴 РУЧКА ПЕРЕСЫЛАЕТ РАБОТУ, А НЕ ИСПОЛНЯЕТ ЕЁ У СЕБЯ.

    Гейтвей зовёт `/internal/tools/call` и попадает на любую реплику. Если ход
    идёт на другой, работу надо отдать туда — иначе тул допишет историю в
    копию `Session`, которую никто не сохранит.

    Мутация: исполнять локально всегда — тест краснеет: в шине нет работы, а
    задача заведена здесь.
    """
    it.register_session("gw-fwd", user_id=7, project_id="p1", run_id="чужой-прогон")
    подписка = B.get_bus().subscribe(it._ТИКЕТЫ_КАНАЛ)
    try:
        итог = await it.tools_call(
            it.ToolCallBody(tool="fe_classify_report", args={}, gateway_session="gw-fwd",
                            mode="start"),
            authorization="")
        сообщение = await подписка.get(timeout=2.0)
    finally:
        подписка.close()

    assert итог["status"] == "started"
    assert сообщение is not None, "работа не ушла владельцу хода"
    assert сообщение["cmd"] == "run_tool" and сообщение["tool"] == "fe_classify_report"
    assert сообщение["ticket"] == итог["ticket"]
    assert итог["ticket"] not in it._tickets, (
        "задача заведена здесь — тул исполнится там, где истории хода нет")
