"""Прогон, начатый одной репликой, доступен с любой другой.

🔴 ЗАЧЕМ. `docs/rules/architecture.md` 3.2 (MUST): «Запрос не должен зависеть
от того, какая реплика его обслужила». Это то, ради чего снимается
`--workers 1`. Прогон рождается на ОДНОЙ реплике (там его `asyncio.Task`), а
подписка, кнопка «Стоп» и прогресс тула могут прийти на ЛЮБУЮ.

КАК ЗДЕСЬ ИЗОБРАЖАЕТСЯ ВТОРАЯ РЕПЛИКА. Прогон запускается обычным путём, а
затем его запись убирается из `_runs` — реестра ЭТОЙ реплики. Задача при этом
жива (её держит цикл событий), общее состояние и шина на месте. Ровно то, что
видит соседний воркер: чужой живой прогон, до которого не дотянуться руками.
Сам объект придерживаем, чтобы вернуть реестр в конце и не сломать соседей.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from finengine.skills import run_orchestrator as ro
from finengine.store import bus as B
from finengine.store import cache as C


@pytest.fixture(autouse=True)
def чистая_координация(tmp_path, monkeypatch):
    from finengine.config import settings

    monkeypatch.setattr(settings, "docs_dir", tmp_path)
    monkeypatch.setattr(settings, "redis_url", "")
    B.reset_bus()
    C._cached = None
    yield
    B.reset_bus()
    C._cached = None
    ro._runs.clear()


class _Реплика:
    """Убирает прогон из реестра — и возвращает по выходе."""

    def __init__(self, run_id: str):
        self.run_id = run_id

    def __enter__(self):
        self._state = ro._runs.pop(self.run_id)
        return self._state

    def __exit__(self, *_):
        ro._runs[self.run_id] = self._state


async def _прогон(события: list[dict], пауза: float = 0.0):
    async def _раннер():
        for ev in события:
            if пауза:
                await asyncio.sleep(пауза)
            yield ev
    return ro.start_run(_раннер, kind="chat", meta={})


@pytest.mark.asyncio
async def test_a_finished_run_replays_from_the_journal_anywhere():
    """Закончившийся прогон читается по журналу — как и раньше."""
    run_id = await _прогон([{"type": "text_delta", "text": "а"}, {"type": "done"}])
    await asyncio.sleep(0.05)

    with _Реплика(run_id):
        события = [ev async for ev in ro.subscribe(run_id, kind="chat")]

    assert [e["type"] for e in события] == ["text_delta", "done"]


@pytest.mark.asyncio
async def test_a_live_run_of_another_replica_gives_catch_up_and_tail():
    """🔴 ГЛАВНОЕ. Мутация: убрать ветку живого чужого прогона — подписка
    отдаст журнал и оборвётся, то есть ответ будет выглядеть обрезанным."""
    держатель: list[str] = []

    async def _раннер():
        yield {"type": "text_delta", "text": "первое"}
        # Ждём, пока подписчик догонит журнал, и продолжаем — хвост обязан
        # доехать через шину, а не через локальную очередь.
        while not держатель:
            await asyncio.sleep(0.01)
        yield {"type": "text_delta", "text": "второе"}
        yield {"type": "done"}

    run_id = ro.start_run(_раннер, kind="chat", meta={})
    await asyncio.sleep(0.05)

    собрано: list[dict] = []
    with _Реплика(run_id):
        async for ev in ro.subscribe(run_id, kind="chat"):
            собрано.append(ev)
            держатель.append("догнал")

    assert [e.get("text") or e["type"] for e in собрано] == ["первое", "второе", "done"]


def _живой(события_до: list[dict]):
    """Прогон, который выдал что просили и ЖДЁТ. Возвращает (run_id, отпустить).

    Живой прогон обязателен для проверок ниже: если он успел закончиться до
    подписки, ветка живого чужого прогона не исполняется вовсе — и мутация в
    ней ничего не покраснит. Ровно на этом я и попался в первой редакции: два
    сторожа выглядели зелёными, а не различали ничего.
    """
    отпустить = asyncio.Event()

    async def _раннер():
        for ev in события_до:
            yield ev
        await отпустить.wait()
        yield {"type": "done"}

    return ro.start_run(_раннер, kind="chat", meta={}), отпустить


@pytest.mark.asyncio
async def test_nothing_is_delivered_twice():
    """Событие, попавшее и в журнал, и в шину, отдаётся ОДИН раз.

    Номер события — единственное, что их различает: догон читает журнал, хвост
    слушает шину, и в щели между ними одно и то же событие приходит дважды.
    Здесь повтор подан явно: после догона в шину кладётся сообщение с номером
    уже отданного события. Мутация «не сравнивать номер» краснит.
    """
    run_id, отпустить = _живой([{"type": "a"}])
    await asyncio.sleep(0.05)

    with _Реплика(run_id):
        поток = ro.subscribe(run_id, kind="chat")
        первое = await поток.__anext__()          # догон: событие №1 из журнала
        # Повтор того же события №1 — уже отданного. Подписка обязана смолчать.
        await B.get_bus().publish(ro.канал(run_id), {"seq": 1, "event": {"type": "a"}})
        отпустить.set()
        остальное = [ev async for ev in поток]

    assert первое["type"] == "a"
    assert [e["type"] for e in остальное] == ["done"], (
        "подписка отдала повтор события, уже прочитанного из журнала")


@pytest.mark.asyncio
async def test_a_subscriber_does_not_hang_when_the_terminal_is_lost():
    """🔴 ШИНА НЕ ГАРАНТИРУЕТ ДОСТАВКУ — подписка обязана это пережить.

    Прогон закрывается, а терминальное сообщение до подписчика НЕ доходит:
    канал отцеплен от шины. Подписка должна закончиться по ОБЩЕМУ СОСТОЯНИЮ, а
    не висеть до таймаута соединения. Мутация «убрать перепроверку состояния в
    паузе» — тест падает по своему сроку.

    ⚠️ ПОДПИСЧИКА НАДО ЗАГНАТЬ ИМЕННО В ХВОСТ. Первая редакция обрывала шину,
    пока он ещё дочитывал ЖУРНАЛ, — и `done` приезжал файлом (журнал успел
    дорасти, пока генератор был приостановлен). Тест падал, хотя код был прав.
    Поэтому здесь сперва вычерпывается догон, и только потом рвётся связь.
    """
    run_id, отпустить = _живой([{"type": "a"}])
    await asyncio.sleep(0.05)

    with _Реплика(run_id):
        поток = ro.subscribe(run_id, kind="chat")
        assert (await поток.__anext__())["type"] == "a"   # догон исчерпан

        # Загоняем подписчика в хвост: следующий шаг блокируется на ожидании
        # шины. Пока он там — рвём связь и закрываем прогон.
        следующий = asyncio.create_task(_собрать(поток))
        await asyncio.sleep(0.1)
        B.get_bus()._каналы.clear()
        отпустить.set()

        остальное = await asyncio.wait_for(следующий, timeout=6.0)

    # 🔴 ПОДПИСКА НЕ ПРОСТО ЗАКАНЧИВАЕТСЯ — ОНА ДОНОСИТ ХВОСТ.
    #
    # Первая редакция ждала здесь пустоты: раз шина оборвалась, значит и
    # событий больше нет. Живая проверка между процессами (26.08) показала, чем
    # это оборачивается: подписчик получал «первое» и «второе» и НЕ получал
    # `done` — конец ответа уходил вместе с ним. Теперь хвост добирается
    # журналом, и это строго лучше: журнал не теряет.
    assert [e["type"] for e in остальное] == ["done"], (
        "хвост, не доехавший по шине, обязан прийти из журнала")


async def _собрать(поток):
    return [ev async for ev in поток]


@pytest.mark.asyncio
async def test_stop_reaches_the_owner_from_another_replica():
    """Кнопка «Стоп» на чужой реплике доходит до владельца задачи.

    Отменить чужую `asyncio.Task` нельзя — только попросить владельца.
    Мутация: вернуть `False` для чужого прогона вместо команды — тест
    краснеет: прогон продолжает идти.
    """
    async def _раннер():
        yield {"type": "start"}
        await asyncio.sleep(30)          # ждём отмены
        yield {"type": "done"}

    run_id = ro.start_run(_раннер, kind="chat", meta={})
    await asyncio.sleep(0.05)

    with _Реплика(run_id) as state:
        assert ro.cancel_run(run_id) is True
        for _ in range(200):
            await asyncio.sleep(0.01)
            if state.finished:
                break

    assert state.finished, "прогон не остановился по команде с чужой реплики"
    assert state.error == "cancelled"


@pytest.mark.asyncio
async def test_progress_from_another_replica_lands_in_the_journal():
    """🔴 У ЖУРНАЛА ОДИН ПИСАТЕЛЬ — ВЛАДЕЛЕЦ.

    Тул гейтвей-петли может исполниться на другой реплике, чем ход, и его
    прогресс обязан доехать. Но писать в журнал со второй реплики нельзя:
    два писателя разъедут номера событий, а по ним подписчик отбрасывает
    повторы. Поэтому чужая реплика ПРОСИТ владельца.

    Мутация: писать напрямую (`state.emit` мимо владельца) — недоказуемо
    отсюда; мутация «вернуть False для чужого прогона» краснит этот тест.
    """
    async def _раннер():
        yield {"type": "start"}
        await asyncio.sleep(0.6)
        yield {"type": "done"}

    run_id = ro.start_run(_раннер, kind="chat", meta={})
    await asyncio.sleep(0.05)

    with _Реплика(run_id) as state:
        assert ro.emit_to_run(run_id, {"type": "tool_call", "name": "fe_calc"}) is True
        for _ in range(200):
            await asyncio.sleep(0.01)
            if any(e.get("name") == "fe_calc" for e in state.events):
                break

    типы = [e["type"] for e in state.events]
    assert "tool_call" in типы, "прогресс с чужой реплики не доехал до владельца"
    # И он лёг в журнал ровно один раз — писатель один.
    из_журнала = list(ro._из_журнала(run_id, "chat"))
    assert sum(1 for e in из_журнала if e.get("name") == "fe_calc") == 1


@pytest.mark.asyncio
async def test_an_unknown_run_still_says_so():
    """Незнакомый идентификатор — по-прежнему внятный отказ, а не тишина."""
    события = [ev async for ev in ro.subscribe("нет-такого", kind="chat")]

    assert len(события) == 1 and события[0]["type"] == "error"
    assert "не найден" in события[0]["message"]


@pytest.mark.asyncio
async def test_restart_recovery_does_not_bury_a_live_run_of_a_neighbour():
    """🔴 ЖИВОЙ ПРОГОН СОСЕДА — НЕ СИРОТА.

    Воркеров несколько, и упавший перезапускается по одному: его старт
    пробегает ЧУЖИЕ журналы. Без проверки общего состояния он дописал бы
    «прогон оборвался при перезапуске сервера» идущему прогону соседа — и
    пользователь увидел бы отказ посреди работающего хода.

    Мутация: убрать проверку — тест краснеет: живому прогону дописан отказ.
    """
    отпустить = asyncio.Event()

    async def _раннер():
        yield {"type": "text_delta", "text": "идёт"}
        await отпустить.wait()
        yield {"type": "done"}

    живой = ro.start_run(_раннер, kind="chat", meta={})
    мёртвый = await _прогон([{"type": "text_delta", "text": "оборвался"}])
    await asyncio.sleep(0.05)
    # У мёртвого убираем общую запись — так выглядит прогон УМЕРШЕГО процесса.
    from finengine.store.cache import get_cache
    get_cache().delete(f"run:{мёртвый}")

    with _Реплика(живой), _Реплика(мёртвый):
        ro.recover_orphan_runs()

    живые_события = [e["type"] for e in ro._из_журнала(живой, "chat")]
    мёртвые_события = [e["type"] for e in ro._из_журнала(мёртвый, "chat")]
    отпустить.set()
    await asyncio.sleep(0.05)

    assert "session_lost" not in живые_события, (
        "живому прогону соседа дописан отказ — пользователь увидит обрыв")
    assert "session_lost" in мёртвые_события, (
        "оборванный прогон не помечен — подписка на него будет крутиться вечно")


@pytest.mark.asyncio
async def test_a_foreign_run_still_checks_the_owner():
    """🔴 ПРОВЕРКА ВЛАДЕЛЬЦА НЕ ОТКЛЮЧАЕТСЯ НА ЧУЖОЙ РЕПЛИКЕ.

    Во всех пяти роутах она стояла так: «если прогон есть В ПАМЯТИ — сверить
    владельца», иначе пропустить. Пока «не в памяти» значило «давно закончился,
    остался журнал», это было мягко. С несколькими репликами это значит ещё и
    «идёт у соседа» — то есть на живом чужом ходе проверка отключалась бы
    вовсе, и любой авторизованный пользователь читал бы чужой поток по
    идентификатору прогона.

    Требование `docs/rules/security.md` 3.1: проверка владельца ресурса на
    КАЖДОМ защищённом роуте. Мутация: вернуть проверку только для локального
    прогона — тест краснеет.
    """
    async def _раннер():
        yield {"type": "start"}
        await asyncio.sleep(5)

    run_id = ro.start_run(_раннер, kind="chat", meta={"user_id": 7})
    await asyncio.sleep(0.05)

    try:
        with _Реплика(run_id):
            assert ro.владелец_прогона(run_id) == 7, (
                "владелец не виден чужой реплике — проверка доступа отключится")
            assert ro.чужой_прогон(run_id, 8) is True, "чужой ход отдан не своему"
            assert ro.чужой_прогон(run_id, 7) is False, "свой ход не отдан владельцу"
            assert ro.чужой_прогон(run_id, 8, is_admin=True) is False
    finally:
        ro.cancel_run(run_id)


# ─────────── журнал прогона живёт в ОБЩЕМ хранилище ───────────


@pytest.mark.asyncio
async def test_the_journal_no_longer_lives_on_the_volume():
    """🔴 ПОСЛЕДНЕЕ, ЧТО ДЕРЖАЛО РЕПЛИКИ В ОДНОМ КОНТЕЙНЕРЕ.

    Пока журнал был файлом, воркеры uvicorn работали (они делят том), а
    разнести реплики по машинам было нельзя: подписчик на чужой машине не
    увидел бы журнала вовсе и отдал бы только хвост — ответ без начала.

    Мутация: вернуть запись в файл — тест краснеет, потому что общий журнал
    останется пустым.
    """
    from finengine.store import journals

    run_id = await _прогон([{"type": "a"}, {"type": "done"}])
    await asyncio.sleep(0.05)

    из_хранилища = journals.read(ro.вид_журнала("chat"), owner="_", key=run_id)
    assert [e["type"] for e in из_хранилища] == ["a", "done"]
    assert not ro.event_file(run_id, "chat").exists(), (
        "журнал по-прежнему пишется файлом на томе")


@pytest.mark.asyncio
async def test_a_run_from_the_file_era_still_replays(tmp_path):
    """Прогоны эпохи файлового журнала не должны пропасть при переключении.

    Откат тот же, что у текста документа: спросить хранилище, потом диск. Без
    него старые прогоны перестали бы открываться в тот же миг, когда включили
    новое хранилище.

    Мутация: убрать чтение файла в `_из_журнала` — тест краснеет.
    """
    from finengine.config import settings

    старый = "bbbbbbbb-0000-0000-0000-000000000000"
    d = Path(settings.docs_dir) / "chat_events"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{старый}.jsonl").write_text(
        '{"type": "text_delta", "text": "из файла"}\n{"type": "done"}\n', encoding="utf-8")

    события = [ev async for ev in ro.subscribe(старый, kind="chat")]

    assert [e["type"] for e in события] == ["text_delta", "done"]
