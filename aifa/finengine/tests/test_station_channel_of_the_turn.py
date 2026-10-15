"""Канал станции на ХОД: нонс, скоуп документов, подписка и её снятие.

ПОЧЕМУ ЭТОТ ФАЙЛ СУЩЕСТВУЕТ (#190, коммит К5). Ход чата теперь открывает
комнате станции доступ к своим документам и подписывается на нонс-канал. Три
вещи здесь ломаются молча, и ни одну не видно с экрана:

1. НОНС СТАНОВИТСЯ ИМЕНЕМ ФАЙЛА. В комнате прогресс пишется в
   `progress-<nonce>.ndjson`, и демон разбирает это имя по префиксу и суффиксу.
   Возьми нонсом сырой ключ хода — в нём три двоеточия — и файл не родится
   вовсе. Прогон при этом отработает, а прогресс и результат потеряются.

2. БЕЗ СКОУПА КОМНАТА НЕ ПОЛУЧИТ ДОКУМЕНТ. `GET /internal/doc-text` отвечает
   403 на нонс, для которого скоуп не открыт. И тот же вызов — единственный
   писатель связи нонс→документ, без которой сторож дубля прогона становится
   пустышкой.

3. ПОДПИСКУ НАДО СНИМАТЬ СВОЮ. `unregister(nonce, None)` сносит ВСЕ очереди
   нонса, а их на одном нонсе бывает несколько. Не снять вовсе — на каждый ход
   остаётся висеть задача слушателя на всё время жизни прогона.

⚠️ ЧЕТВЁРТОЕ ДОБАВЛЕНО В К8b: ХОД ВЛАДЕЕТ ПОДПИСКОЙ, НО НЕ ЖИЗНЬЮ ПРОГОНА.
В К5 конец хода делал `task.cancel()` безусловно. Прогон станции идёт полторы-три
минуты и ПЕРЕЖИВАЕТ короткий ход штатно — отмена рвала его вместе с ходом, и
терялся не только результат, но и отметка «прогон кончился»: сторож дубля #160
не узнавал о завершении и пускал второй прогон. Оплачено это уже было однажды —
253,5 секунды сожжённого повтора.

ПРОВЕРКА СТОРОЖА ОТКАТОМ: подставить в `arm` сырой `gw_key` вместо хвоста;
убрать `register_nonce_docs`; заменить снятие подписки на `unregister(nonce)` без
очереди; вернуть безусловный `cancel()` в `disarm` — каждая правка обязана
покрасить свой тест.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from finengine.api import internal_docs, station_progress_ingest, station_turn


GW_KEY = "agent:aifa-station:openresponses:fe-0123456789abcdef0123456789abcdef"
NONCE = station_turn.run_nonce_of(GW_KEY)


@pytest.fixture(autouse=True)
def _clean_scope():
    internal_docs.reset_scopes()
    station_progress_ingest._bus.clear()
    station_turn._detached.clear()
    yield
    internal_docs.reset_scopes()
    station_progress_ingest._bus.clear()
    station_turn._detached.clear()


def _arm(cell: dict, *, doc_ids: list[str] | None = None) -> None:
    station_turn.arm(nonce=NONCE, gw_key=GW_KEY, doc_ids=doc_ids or ["doc-a"],
                     owner_id=7, project_id="p", cell=cell)


def test_nonce_is_the_tail_and_can_be_a_file_name():
    """Нонс обязан годиться в имя файла: без двоеточий, слэшей и пробелов.

    Проверяется РЕАЛЬНЫЙ вывод нонса из ключа (`station_turn.run_nonce_of`), а
    не правило, пересказанное в тесте: пересказ остался бы зелёным, если бы код
    начал брать ключ целиком."""
    assert ":" not in NONCE, "в нонсе двоеточие — файл progress-<nonce>.ndjson не родится"
    assert "/" not in NONCE and " " not in NONCE
    assert NONCE == "0123456789abcdef0123456789abcdef"
    assert NONCE in GW_KEY and NONCE != GW_KEY, "нонс — хвост ключа, а не сам ключ"
    assert station_turn.run_nonce_of("") == "", "ход без гейтвей-петли нонса не получает"


async def test_arming_opens_the_document_scope_and_a_subscription():
    cell: dict = {}
    _arm(cell, doc_ids=["doc-a", "doc-b"])
    try:
        assert internal_docs._allowed(NONCE, "doc-a"), (
            "скоуп не открыт — комната получит 403 на выдачу текста, а связь "
            "нонс→документ не возникнет вовсе"
        )
        assert internal_docs._allowed(NONCE, "doc-b")
        assert not internal_docs._allowed(NONCE, "doc-чужой"), "скоуп шире, чем ход"
        assert station_progress_ingest._bus.get(NONCE), "подписки на нонс нет"
        assert "station" in cell, "нечего будет снимать в конце хода"
    finally:
        station_turn.disarm(cell)


async def test_a_turn_that_saw_nothing_is_cancelled_and_unsubscribed():
    """Признаков жизни не было — отменяем: ничего и не начиналось.

    Здесь же проверяется, что снимается СВОЯ очередь, а не все: на одном нонсе
    их бывает несколько, и чужой подписчик остался бы без канала, не сказав ни
    слова.
    """
    foreign = station_progress_ingest.register(NONCE)  # чужой подписчик
    cell: dict = {}
    _arm(cell)
    channel = station_turn.channels_of(cell)[station_turn.KIND_EXTRACT]
    station_turn.disarm(cell)
    await asyncio.sleep(0)

    left = station_progress_ingest._bus.get(NONCE) or []
    assert channel.queue not in left, "своя очередь не снята — на каждый ход утечка"
    assert foreign in left, (
        "снесены ВСЕ очереди нонса вместо своей — чужой подписчик остался без "
        "канала и замолчал бы, не сказав ни слова"
    )
    assert channel.task.cancelled() or channel.task.done(), (
        "слушатель хода остался жить, хотя прогон даже не начинался")
    assert "station" not in cell, "повторное снятие сработает по мёртвой ссылке"
    assert not station_turn._detached, "нечего было отцеплять"


async def test_a_live_run_is_detached_not_cancelled():
    """Прогон подавал признаки жизни — ход ОТЦЕПЛЯЕТСЯ, а не рвёт его.

    Это главный инвариант К8b. Отмена здесь означала бы потерю не только
    результата, но и отметки «прогон кончился»: сторож дубля не узнал бы о
    завершении и пустил бы второй прогон движка (#160, замер 253,5 с).
    """
    cell: dict = {}
    _arm(cell)
    channel = station_turn.channels_of(cell)[station_turn.KIND_EXTRACT]
    channel.queue.put_nowait({"type": "start", "company_name": "Тест"})
    await asyncio.sleep(0)  # дать слушателю прочитать событие
    assert channel.saw_life, "предпосылка теста ушла: признак жизни не отмечен"

    station_turn.disarm(cell)
    await asyncio.sleep(0)
    assert not channel.task.done(), (
        "прогон оборван вместе с ходом — потеряются и результат, и отметка "
        "о завершении, а сторож дубля пустит второй движок"
    )
    assert channel.turn_over, "слушатель не знает, что экрана уже нет"
    assert station_progress_ingest._bus.get(NONCE), (
        "очередь снята у живого прогона — результату некуда будет приехать")

    # «Стоп» обязан дотягиваться до отцепленного: иначе кнопка гасит ход, а
    # комната продолжает считать и пишет в чат, из которого пользователь ушёл.
    # Реестр отцепленных — «ран → {вид: канал}» (#244): у одного рана их два,
    # извлечение и анализ, и плоский словарь молча терял бы один из результатов.
    channel.run_cell["run_id"] = "run-1"
    station_turn._detached["run-1"] = {channel.kind: channel}
    assert station_turn.stop("run-1") is True
    await asyncio.sleep(0)
    assert channel.task.cancelled() or channel.task.done(), "«Стоп» не дотянулся"


async def test_explicit_stop_cancels_even_a_live_run():
    """Явный «Стоп» рвёт прогон, даже если он живой: человек отказался."""
    cell: dict = {}
    _arm(cell)
    channel = station_turn.channels_of(cell)[station_turn.KIND_EXTRACT]
    channel.queue.put_nowait({"type": "start"})
    await asyncio.sleep(0)

    station_turn.disarm(cell, stopped=True)
    await asyncio.sleep(0)
    assert channel.task.cancelled() or channel.task.done(), (
        "«Стоп» оставил прогон работать — пользователь отказался от результата")
    assert not station_turn._detached, "отменённый прогон попал в реестр отцепленных"


def test_a_turn_without_the_gateway_arms_nothing():
    """Ход без гейтвей-петли нонса не получает — вооружать нечего и незачем."""
    cell: dict = {}
    station_turn.arm(nonce="", gw_key="", doc_ids=["doc-a"], owner_id=7,
                     project_id="p", cell=cell)
    assert cell == {}, "канал взведён без нонса — снять его будет нечем"
    assert not station_progress_ingest._bus


def test_disarming_an_unarmed_turn_is_quiet():
    """Конец хода зовёт снятие ВСЕГДА, в том числе когда ничего не взводили."""
    station_turn.disarm({})
    station_turn.disarm({"other": 1})


def test_stopping_an_unknown_run_is_quiet():
    assert station_turn.stop("нет такого") is False


# ── адресат АНАЛИЗА: второй канал того же хода (#244, шаг 2) ─────────────────


async def test_two_kinds_get_two_nonces_and_two_channels():
    """Извлечение и разбор — РАЗНЫЕ адреса, иначе доставят одного из двух.

    Нонс — это адрес терминального события. Пусти оба прогона хода на один
    нонс, и дренаж вернётся на ПЕРВОМ `done`: кто добежал первым, тот и
    доставлен. Разделить их постфактум нечем — событие своего вида не несёт.
    """
    cell: dict = {}
    _arm(cell)
    station_turn.arm(nonce=station_turn.analysis_nonce_of(NONCE), gw_key=GW_KEY,
                     doc_ids=["doc-a"], owner_id=7, project_id="p", cell=cell,
                     kind=station_turn.KIND_ANALYZE)

    channels = station_turn.channels_of(cell)
    assert set(channels) == {station_turn.KIND_EXTRACT, station_turn.KIND_ANALYZE}
    assert channels[station_turn.KIND_EXTRACT].nonce != \
        channels[station_turn.KIND_ANALYZE].nonce, "оба вида сели на один адрес"
    assert channels[station_turn.KIND_ANALYZE].kind == station_turn.KIND_ANALYZE
    station_turn.disarm(cell, stopped=True)


def test_analysis_nonce_is_derived_not_issued():
    """Выводится из нонса хода — обе стороны считают его одной функцией.

    Выданный нонс пришлось бы хранить и доставлять дважды: модели в промпт и
    раннеру в подписку. Два источника одного значения у нас уже расходились.
    """
    assert station_turn.analysis_nonce_of(NONCE) == f"{NONCE}-a"
    assert station_turn.analysis_nonce_of("") == "", "пустой ключ — пустой нонс"
    # Форма важна: нонс становится именем файла `progress-<nonce>.ndjson`, и
    # демон разбирает его по префиксу и суффиксу.
    assert "/" not in station_turn.analysis_nonce_of(NONCE)
    assert "." not in station_turn.analysis_nonce_of(NONCE)


def test_each_kind_has_its_own_finisher():
    """Вид выбирает адресата ТАБЛИЦЕЙ, а не развилкой внутри общего стока.

    Развилка внутри `_finish_run` — это тот же монолит заново, только в новом
    месте: у извлечения итог собирается по раскладке роли и Excel, у разбора —
    по карточке и артефакту, общего у них ровно ноль.
    """
    assert station_turn._FINISHERS[station_turn.KIND_EXTRACT] is station_turn._finish_run
    assert station_turn._FINISHERS[station_turn.KIND_ANALYZE] is station_turn._finish_analysis


async def test_stop_kills_every_kind_of_the_run():
    """«Стоп» гасит ОБА канала рана, а не тот, что попался первым.

    Погасить один значит оставить второй писать в чат, из которого пользователь
    уже ушёл, — то есть ровно то, от чего кнопка защищает, только наполовину.
    """
    cell: dict = {}
    _arm(cell)
    station_turn.arm(nonce=station_turn.analysis_nonce_of(NONCE), gw_key=GW_KEY,
                     doc_ids=["doc-a"], owner_id=7, project_id="p", cell=cell,
                     kind=station_turn.KIND_ANALYZE)
    both = dict(station_turn.channels_of(cell))
    for ch in both.values():
        ch.run_cell["run_id"] = "run-2"
    station_turn._detached["run-2"] = both

    assert station_turn.stop("run-2") is True
    assert all(ch.turn_over for ch in both.values()), "один из каналов остался жив"
    assert "run-2" not in station_turn._detached


# ── ВЛАДЕЛЕЦ ДОКУМЕНТА: скоуп хода набивается НЕ телом запроса (Б8.1) ────────


async def test_a_foreign_document_never_enters_the_turn_scope(monkeypatch, tmp_path):
    """Чужой doc_id из тела `POST /chat/turns` в скоуп нонса не попадает.

    ДЕФЕКТ, КУПЛЕННЫЙ РАЗБОРОМ 19.08.2026 (Б8.1). Список документов хода
    собирался из `request.doc_ids` + `active_doc_id` и БЕЗ проверки владельца
    уходил в `station_turn.arm`, то есть прямо в `register_nonce_docs`. Скоуп
    нонса — единственное, что решает, какой текст комната получит по
    `GET /internal/doc-text`: сам роут проверяет токен демона, форму doc_id и
    членство в скоупе, но НЕ владельца. Любой вошедший пользователь, подставив
    чужой doc_id в тело хода, вытягивал в свою комнату чужой отчёт.
    `docs/rules/security.md` §3.1 — блокер.

    ХОДИМ БОЕВЫМ ПУТЁМ: настоящий обработчик `/chat/turns`, настоящий `arm`,
    настоящий `register_nonce_docs`, настоящий `/internal/doc-text`. Подменены
    только внешние опоры — индекс документов и агент (ход нам нужен ради
    взвода канала, а не ради ответа модели).

    ПРОВЕРКА СТОРОЖА ОТКАТОМ: вернуть в `main.chat_turn_start` голое
    `_turn_docs = [d for d in ([*(request.doc_ids or []), …]) if d]` — тест
    обязан покраснеть на том, что чужой документ оказался в скоупе и выдался.
    """
    from finengine.api import main as m
    from finengine.auth.db import User
    from finengine.config import settings
    from finengine.models.query import QueryRequest
    from finengine.skills import grounding_bus, run_logger

    # Только ASCII: `internal_docs._DOC_ID_RE` отбивает всё прочее 400-й,
    # и тест мерил бы форму имени вместо владельца.
    mine, foreign = "doc-mine", "doc-foreign"
    me = User(id=7, username="u7")

    monkeypatch.setattr(settings, "docs_dir", tmp_path)
    monkeypatch.setattr(run_logger, "_LOG_PATH", tmp_path / "extract_runs.jsonl")
    monkeypatch.setattr(run_logger, "_CHAT_LOG_PATH", tmp_path / "chat_runs.jsonl")
    monkeypatch.delenv("FINENGINE_OPENCLAW_GATEWAY_TOKEN", raising=False)
    grounding_bus._worker_docs.clear()

    owners = {mine: 7, foreign: 8}
    monkeypatch.setattr(m, "get_store", lambda _х=_IndexOfTwoOwners(owners): _х)
    monkeypatch.setattr(m, "get_agent", lambda: _AgentThatSaysNothing())
    for doc_id in owners:
        (tmp_path / f"{doc_id}.txt").write_text("===PAGE 1===\nтекст\n", encoding="utf-8")

    await m.chat_turn_start(
        QueryRequest(message="извлеки показатели", arch="aifa_gateway",
                     doc_ids=[mine, foreign], active_doc_id=foreign),
        me,
    )
    scoped = await _scope_of_the_started_turn()

    assert scoped, "ход не взвёл ни одного канала — тест проверяет не то, ради чего написан"
    assert mine in scoped, "свой документ выпал из скоупа — ход останется без текста"
    assert foreign not in scoped, (
        "чужой документ попал в скоуп нонса — по нему комната получит текст "
        "чужого отчёта через /internal/doc-text (security.md §3.1)"
    )

    nonce = next(n for n, docs in internal_docs.nonce_scopes().items() if mine in docs)
    with pytest.raises(HTTPException) as denied:
        await internal_docs.doc_text(foreign, nonce=nonce, authorization="")
    assert denied.value.status_code == 403
    assert await internal_docs.doc_text(mine, nonce=nonce, authorization=""), (
        "свой документ тоже перестал выдаваться — фильтр съел законный ход")


async def test_a_followup_turn_reopens_scope_for_the_session_document(
        monkeypatch, tmp_path):
    """Короткий второй ход не повторяет ``doc_ids``, но станции нужен текст.

    Реальный сценарий: первый ход вернул ``goal_needs_clarification``, второй
    содержит только «6М 2026». Агент помнит документ в Session и потому строит
    промпт/гейт по нему, а `/chat/turns` прежде открывал nonce-скоуп только из
    пустого тела ВТОРОГО запроса. Комната получала 403 после успешного OCR.

    Проверяем настоящий путь ``/chat/turns -> station_turn.arm -> doc_text``.
    Откат: снова собирать ``_turn_docs`` только из тела запроса — тест обязан
    упасть на пустом scope.
    """
    from finengine.api import main as m
    from finengine.auth.db import User
    from finengine.config import settings
    from finengine.models.query import QueryRequest
    from finengine.skills import grounding_bus, run_logger

    doc_id, foreign = "doc-session", "doc-foreign-session"
    me = User(id=7, username="u7")
    agent = _AgentWithRememberedDocument(doc_id, foreign)

    monkeypatch.setattr(settings, "docs_dir", tmp_path)
    monkeypatch.setattr(run_logger, "_LOG_PATH", tmp_path / "extract_runs.jsonl")
    monkeypatch.setattr(run_logger, "_CHAT_LOG_PATH", tmp_path / "chat_runs.jsonl")
    monkeypatch.delenv("FINENGINE_OPENCLAW_GATEWAY_TOKEN", raising=False)
    grounding_bus._worker_docs.clear()
    monkeypatch.setattr(
        m, "get_store",
        lambda _x=_IndexOfTwoOwners({doc_id: me.id, foreign: 8}): _x,
    )
    monkeypatch.setattr(m, "get_agent", lambda: agent)
    (tmp_path / f"{doc_id}.txt").write_text(
        "===PAGE 1===\nтекст отчёта\n", encoding="utf-8")

    request = QueryRequest(message="6М 2026", session_id="session-1",
                           arch="aifa_gateway")
    turn_docs = m._chat_turn_docs(request, me, agent)
    cell: dict = {}
    station_turn.arm(
        nonce=NONCE, gw_key=GW_KEY, doc_ids=turn_docs, owner_id=me.id,
        project_id="project-1", cell=cell,
    )
    try:
        assert doc_id in internal_docs.nonce_scopes().get(NONCE, set()), (
            "документ сохранён в Session, но новый nonce его не разрешает — "
            "станция получит 403 на втором ходе")
        assert foreign not in internal_docs.nonce_scopes().get(NONCE, set()), (
            "сохранённый Session обошёл повторную owner-проверку")
        assert await internal_docs.doc_text(
            doc_id, nonce=NONCE, authorization="")
        with pytest.raises(HTTPException) as denied:
            await internal_docs.doc_text(
                foreign, nonce=NONCE, authorization="")
        assert denied.value.status_code == 403
    finally:
        station_turn.disarm(cell, stopped=True)


class _IndexOfTwoOwners:
    """Индекс документов из двух отчётов разных владельцев."""

    def __init__(self, owners: dict[str, int]) -> None:
        self._owners = owners

    def get_document(self, doc_id: str):
        owner = self._owners.get(doc_id)
        if owner is None:
            return None
        return SimpleNamespace(doc_id=doc_id, owner_id=owner, company_name="",
                               filename=f"{doc_id}.pdf")


class _AgentThatSaysNothing:
    """Модель хода не предмет: канал взводится ДО первого её события."""

    async def query_stream(self, **_kwargs):
        return
        yield {}  # недостижимо; нужен, чтобы метод стал асинхронным генератором


class _AgentWithRememberedDocument(_AgentThatSaysNothing):
    """Минимальная Session-граница настоящего агента для второго хода."""

    def __init__(self, *doc_ids: str) -> None:
        self.doc_ids = list(doc_ids)

    def session_doc_ids(self, session_id: str, *, auth_user_id: int | None = None):
        assert session_id == "session-1"
        assert auth_user_id == 7
        return list(self.doc_ids)


async def _scope_of_the_started_turn(timeout: float = 3.0) -> set[str]:
    """Дождаться взвода канала фоновым ходом и вернуть все документы скоупа.

    Ход — фоновый прогон (`run_orchestrator.start_run`), POST возвращает
    `run_id` до того, как раннер дошёл до `arm`. Ждём по факту появления
    скоупа, а не паузой на глазок.
    """
    deadline = asyncio.get_running_loop().time() + timeout
    while not internal_docs.nonce_scopes() and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.01)
    return {d for docs in internal_docs.nonce_scopes().values() for d in docs}


def test_the_admin_keeps_the_documents_the_filter_would_cut(monkeypatch):
    """Фильтр владельца не имеет права отрезать станцию у админа.

    РИСК, НАЗВАННЫЙ ПРИ РАЗБОРЕ Б8.1. Админ ходит по чужим и по легаси-отчётам
    (`owner_id=0`, документы, заведённые до появления поля) — это его штатная
    работа. Возьми фильтр строже, чем `_user_can_see`, и у админа отвалится
    станция ровно на старых документах, причём молча: скоуп пуст, комната
    получает 403, а выглядит это как «извлечение не работает».

    ПРОВЕРКА СТОРОЖА ОТКАТОМ: убрать `if user.is_admin: return True` из
    `main._user_can_see` — тест краснеет.
    """
    from finengine.api import main as m
    from finengine.auth.db import User

    legacy, someone_elses = "doc-legacy", "doc-of-user-8"
    monkeypatch.setattr(m, "get_store",
                        lambda _х=_IndexOfTwoOwners({legacy: 0, someone_elses: 8}): _х)

    admin = User(id=1, username="admin", is_admin=True)
    assert m._docs_of_the_turn([legacy, someone_elses], admin) == [legacy, someone_elses]

    stranger = User(id=8, username="u8")
    assert m._docs_of_the_turn([legacy, someone_elses], stranger) == [someone_elses]


async def test_a_foreign_document_never_enters_the_scope_of_the_legacy_pool(
        monkeypatch, tmp_path):
    """НЕгейтвейный ход: чужой doc_id не попадает в скоуп фонового пула нонсов.

    ВТОРАЯ ДВЕРЬ ТОГО ЖЕ ДЕФЕКТА (Б8.1, найдена состязательной проверкой
    20.08.2026). `station_turn.arm` — не единственный писатель
    скоуп нонсов. Когда `gateway_session_key` пуст, документы в скоуп
    регистрирует легаси-пул фоновых нонсов внутри `agent.query_stream`, и берёт
    он их из `session.doc_ids`, то есть ДОСЛОВНО из тела запроса
    (`agent.query_stream`: `if doc_ids: session.doc_ids = doc_ids`). Первая
    редакция правки закрыла только `arm` — а пустой ключ бывает на `/query`, на
    `/query/stream` и на `/chat/turns` с `pinned_tool`, то есть на кнопках
    интерфейса. Чужой отчёт по-прежнему уезжал в комнату.

    ХОДИМ БОЕВЫМ ПУТЁМ: настоящий роут `/query/stream`, настоящий
    `FinengineAgent.query_stream`, настоящий `register_nonce_docs`, настоящий
    `/internal/doc-text`. Подменены только внешние опоры — индекс, сборка
    контекста и вызов модели.

    ПРОВЕРКА СТОРОЖА ОТКАТОМ: вернуть в `main.query_stream`
    `doc_ids=request.doc_ids or None` — тест обязан покраснеть на том, что
    чужой документ оказался в скоупе и выдался комнате.
    """
    from finengine.api import main as m
    from finengine.auth.db import User
    from finengine.config import settings
    from finengine.models.query import QueryRequest
    from finengine.orchestrator import agent as ag
    from finengine.skills import grounding_bus, run_logger

    mine, foreign = "doc-mine", "doc-foreign"
    me = User(id=7, username="u7")

    # Срез 31: каждое сообщение сначала разбирает модель-маршрутизатор; тест про
    # скоуп фонового пула нонсов, поэтому маршрут «ответить самой» подменён.
    async def _answer(system, prompt, *, model_version=None):
        return '{"route": "answer", "intent_kind": null, "targets": []}'

    monkeypatch.setattr("finengine.skills.atoms.intent._default_complete", _answer)
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr(settings, "docs_dir", tmp_path)
    monkeypatch.setattr(run_logger, "_CHAT_LOG_PATH", tmp_path / "chat_runs.jsonl")
    monkeypatch.delenv("FINENGINE_OPENCLAW_GATEWAY_TOKEN", raising=False)
    grounding_bus._worker_docs.clear()

    monkeypatch.setattr(m, "get_store",
                        lambda _х=_IndexOfTwoOwners({mine: 7, foreign: 8}): _х)
    monkeypatch.setattr(m, "get_agent", lambda: ag.FinengineAgent())
    monkeypatch.setattr(ag, "_build_context", lambda *_a, **_kw: ("текст отчёта", []))
    monkeypatch.setattr(ag.llm, "stream_events", _a_model_that_says_nothing)
    for doc_id in (mine, foreign):
        (tmp_path / f"{doc_id}.txt").write_text("===PAGE 1===\nтекст\n", encoding="utf-8")

    response = await m.query_stream(
        QueryRequest(message="извлеки показатели", doc_ids=[mine, foreign]), me)
    async for _chunk in response.body_iterator:
        pass

    assert internal_docs.nonce_scopes(), (
        "ход не открыл ни одного скоупа — тест проверяет не то, ради чего написан: "
        "пул фоновых нонсов не сработал, и «чужого нет» вышло бы даром")
    scoped = {d for docs in internal_docs.nonce_scopes().values() for d in docs}
    assert mine in scoped, "свой документ выпал из скоупа — ход останется без текста"
    assert foreign not in scoped, (
        "чужой документ попал в скоуп фонового нонса — по нему комната получит "
        "текст чужого отчёта через /internal/doc-text (security.md §3.1)")

    nonce = next(iter(internal_docs.nonce_scopes()))
    with pytest.raises(HTTPException) as denied:
        await internal_docs.doc_text(foreign, nonce=nonce, authorization="")
    assert denied.value.status_code == 403
    assert await internal_docs.doc_text(mine, nonce=nonce, authorization=""), (
        "свой документ тоже перестал выдаваться — фильтр съел законный ход")


async def _a_model_that_says_nothing(**_kwargs):
    """Модель хода не предмет: скоуп открывается до первого её события."""
    yield {"type": "done", "usage": {}}


async def test_the_pinned_tool_turn_hands_the_agent_the_same_docs_as_the_station(
        monkeypatch, tmp_path):
    """Обе половины хода описывают ОДИН набор документов — и на кнопке тоже.

    ДВЕ ПРЕТЕНЗИИ ОДНИМ СТОРОЖЕМ (20.08.2026).

    ПЕРВАЯ — кнопка. `POST /chat/turns` с `pinned_tool` гейтвей-петлю не
    включает (`use_gateway_loop = … and not request.pinned_tool`), `gw_key`
    пуст, `arm` не зовётся вовсе — и весь фильтр, стоявший ТОЛЬКО перед `arm`,
    оказывался мимо. Список из тела запроса уезжал в `session.doc_ids`, а
    оттуда в скоуп фонового пула.

    ВТОРАЯ — расхождение половин. В агента уходил `request.doc_ids`
    НЕотфильтрованным, тогда как в станцию — отфильтрованный: состав сессии (он
    же ложится на диск в файл сессии) и скоуп станции ОДНОГО хода описывали
    разные наборы. Симметрия этих двух — тот самый довод, которым обоснована
    молчаливая фильтрация в `_docs_of_the_turn`.

    ПРОВЕРКА СТОРОЖА ОТКАТОМ: вернуть в `main.chat_turn_start`
    `doc_ids=request.doc_ids or None` в вызове `agent.query_stream` — тест
    краснеет на том, что агент получил чужой документ.
    """
    from finengine.api import main as m
    from finengine.auth.db import User
    from finengine.config import settings
    from finengine.models.query import QueryRequest
    from finengine.skills import run_logger

    mine, foreign = "doc-mine", "doc-foreign"
    me = User(id=7, username="u7")

    monkeypatch.setattr(settings, "docs_dir", tmp_path)
    monkeypatch.setattr(run_logger, "_LOG_PATH", tmp_path / "extract_runs.jsonl")
    monkeypatch.setattr(run_logger, "_CHAT_LOG_PATH", tmp_path / "chat_runs.jsonl")
    monkeypatch.setattr(m, "get_store",
                        lambda _х=_IndexOfTwoOwners({mine: 7, foreign: 8}): _х)

    agent = _AgentThatRemembersItsDocuments()
    monkeypatch.setattr(m, "get_agent", lambda: agent)

    await m.chat_turn_start(
        QueryRequest(message="извлеки показатели", pinned_tool="fe_calc",
                     doc_ids=[mine, foreign], active_doc_id=foreign),
        me,
    )
    seen = await agent.wait()

    assert seen["doc_ids"] == [mine], (
        "агенту отдали чужой документ: он ляжет в session.doc_ids, а оттуда — "
        "в скоуп фонового пула нонсов (agent.query_stream)")
    assert seen["active_doc_id"] == "", (
        "чужой документ остался активным — ход будет считать по чужому отчёту")


class _AgentThatRemembersItsDocuments:
    """Агент-шпион: предмет теста — ЧТО роут ему отдал, а не что он ответил."""

    def __init__(self) -> None:
        self.seen: dict = {}
        self.called = asyncio.Event()

    async def query_stream(self, **kwargs):
        self.seen = {"doc_ids": list(kwargs.get("doc_ids") or []),
                     "active_doc_id": kwargs.get("active_doc_id") or ""}
        self.called.set()
        return
        yield {}  # недостижимо; нужен, чтобы метод стал асинхронным генератором

    def query(self, **kwargs) -> dict:
        """Непотоковая ручка зовёт агента синхронно — её список тоже предмет."""
        self.seen = {"doc_ids": list(kwargs.get("doc_ids") or []),
                     "active_doc_id": kwargs.get("active_doc_id") or ""}
        return {"session_id": "s", "answer": "", "sources": []}

    async def wait(self, timeout: float = 3.0) -> dict:
        await asyncio.wait_for(self.called.wait(), timeout)
        return self.seen


async def test_the_non_streaming_query_filters_the_documents_too(monkeypatch, tmp_path):
    """Третья дверь той же комнаты: `POST /query` отдаёт агенту то же, что и все.

    `agent.query` — обёртка над тем же `query_stream` (`agent.py`: собирает его
    события в один ответ), то есть тот же `session.doc_ids` и тот же пул
    фоновых нонсов. Дверь тише двух других, но открыта ровно так же, и чинить
    её отдельным способом нечем: фильтр один на все три роута.

    ПРОВЕРКА СТОРОЖА ОТКАТОМ: вернуть в `main.query` голое
    `doc_ids=request.doc_ids or None` — тест краснеет.
    """
    from finengine.api import main as m
    from finengine.auth.db import User
    from finengine.models.query import QueryRequest

    mine, foreign = "doc-mine", "doc-foreign"
    monkeypatch.setattr(m, "get_store",
                        lambda _х=_IndexOfTwoOwners({mine: 7, foreign: 8}): _х)
    agent = _AgentThatRemembersItsDocuments()
    monkeypatch.setattr(m, "get_agent", lambda: agent)

    await m.query(QueryRequest(message="сколько выручки", doc_ids=[mine, foreign]),
                  User(id=7, username="u7"))

    assert agent.seen["doc_ids"] == [mine], (
        "непотоковая ручка отдала агенту чужой документ")
