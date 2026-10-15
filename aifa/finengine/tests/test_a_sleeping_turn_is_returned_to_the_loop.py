"""Ход, уснувший при готовой таблице, возвращается в петлю, а не хоронится.

ДЕФЕКТ (19.08.2026, Банк «Санкт-Петербург»). Модель запустила извлечение,
написала «жду результата» и ЗАКОНЧИЛА ХОД. Станция отработала: 110 показателей,
23 сегментные строки, `metrics.json` на 121 917 байт. Расчёт, который по
эстафете ставит терминал, не запустил никто. Сток досидел дедлайн и закрыл
прогон отказом. На экране у пользователя — отказ при полностью удавшейся работе.

ПОЧЕМУ НЕ ХУКОМ ЯДРА. `before_agent_finalize` умеет просить у модели ещё один
проход, но ядро отказывает, если в ходе был побочный эффект, а `exec` помечен
НЕ replay-safe (`isReplaySafeToolCall`: `case "exec": return false`). То есть
хук откажет ровно после того шага, ради которого нужен. Разбор —
`docs/УПРАВЛЕНИЕ_ПЛАНОМ_ЯДРОМ_21-08.md`.

ПОТОЛОК — РЕШЕНИЕ ВЛАДЕЛЬЦА 21.08: три продолжения на ключ сессии.

ЧТО ЗДЕСЬ СТЕРЕЖЁТСЯ — ПОВЕДЕНИЕ, А НЕ ТЕКСТ В ФАЙЛЕ. Зовётся настоящий
`_remember_partial` (адресат стока на «прогон замолчал»), подделан ровно шлюз
гейтвея; проверяется, ушёл ли новый ход и с каким ключом сессии.

ПРОВЕРКА СТОРОЖЕЙ ОТКАТОМ (мутации прогнаны 21.08.2026, каждая покраснела):
  · `_may_continue` → `return False` → падают три теста продолжения;
  · снять проверку `if not getattr(run, "metrics", None)` → падает
    `test_an_empty_run_is_not_continued_blindly`;
  · снять проверку потолка → падает `test_the_fourth_time_the_turn_is_buried`;
  · снять проверку `channel.turn_over` → падает
    `test_a_turn_still_running_is_not_pushed`;
  · продолжение уходит с ПУСТЫМ ключом сессии → падает
    `test_the_continuation_stays_in_the_same_conversation`.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

import pytest

from finengine.api import station_turn as st


@dataclass
class _Run:
    metrics: list = field(default_factory=list)
    doc_id: str = "doc-1"
    summary: dict = field(default_factory=dict)


def _channel(gw_key: str = "agent:aifa-station:openresponses:fe-deadbeef",
             *, turn_over: bool = True) -> st.TurnChannel:
    ch = st.TurnChannel(
        nonce="deadbeef" * 4, gw_key=gw_key, queue=None, owner_id=7,
        project_id="p1", run_cell={"run_id": "r1"}, started_at=0.0,
        kind=st.KIND_EXTRACT)
    ch.turn_over = turn_over
    ch.saw_life = True
    return ch


@pytest.fixture(autouse=True)
def _clean_counters():
    st._continuations.clear()
    yield
    st._continuations.clear()


@pytest.fixture()
def spy(monkeypatch):
    """Подделан ровно шлюз гейтвея; всё остальное — настоящий код модуля."""
    sent: list[dict] = []

    async def _fake_stream(*, prompt: str, gateway_session_key: str | None = None, **kw):
        sent.append({"prompt": prompt, "gw_key": gateway_session_key})
        return
        yield  # noqa: PLE0101 — делает функцию асинхронным генератором

    saved: list[tuple] = []
    monkeypatch.setattr("finengine.llm.openclaw.stream_events", _fake_stream)
    monkeypatch.setattr(st, "arm", lambda **kw: saved.append(("arm", kw)))
    monkeypatch.setattr(st, "_remember_screen",
                        lambda channel, run, *, done, status: saved.append(("screen", status)))
    return {"sent": sent, "saved": saved}


@pytest.mark.asyncio
async def test_a_full_table_without_a_terminal_returns_the_agent_to_the_loop(spy):
    """Главный случай: строки приехали, терминала нет — новый ход, не отказ."""
    channel, run = _channel(), _Run(metrics=[{"enum": "REVENUE"}] * 110)
    st._remember_partial(channel, run)
    await asyncio.sleep(0)

    assert spy["sent"], (
        "ход замолчал при 110 готовых строках, а продолжения не ушло — "
        "пользователь снова получит отказ при удавшейся работе")
    статусы = [s for kind, s in spy["saved"] if kind == "screen"]
    assert "failed" not in статусы, (
        f"прогон записан как {статусы} — продолжение идёт, хоронить рано")


@pytest.mark.asyncio
async def test_the_continuation_stays_in_the_same_conversation(spy):
    """Тот же ключ сессии: модель обязана видеть свой план и уже сделанное."""
    channel, run = _channel(), _Run(metrics=[{"enum": "REVENUE"}])
    st._remember_partial(channel, run)
    await asyncio.sleep(0)

    assert spy["sent"][0]["gw_key"] == channel.gw_key, (
        f"продолжение ушло с ключом {spy['sent'][0]['gw_key']!r} вместо "
        f"{channel.gw_key!r} — это новый разговор с чистого листа, модель "
        "перезапустит извлечение")


@pytest.mark.asyncio
async def test_the_continuation_forbids_rerunning_the_extraction(spy):
    """Иначе продолжение удвоит работу и деньги — дефект #160, 253 секунды."""
    channel, run = _channel(), _Run(metrics=[{"enum": "REVENUE"}] * 42)
    st._remember_partial(channel, run)
    await asyncio.sleep(0)

    prompt = spy["sent"][0]["prompt"]
    assert "НЕ ПЕРЕЗАПУСКАЙ" in prompt, "запрет на повтор отработавшего пропал"
    assert "42" in prompt, "модели не сказано, сколько строк уже приехало"


@pytest.mark.asyncio
async def test_the_continuation_does_not_dictate_the_next_step(spy):
    """Толкатель будит агента, а не диктует маршрут.

    Первая редакция подсказывала «обычно это расчёт коэффициентов» — то есть
    зашивала наш конвейер в текст. Решение владельца 21.08: маршрут выбирает
    модель по СВОЕМУ плану. Пользователь мог просить только таблицу, и тогда
    верный следующий шаг — ответить, а не считать.
    """
    channel, run = _channel(), _Run(metrics=[{"enum": "REVENUE"}])
    st._remember_partial(channel, run)
    await asyncio.sleep(0)

    prompt = spy["sent"][0]["prompt"]
    названо = [имя for имя in ("расчёт коэффициент", "aifa-calc", "aifa-extract",
                               "aifa-analyze", "run.sh", "--metrics", "--out-json")
               if имя.lower() in prompt.lower()]
    assert not названо, (
        f"продолжение диктует маршрут вместо того, чтобы разбудить: {названо}. "
        "Так конвейер снова окажется зашит в текст")
    assert "СВОЙ план" in prompt, "модель не отправлена к собственному плану"


@pytest.mark.asyncio
async def test_an_empty_run_is_not_continued_blindly(spy):
    """Строк нет — продолжать нечем; «попробуй ещё раз» вслепую не толкатель."""
    channel, run = _channel(), _Run(metrics=[])
    st._remember_partial(channel, run)
    await asyncio.sleep(0)

    assert not spy["sent"], "пустой прогон продолжен вслепую"
    assert ("screen", "failed") in spy["saved"], "пустой прогон обязан закрыться отказом"


@pytest.mark.asyncio
async def test_a_turn_still_running_is_not_pushed(spy):
    """Ход идёт — модель доведёт сама, а второй ход столкнулся бы с первым."""
    channel, run = _channel(turn_over=False), _Run(metrics=[{"enum": "REVENUE"}])
    st._remember_partial(channel, run)
    await asyncio.sleep(0)

    assert not spy["sent"], "продолжение ушло поверх идущего хода"


@pytest.mark.asyncio
async def test_a_non_gateway_run_is_not_continued(spy):
    """Без ключа сессии продолжать некуда — это не гейтвейный путь."""
    channel, run = _channel(gw_key=""), _Run(metrics=[{"enum": "REVENUE"}])
    st._remember_partial(channel, run)
    await asyncio.sleep(0)

    assert not spy["sent"]


@pytest.mark.asyncio
async def test_the_fourth_time_the_turn_is_buried(spy):
    """Потолок владельца — три. Без него непонимание модели станет петлёй."""
    run = _Run(metrics=[{"enum": "REVENUE"}])
    channel = _channel()
    for _ in range(4):
        st._remember_partial(channel, run)
        await asyncio.sleep(0)

    assert len(spy["sent"]) == 3, (
        f"продолжений {len(spy['sent'])} вместо трёх — потолок не держит, "
        "и каждый лишний заход это деньги и минуты")
    assert st.continuations_used(channel.gw_key) == 3
    assert ("screen", "failed") in spy["saved"], (
        "исчерпав потолок, ход обязан закрыться честным отказом")


@pytest.mark.asyncio
async def test_a_finished_run_after_the_turn_also_reports_back(spy):
    """🔴 ЖИВОЙ ДЕФЕКТ 22.08: удача молчала так же, как молчал провал.

    Прогон владельца по Северстали закончился УДАЧНО — 60 показателей, Excel
    собран, прогон записан `done`. А в чате не появилось НИ ОДНОГО СЛОВА: итог
    ушёл в историю сессии, чтобы его пересказал СЛЕДУЮЩИЙ ход. Следующего хода
    может не быть никогда, и пользователь смотрит на готовую таблицу и молчащий
    чат.

    Толкатель ловил только «прогон замолчал». Удачно кончившийся в него не
    попадал, хотя молчание перед пользователем ровно то же.
    """
    channel, run = _channel(), _Run(metrics=[{"enum": "REVENUE"}] * 60)
    assert st._may_report_back(channel, run), (
        "удачный прогон после конца хода не считается поводом вернуть агента — "
        "пользователь снова получит таблицу и молчание")
    st._continue_turn(channel, run, finished=True)
    await asyncio.sleep(0)

    assert spy["sent"], "агент не возвращён в петлю после удачного прогона"
    prompt = spy["sent"][0]["prompt"]
    assert "ЗАКОНЧИЛАСЬ УДАЧНО" in prompt, (
        "модели сказано про обрыв, а прогон УДАЛСЯ — она станет чинить целое")
    assert "60" in prompt, "не сказано, сколько строк уже готово"
    assert "НЕ ПЕРЕЗАПУСКАЙ" in prompt, "нет запрета на повтор готовой работы"


@pytest.mark.asyncio
async def test_a_finished_run_inside_a_live_turn_is_not_pushed(spy):
    """Ход ИДЁТ — модель ответит сама, второй ход столкнулся бы с первым."""
    channel, run = _channel(turn_over=False), _Run(metrics=[{"enum": "REVENUE"}])
    assert not st._may_report_back(channel, run)


@pytest.mark.asyncio
async def test_the_answer_of_the_continuation_is_kept(spy, monkeypatch):
    """🔴 ЖИВОЙ ДЕФЕКТ 23.08: агент ответил, а слова выбросили.

    Толкатель вернул агента в петлю, агент за минуту написал полный разбор по
    пяти коэффициентам — и текст остался ТОЛЬКО в логе гейтвея: продолжение
    читало поток `async for _ in …: pass`. Владелец десять минут смотрел в
    молчащий чат при готовой работе.

    Объяснение в коде было наполовину верным: SSE и правда закрыт вместе с
    ходом. Но «результат он увидит из сохранённого исхода» — нет: там ТАБЛИЦА,
    а не слова. Слова кладём в серверную сессию, её отдаёт `/chat/history`.
    """
    сохранено: list[tuple[str, str]] = []

    class _Session:
        def add(self, role, content):  # noqa: ANN001
            сохранено.append((role, content))

    monkeypatch.setattr("finengine.api.main.get_agent",
                        lambda: type("A", (), {"get_or_create_session":
                                               lambda _s, _sid, auth_user_id=None: _Session()})())
    monkeypatch.setattr("finengine.orchestrator.agent._save_session", lambda _s: None)

    channel = _channel()
    channel.session_id = "sess-1"
    st._remember_answer(channel, "  Вот расчёты по отчёту: ROCE 18,1%.  ")
    assert сохранено == [("assistant", "Вот расчёты по отчёту: ROCE 18,1%.")], (
        "ответ продолжения не лёг в сессию — в чате снова будет тишина при "
        "готовой работе")
    assert "answer" in channel.delivered


@pytest.mark.asyncio
async def test_an_empty_continuation_is_not_written_as_a_message(spy, monkeypatch):
    """Пустой ответ — не сообщение. Пустой пузырь в ленте хуже отсутствия."""
    сохранено: list = []

    class _Session:
        def add(self, role, content):  # noqa: ANN001
            сохранено.append((role, content))

    monkeypatch.setattr("finengine.api.main.get_agent",
                        lambda: type("A", (), {"get_or_create_session":
                                               lambda _s, _sid, auth_user_id=None: _Session()})())
    monkeypatch.setattr("finengine.orchestrator.agent._save_session", lambda _s: None)
    channel = _channel()
    channel.session_id = "sess-1"
    st._remember_answer(channel, "   ")
    assert not сохранено


@pytest.mark.asyncio
async def test_the_cap_is_per_conversation(spy):
    """Счётчик на КЛЮЧ СЕССИИ: чужой ход не тратит наш потолок."""
    run = _Run(metrics=[{"enum": "REVENUE"}])
    a, b = _channel("agent:…:fe-aaaa"), _channel("agent:…:fe-bbbb")
    for _ in range(3):
        st._remember_partial(a, run)
        await asyncio.sleep(0)
    st._remember_partial(b, run)
    await asyncio.sleep(0)

    assert st.continuations_used(a.gw_key) == 3
    assert st.continuations_used(b.gw_key) == 1, (
        "потолок оказался общим на всех — один длинный разговор перекрыл бы "
        "продолжения всем остальным пользователям")


# ─── окно молчания после конца хода: сжимаем, только пока НИЧЕГО не пришло ───
#
# 🔴 ЖИВОЙ ПРОГОН ВЛАДЕЛЬЦА 23.08.2026, НОНС 66e5e30f. Станция отдала 28 строк,
# ушла в тихий хвост (сегменты и расчёт), и терминал был бы в 17:46:00 — а
# дренаж сдался в 17:45:50. ДЕСЯТЬ СЕКУНД. Человек получил таблицу и молчащий
# чат, а прогон записан упавшим при полностью сделанной работе.
#
# ⚠️ ЧИСЛО Я К ТОМУ МОМЕНТУ УЖЕ ПОДНИМАЛ ОДНАЖДЫ (60 → 420), и второй промах
# показывает, что неверна не величина, а посылка: молчание после конца хода НЕ
# отличает мёртвый прогон от живого. Отличает другое — пришло ли хоть что-то.
#
# ПРОВЕРКА ОТКАТОМ (23.08.2026): сжимать окно независимо от строк → падает
# `test_a_run_that_already_sent_rows_is_given_the_full_window`; убрать сжатие
# вовсе → падает `test_a_run_that_sent_nothing_is_still_cut_short`.


def test_a_run_that_already_sent_rows_is_given_the_full_window():
    """Строки идут — комната работает, и хвост её работы законно тихий."""
    channel = _channel()
    короткое = st._quiet_after_turn_for(channel)
    assert короткое(True) is None, (
        "окно сжато при уже приехавших строках — ровно так 23.08 потерян "
        "прогон, которому до терминала оставалось десять секунд")


def test_a_run_that_sent_nothing_is_still_cut_short():
    """Ничего не пришло — ждать нечего: ради этого сжатие и заводилось."""
    channel = _channel()
    короткое = st._quiet_after_turn_for(channel)
    assert короткое(False) == st._QUIET_AFTER_TURN_SEC


def test_a_live_turn_never_shortens_the_window():
    """Ход ИДЁТ — комната считает молча, и это норма при любых строках."""
    channel = _channel(turn_over=False)
    короткое = st._quiet_after_turn_for(channel)
    assert короткое(True) is None and короткое(False) is None
