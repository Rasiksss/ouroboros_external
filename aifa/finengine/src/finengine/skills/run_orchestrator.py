"""Generic resumable run orchestration.

Generalised from the chain-only `chain_orchestrator` so that every
streaming endpoint (extract / chat / quick_synthesis / analyze_chains)
shares one background-task + journal + resume pattern.

How it works:

1. `start_run(runner, kind=…, meta=…)` spawns the runner as an
   `asyncio.create_task` *independent of the request that started it*.
   Client disconnect kills the request, not the task.
2. Each event the runner emits is:
   - appended to `data/documents/{kind}_events/{run_id}.jsonl`
   - dispatched to all live `asyncio.Queue` observers
3. `subscribe(run_id, kind=…)` returns an async generator that yields
   every event already in the journal (catch-up) then tails new events
   from a fresh queue until the run finishes — works whether you
   connect at start, mid-run or after completion.
4. On backend startup, `recover_orphan_runs()` scans all known kinds
   and appends a synthetic `session_lost` + `done` to any journal
   that died mid-stream so the next subscriber can finalise instead
   of spinning.

Storage layout — one directory per kind, sibling to docs_dir:
  data/documents/chain_events/{run_id}.jsonl
  data/documents/extract_events/{run_id}.jsonl
  data/documents/quick_events/{run_id}.jsonl
  data/documents/chat_events/{run_id}.jsonl

The kind directory is created on first write. Legacy chain_events stays
where it was so existing journals are unaffected.

🔴 ПОЧЕМУ ЗДЕСЬ ТРИ РАЗНЫХ МЕСТА, А НЕ ОДНО (правило 3.2, `>1` реплика).
Прогон рождается на ОДНОЙ реплике, а подписка на него может прийти на ЛЮБУЮ.
Перенести `asyncio.Task` между процессами нельзя, поэтому разнесены роли:

  * ЗАДАЧА И ЛОКАЛЬНЫЕ НАБЛЮДАТЕЛИ — в памяти реплики-владельца (`_runs`).
    Это не пережиток: только владелец может отменить свою задачу;
  * СОСТОЯНИЕ ПРОГОНА (жив/закончен, вид, ошибка) — в общем кэше
    (`store/cache`). По нему любая реплика решает, стоит ли ждать хвост;
  * СОБЫТИЯ — в журнале (догон) И в шине (живой хвост). Две дороги, потому
    что у каждой своя дыра: шина НЕ ХРАНИТ (подписавшийся позже не увидит
    прошлого), а журнал НЕ БУДИТ (по нему не дождаться следующего события).

⚠️ ПОРЯДОК ПОДПИСКИ НЕСУЩИЙ: открыть хвост → прочитать журнал → слушать.
Обратный порядок теряет события, случившиеся между чтением журнала и
подпиской, — «пропало ровно одно событие в середине», отказ, который ловится
глазами и не воспроизводится. Номер события (`seq`) закрывает вторую дыру:
догон говорит, до какого номера уже прочитано, и хвост отбрасывает повторы.
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
import uuid
from pathlib import Path
from typing import Any, AsyncGenerator, Awaitable, Callable, Iterable

from finengine.config import settings

log = logging.getLogger(__name__)


# Known run kinds. Each maps to its events directory under docs_dir.
# Extending: add a new kind here, and any code that wants to enumerate
# all run journals (orphan recovery) picks it up automatically.
KNOWN_KINDS: tuple[str, ...] = ("chain", "extract", "quick", "chat", "synth", "ir_calendar", "scenario")


def _events_dir(kind: str) -> Path:
    """Прежний каталог журналов на томе. Нужен для ЧТЕНИЯ старых прогонов."""
    return settings.docs_dir / f"{kind}_events"


def event_file(run_id: str, kind: str) -> Path:
    """Прежний путь журнала прогона. Нужен для чтения старых прогонов."""
    return _events_dir(kind) / f"{run_id}.jsonl"


def вид_журнала(kind: str) -> str:
    """Имя вида в общем журнале. Собирается ОДНОЙ функцией."""
    return f"run_{kind}"


# 🔴 ВЛАДЕЛЕЦ У ЖУРНАЛА ПРОГОНА ОБЩИЙ, И ЭТО НЕ НЕДОСМОТР. Подписка ищет прогон
# по одному `run_id` — владельца она не знает и знать не должна: доступ
# проверяют роуты (`чужой_прогон`), по общей записи прогона. Класть журнал под
# владельца значило бы требовать его там, где его нет, ради разграничения,
# которое уже сделано выше.
_ВЛАДЕЛЕЦ = "_"

_FILE_LOCK = threading.Lock()


def append_event(run_id: str, kind: str, event: dict) -> None:
    """Дописать событие в журнал прогона. Отказ журнала не роняет прогон.

    🔴 ЖУРНАЛ ПЕРЕЕХАЛ С ТОМА В ОБЩЕЕ ХРАНИЛИЩЕ (26.08.2026). Пока он был
    файлом, реплики могли жить только в ОДНОМ контейнере: воркеры uvicorn
    делят том, и догон подписки работал. Разнести их по машинам было нельзя —
    подписчик на чужой машине не увидел бы журнала вовсе и отдал бы только
    хвост, то есть ответ без начала.

    Правило `docs/rules/architecture.md` 3.2 называет это прямо: «журналы →
    spool→S3». Мы кладём в тот же `store/journals`, что следы, аудит и расход:
    у прогона ровно та же форма — дописывание строки и чтение по ключу.
    """
    from finengine.store import journals  # noqa: PLC0415

    journals.append(вид_журнала(kind), owner=_ВЛАДЕЛЕЦ, key=run_id, entry=event)


# ── Per-run state ─────────────────────────────────────────────────────────
class RunState:
    """Mutable state for a single run, regardless of kind."""

    def __init__(self, run_id: str, kind: str, meta: dict):
        self.run_id = run_id
        self.kind = kind
        self.meta = meta
        self.events: list[dict] = []
        self.observers: list[asyncio.Queue[dict | None]] = []
        # #84 C3-1: живой asyncio-таск прогона — для cancel_run (кнопка «Стоп»).
        self.task: "asyncio.Task | None" = None
        # Слушатель канала управления. Живёт ровно столько же, сколько прогон.
        self.control: "asyncio.Task | None" = None
        self.finished: bool = False
        self.started_at: float = time.time()
        self.finished_at: float | None = None
        self.error: str | None = None
        self.seq: int = 0

    def emit(self, event: dict) -> None:
        """Журнал + память + локальные наблюдатели + шина.

        Номер события присваивается ЗДЕСЬ и только здесь: он же уходит в шину,
        и по нему подписчик на чужой реплике отбрасывает то, что уже прочитал
        из журнала.
        """
        self.seq += 1
        self.events.append(event)
        append_event(self.run_id, self.kind, event)
        for q in list(self.observers):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                pass
        _огласить(self.run_id, self.seq, event)

    def close(self, error: str | None = None) -> None:
        """Mark finished. All observers unblock with sentinel `None`."""
        self.finished = True
        self.error = error
        self.finished_at = time.time()
        _записать_состояние(self.run_id, kind=self.kind, finished=True,
                            error=error, seq=self.seq)
        for q in list(self.observers):
            try:
                q.put_nowait(None)
            except asyncio.QueueFull:
                pass
        # Терминал В ШИНУ отдельным сообщением: подписчик на чужой реплике
        # ждёт события, а не признака в кэше, — иначе он висел бы до таймаута
        # после уже закрытого прогона.
        _огласить(self.run_id, self.seq + 1, {"type": "__closed__"})
        # Слушатель команд больше не нужен: командовать закрытым прогоном
        # нечем. Оставить его — значит держать подписку на канал до конца
        # процесса, по одной на каждый прогон.
        if self.control is not None and not self.control.done():
            self.control.cancel()


# ── Общее состояние прогона (видно всем репликам) ────────────────────────
# Живёт в кэше координации, а не в базе: запись короткая, читается на каждой
# подписке и естественно истекает. Срок с большим запасом — прогон извлечения
# идёт до 15 минут, а подписка на завершённый нужна ещё какое-то время после.
_СОСТОЯНИЕ_TTL = 6 * 3600.0


def _ключ_состояния(run_id: str) -> str:
    return f"run:{run_id}"


def канал(run_id: str) -> str:
    """Канал шины для прогона. Собирается ОДНОЙ функцией."""
    return f"run:{run_id}"


def _записать_состояние(run_id: str, **поля) -> None:
    """Обновить общую запись прогона. Отказ кэша не роняет прогон."""
    from finengine.store.cache import get_cache  # noqa: PLC0415

    try:
        кэш = get_cache()
        запись = кэш.get(_ключ_состояния(run_id)) or {}
        запись.update(поля)
        кэш.set(_ключ_состояния(run_id), запись, _СОСТОЯНИЕ_TTL)
    except Exception:  # noqa: BLE001 — координация не должна ронять прогон
        log.warning("состояние прогона %s не записано", run_id[:8], exc_info=True)


def состояние_прогона(run_id: str) -> dict | None:
    """Общая запись прогона: `{kind, finished, error, seq}`. `None` — не знаем."""
    from finengine.store.cache import get_cache  # noqa: PLC0415

    try:
        return get_cache().get(_ключ_состояния(run_id))
    except Exception:  # noqa: BLE001
        log.warning("состояние прогона %s не прочитано", run_id[:8], exc_info=True)
        return None


def владелец_прогона(run_id: str) -> int | None:
    """Чей это прогон. `None` — прогона нет или владелец не записан.

    🔴 СПРАШИВАТЬ НАДО И ЧУЖОЙ ПРОГОН. Раньше проверка владельца во всех пяти
    роутах стояла так: «если прогон есть В ПАМЯТИ — сверить владельца», а если
    нет — пропустить. Пока прогон не в памяти означало «давно закончился и
    остался только журнал», это было мягко; с несколькими репликами это
    означает ещё и «идёт у соседа», то есть проверка отключалась бы на живом
    чужом ходе. Владелец лежит в общем состоянии, и спрашивается он оттуда.
    """
    state = _runs.get(run_id)
    if state is not None:
        значение = state.meta.get("user_id")
        return int(значение) if значение is not None else None
    запись = состояние_прогона(run_id) or {}
    значение = запись.get("owner_id")
    return int(значение) if значение is not None else None


def чужой_прогон(run_id: str, user_id: int, *, is_admin: bool = False) -> bool:
    """Нельзя ли этому пользователю смотреть прогон. Одно правило на все роуты."""
    if is_admin:
        return False
    владелец = владелец_прогона(run_id)
    return владелец is not None and владелец != user_id


def _огласить(run_id: str, seq: int, event: dict) -> None:
    """Отправить событие в шину. Порядок держит номер, а не очередь задач.

    Публикация асинхронна, а `emit` — нет, поэтому сообщение ставится в
    последовательный хвост публикаций. Нельзя заводить независимую сетевую
    задачу на каждый узел большого PlanGraph: форма «все показатели» рождает
    сотни событий и исчерпывает Redis connection pool ещё до терминала хода.
    Номер события всё равно остаётся частью протокола — журнал и переподписка
    по-прежнему могут пересекаться.
    """
    try:
        петля = asyncio.get_running_loop()
    except RuntimeError:
        return  # вне цикла событий (тесты, синхронный вызов) — оглашать некуда
    _поставить_публикацию(
        петля, lane="events", channel=канал(run_id),
        message={"seq": seq, "event": event},
    )


# Два последовательных хвоста на event loop: обычные события не создают
# шторм соединений, а служебная команда cancel/emit не ждёт, пока большой граф
# выгрузит весь UI-прогресс. Записи удаляются после опустошения хвоста, поэтому
# завершённые циклы/прогоны здесь не удерживаются.
_ХВОСТЫ_ПУБЛИКАЦИЙ: dict[tuple[asyncio.AbstractEventLoop, str], asyncio.Task] = {}


def _поставить_публикацию(
    петля: asyncio.AbstractEventLoop, *, lane: str, channel: str, message: dict,
) -> None:
    from finengine.store.bus import get_bus  # noqa: PLC0415

    ключ = (петля, lane)
    предыдущая = _ХВОСТЫ_ПУБЛИКАЦИЙ.get(ключ)

    async def _после_предыдущей() -> None:
        if предыдущая is not None:
            try:
                await предыдущая
            except asyncio.CancelledError:
                pass
        try:
            await get_bus().publish(channel, message)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — шина не должна ронять сам прогон
            log.error("событие прогона не опубликовано в канал %s", channel, exc_info=True)

    задача = петля.create_task(_после_предыдущей())
    _ХВОСТЫ_ПУБЛИКАЦИЙ[ключ] = задача

    def _завершить(done: asyncio.Task) -> None:
        # Забираем исключение даже при остановке event loop: иначе asyncio
        # печатает `Task exception was never retrieved`, скрывая исходный сбой.
        try:
            done.exception()
        except asyncio.CancelledError:
            pass
        if _ХВОСТЫ_ПУБЛИКАЦИЙ.get(ключ) is done:
            _ХВОСТЫ_ПУБЛИКАЦИЙ.pop(ключ, None)

    задача.add_done_callback(_завершить)


# ── Registry ─────────────────────────────────────────────────────────────
# ⚠️ ТОЛЬКО ПРОГОНЫ ЭТОЙ РЕПЛИКИ. Здесь живут задача и локальные наблюдатели;
# чужой прогон сюда не попадает и попасть не может — `asyncio.Task` между
# процессами не переносится. Всё, что нужно знать о чужом прогоне, лежит в
# общем состоянии и в журнале.
_runs: dict[str, RunState] = {}


def get_run(run_id: str) -> RunState | None:
    return _runs.get(run_id)


def list_active(kind: str | None = None) -> list[str]:
    return [
        rid
        for rid, s in _runs.items()
        if not s.finished and (kind is None or s.kind == kind)
    ]


def emit_to_run(run_id: str, event: dict) -> bool:
    """#98 C3-2: эмит события в живой ран ИЗВНЕ его раннера.

    Прогресс-мост: тулы, исполняемые через /internal/tools/call (петля у
    гейтвея), шлют прогресс в chat-ран хода — фронт видит те же события,
    что и при инлайн-петле. Возвращает False, если ран не найден/завершён.
    """
    state = _runs.get(run_id)
    if state is not None:
        if state.finished:
            return False
        state.emit(event)
        return True

    # 🔴 ПРОГОН ЧУЖОЙ — ПРОСИМ ВЛАДЕЛЬЦА, А НЕ ПИШЕМ САМИ. Журнал у прогона
    # один писатель, и это его владелец: две реплики, дописывающие в один
    # журнал, разъедут номера событий, а по ним подписчик отбрасывает повторы.
    # Тул, исполняемый гейтвей-петлёй, вполне может попасть на другую реплику,
    # чем ход, — и прогресс обязан доехать всё равно.
    запись = состояние_прогона(run_id)
    if запись is None or запись.get("finished"):
        return False
    _команда(run_id, {"cmd": "emit", "event": event})
    return True


# ── Канал управления прогоном ────────────────────────────────────────────
# Отдельный канал, а не тот же, что у событий: подписчики событий не должны
# видеть служебные команды, а владелец не должен разбирать поток собственных
# событий ради двух команд.


def канал_управления(run_id: str) -> str:
    return f"run:{run_id}:control"


def _команда(run_id: str, сообщение: dict) -> None:
    """Отправить команду владельцу прогона. Он её исполнит у себя."""
    try:
        петля = asyncio.get_running_loop()
    except RuntimeError:
        return
    _поставить_публикацию(
        петля, lane="control", channel=канал_управления(run_id), message=сообщение,
    )


async def _слушать_команды(state: "RunState") -> None:
    """Владелец слушает свой канал управления, пока прогон жив.

    Две команды, и обе нужны только потому, что запрос может прийти на другую
    реплику: `cancel` — кнопка «Стоп» (задачу может отменить лишь тот, у кого
    она в руках), `emit` — прогресс тула из гейтвей-петли.
    """
    from finengine.store.bus import get_bus  # noqa: PLC0415

    подписка = get_bus().subscribe(канал_управления(state.run_id))
    try:
        while not state.finished:
            сообщение = await подписка.get(timeout=1.0)
            if сообщение is None:
                continue
            приказ = сообщение.get("cmd")
            if приказ == "cancel":
                задача = state.task
                if задача is not None and not задача.done():
                    задача.cancel()
                return
            if приказ == "emit" and not state.finished:
                событие = сообщение.get("event")
                if isinstance(событие, dict):
                    state.emit(событие)
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 — слушатель не должен ронять прогон
        log.warning("слушатель команд прогона %s упал", state.run_id[:8], exc_info=True)
    finally:
        подписка.close()


# ── Main entry point ─────────────────────────────────────────────────────
def start_run(
    runner: Callable[[], AsyncGenerator[dict[str, Any], None]],
    *,
    kind: str,
    meta: dict,
    on_done: Callable[[RunState], Awaitable[None]] | None = None,
) -> str:
    """Spawn a run as a background task and return its `run_id`.

    `runner` is a zero-arg callable that returns an async generator of
    events. Construction is deferred so the caller can capture state in
    a closure without starting iteration eagerly.

    `kind` selects the journal directory. Use one of `KNOWN_KINDS` (or
    add to that tuple if introducing a new run type).

    `meta` is opaque metadata stored alongside the run for the audit log
    (doc_id, arch, user_id, …). NOT exposed to subscribers.

    `on_done` is awaited once after the runner finishes (success or
    error). Use it to write the aggregate row to the appropriate
    *_runs.jsonl audit file.

    Caller is responsible for kicking off the SSE stream that subscribes
    to this run — `start_run` returns immediately; the task runs
    concurrently. Client disconnect on the kicker stream cancels the
    request task, not this one.
    """
    if kind not in KNOWN_KINDS:
        raise ValueError(f"unknown run kind: {kind!r}; expected one of {KNOWN_KINDS}")
    run_id = str(uuid.uuid4())
    state = RunState(run_id, kind, meta)
    _runs[run_id] = state
    # Общая запись — ДО первого события: подписка на чужой реплике может
    # прийти раньше, чем раннер успеет что-нибудь сказать, и должна понять,
    # что ждать есть чего.
    _записать_состояние(run_id, kind=kind, finished=False, error=None, seq=0,
                        started_at=state.started_at,
                        # Владелец — в общей записи: по нему проверяют доступ
                        # реплики, у которых прогона в памяти нет.
                        owner_id=meta.get("user_id"))

    async def _drive() -> None:
        try:
            async for ev in runner():
                state.emit(ev)
        except asyncio.CancelledError:
            state.emit({"type": "error", "message": "run cancelled"})
            state.close(error="cancelled")
            raise
        except Exception as e:
            state.emit({"type": "error", "message": f"{type(e).__name__}: {e}"})
            state.close(error=f"{type(e).__name__}: {e}")
        else:
            state.close()
        finally:
            if on_done is not None:
                try:
                    await on_done(state)
                except Exception:
                    pass

    state.task = asyncio.create_task(_drive(), name=f"{kind}-run-{run_id[:8]}")
    state.control = asyncio.create_task(
        _слушать_команды(state), name=f"{kind}-ctl-{run_id[:8]}")
    return run_id


def cancel_run(run_id: str) -> bool:
    """#84 C3-1: прервать живой прогон (кнопка «Стоп» чата). _drive ловит
    CancelledError → эмитит {"type":"error","message":"run cancelled"} и
    закрывает журнал, так что подписчики получают терминал. True = отменяли."""
    state = _runs.get(run_id)
    if state is not None:
        task = state.task
        if state.finished or task is None or task.done():
            return False
        task.cancel()
        return True

    # Прогон чужой: отменить чужую задачу отсюда нельзя — просим владельца.
    # Ответ «отменяли» здесь означает «команда отправлена»: подтверждения от
    # владельца мы не ждём, иначе кнопка «Стоп» висела бы на круговом запросе.
    запись = состояние_прогона(run_id)
    if запись is None or запись.get("finished"):
        return False
    _команда(run_id, {"cmd": "cancel"})
    return True


# ── Heartbeat живого хвоста ──────────────────────────────────────────────
# ПОЧЕМУ ОН ЕСТЬ. Штатная тишина внутри ОДНОГО тула доходит до 3-6 минут
# (замер 02.08 на Мечеле: сбор показателей 193 с, написание анализа 176 с —
# 80% хода), а хвост подписки — безусловное `await q.get()`: пока раннер
# молчит, в сокет не уходит ни байта. Живой обрыв 05.08 (ход 2ae08a6a) шёл
# именно так: сервер довёл ход до `done`, а у клиента поток уже умер.
#
# ОТКУДА ПЕРИОД. Молчащее соединение рвут, по возрастанию терпения:
#   ~5 мин  — сторож простоя во фронте (web/src/components/workspace/
#             use-chat-run.ts, IDLE_MS = 5*60*1000; перезаводится только на
#             РАСПАРСЕННОМ событии, по срабатыванию делает ac.abort());
#   от 60 с — промежуточные NAT/корпоративные прокси у пользователей;
#   30 мин  — наш Caddy (deploy/Caddyfile: read_timeout/write_timeout 30m,
#             flush_interval -1 — он-то как раз паузу переживает).
# Первым рвёт фронтовый сторож, но период надо брать меньше самого
# НЕТЕРПЕЛИВОГО звена, а не самого раннего: 15 с — это 4 кадра внутрь
# минутного окна NAT и 20-кратный запас до сторожа простоя.
HEARTBEAT_SEC: float = 15.0


# ── Subscribe (initial stream + reattach after reload) ───────────────────
async def subscribe(
    run_id: str, *, kind: str | None = None, heartbeat_sec: float | None = None
) -> AsyncGenerator[dict, None]:
    """Yield every event for `run_id` — past + live.

    Resolution:
    - Live in-memory: replay everything emitted so far, then attach a
      queue and tail until the run closes.
    - Not in memory but journal exists on disk: replay the file and
      exit. The frontend treats the last event accordingly (e.g.
      `session_lost` → finalise with a notice).
    - Neither: yield a single `error` event and exit.

    `kind` lets callers narrow the journal lookup when the registry is
    cold (after a restart). If omitted we probe every known kind.

    `heartbeat_sec` — период кадра-биения в живом хвосте (см. HEARTBEAT_SEC).
    ВЫКЛЮЧЕН ПО УМОЛЧАНИЮ и включается вызывающим: `subscribe` кормит шесть
    SSE-ручек, а `ping` — это настоящее событие в потоке, и обработчики
    остальных пяти потоков (extract, ir_calendar, scenario) на незнакомый
    тип не проверены. Включаем там, где потребитель согласован.
    """
    state = _runs.get(run_id)

    if state is not None:
        for ev in list(state.events):
            yield ev
        if state.finished:
            return
        q: asyncio.Queue[dict | None] = asyncio.Queue()
        state.observers.append(q)
        try:
            while True:
                if heartbeat_sec is None:
                    ev = await q.get()
                else:
                    try:
                        ev = await asyncio.wait_for(q.get(), timeout=heartbeat_sec)
                    except asyncio.TimeoutError:
                        # Прогон мог закрыться ровно в это окно. Пинг после
                        # `done` — течь: подписка обязана закончиться, а не
                        # жить вечно на биении. Но выйти сразу нельзя: в
                        # очереди уже лежат последние события и сентинел
                        # (close раздаёт его всем наблюдателям) — досылаем их,
                        # иначе биение съедало бы хвост ответа.
                        if state.finished:
                            while True:
                                try:
                                    ev = q.get_nowait()
                                except asyncio.QueueEmpty:
                                    return
                                if ev is None:
                                    return
                                yield ev
                        # Пинг рождается ЗДЕСЬ, у подписчика, а не в
                        # RunState.emit: emit пишет каждое событие в журнал
                        # (append + fsync) и раздаёт всем наблюдателям —
                        # биение навсегда осело бы в аудите и в реплее.
                        #
                        # Почему СОБЫТИЕ, а не SSE-комментарий `: ping`:
                        # комментарий держит только сокет. Клиентский парсер
                        # (web/src/lib/api.ts) выбрасывает кадры без строки
                        # `data: `, до потребителя они не доходят — а значит
                        # НЕ переводят фронтовый сторож простоя, который и
                        # рвёт первым. Тип `ping` не пересекается ни с одним
                        # типом, который эмитят раннеры.
                        yield {"type": "ping"}
                        continue
                if ev is None:
                    return
                yield ev
        finally:
            try:
                state.observers.remove(q)
            except ValueError:
                pass
        return

    # Прогона нет в памяти ЭТОЙ реплики. Два разных случая, и путать их нельзя.
    запись = состояние_прогона(run_id)

    if запись is not None and not запись.get("finished"):
        # 🔴 ЖИВОЙ ПРОГОН ЧУЖОЙ РЕПЛИКИ. Порядок здесь несущий:
        #   1) открыть хвост,
        #   2) прочитать журнал (догон),
        #   3) слушать хвост, отбрасывая по номеру то, что уже отдали.
        # Обратный порядок теряет события, случившиеся между чтением журнала и
        # подпиской, — «пропало ровно одно событие в середине».
        from finengine.store.bus import get_bus  # noqa: PLC0415

        подписка = get_bus().subscribe(канал(run_id))
        try:
            отдано = 0
            for ev in _из_журнала(run_id, kind):
                отдано += 1
                yield ev
            пауза = heartbeat_sec if heartbeat_sec is not None else 1.0
            while True:
                сообщение = await подписка.get(timeout=пауза)
                if сообщение is None:
                    # Тишина. Прогон мог закончиться, пока мы ждали, — и
                    # терминальное сообщение могло не дойти: шина доставку не
                    # гарантирует. Спрашиваем общее состояние, иначе подписка
                    # висела бы вечно на закрытом прогоне.
                    свежая = состояние_прогона(run_id)
                    if свежая is None or свежая.get("finished"):
                        # 🔴 ХВОСТ ДОБИРАЕМ ЖУРНАЛОМ, А НЕ ВЫХОДИМ СРАЗУ.
                        #
                        # Поймано живой проверкой между процессами 26.08:
                        # подписчик получал «первое» и «второе» и НЕ получал
                        # `done`. `close()` пишет «закончено» СИНХРОННО, а
                        # публикация последних событий в этот момент ещё висит
                        # задачей — подписчик, увидев признак, уходил и уносил
                        # с собой конец ответа.
                        #
                        # Журнал знает всё: досылаем из него то, что ещё не
                        # отдали. Это же закрывает и потерю сообщения шиной —
                        # доставку она не гарантирует, а журнал не теряет.
                        for i, ev in enumerate(_из_журнала(run_id, kind), start=1):
                            if i > отдано:
                                yield ev
                        return
                    if heartbeat_sec is not None:
                        yield {"type": "ping"}
                    continue
                номер = int(сообщение.get("seq") or 0)
                событие = сообщение.get("event") or {}
                if номер <= отдано:
                    continue  # уже отдали из журнала
                if событие.get("type") == "__closed__":
                    # Терминал шины пришёл — но события перед ним могли
                    # потеряться по дороге. Добираем журналом, как и выше.
                    for i, ev in enumerate(_из_журнала(run_id, kind), start=1):
                        if i > отдано:
                            yield ev
                    return
                отдано = номер
                yield событие
        finally:
            подписка.close()
        return

    # Прогон закончен (или неизвестен) — только журнал.
    было = False
    for ev in _из_журнала(run_id, kind):
        было = True
        yield ev
    if было:
        return

    yield {
        "type": "error",
        "message": (
            "Запуск не найден. Возможно, он завершился давно и журнал "
            "был удалён, либо идентификатор некорректен."
        ),
    }


async def subscribe_versioned(
    run_id: str,
    *,
    kind: str | None = None,
    after_sequence: int = 0,
    heartbeat_sec: float | None = None,
):
    """Expose the existing durable journal through the A11 event contract.

    ``sequence`` is the stable one-based journal position, so a reconnect can
    pass its last received cursor and never receive an event twice. Heartbeats
    are not journal entries and repeat the last durable cursor; clients must
    use them only as liveness signals. Legacy entries remain opaque and are
    never parsed as a substitute for typed results.
    """
    from finengine.contracts.transport import (  # noqa: PLC0415
        CompletedEvent,
        HeartbeatEvent,
        envelope_legacy_event,
    )

    durable_sequence = 0
    completion_status = "succeeded"
    last_type: str | None = None
    async for event in subscribe(run_id, kind=kind, heartbeat_sec=heartbeat_sec):
        if event.get("type") == "ping":
            yield HeartbeatEvent(run_id=run_id, sequence=durable_sequence)
            continue
        durable_sequence += 1
        event_type = event.get("type")
        last_type = event_type
        if event_type == "typed_result" and event.get("status") == "partial":
            completion_status = "partial"
        elif event_type == "typed_error":
            completion_status = "partial" if event.get("partial_results") else "failed"
        elif event_type == "error":
            message = str(event.get("message") or event.get("error") or "")
            completion_status = "cancelled" if "cancel" in message.lower() else "failed"
        if durable_sequence <= after_sequence:
            continue
        if event_type == "done":
            yield CompletedEvent(
                run_id=run_id,
                sequence=durable_sequence,
                status=completion_status,
            )
            continue
        yield envelope_legacy_event(event, run_id=run_id, sequence=durable_sequence)

    # Упавший и отменённый («Стоп») прогон кончается в журнале на `error` без `done`
    # (`_drive`: `error`, затем `close`). Конец ему — `completed` со статусом из того же
    # `error`, как для `done`. Но только когда прогон ЗАКРЫТ: `error` бывает и посреди хода, а
    # чужая реплика при отказе кэша закрывает поток у живого прогона.
    if (
        last_type in {"error", "typed_error"}
        and durable_sequence + 1 > after_sequence
        and _прогон_закрыт(run_id)
    ):
        yield CompletedEvent(run_id=run_id, sequence=durable_sequence + 1, status=completion_status)


def _прогон_закрыт(run_id: str) -> bool:
    """Прогон закрыт — по реестру этой реплики или по общему состоянию. Не прочитано — не закрыт."""
    state = _runs.get(run_id)
    if state is not None:
        return state.finished
    запись = состояние_прогона(run_id)
    return bool(запись and запись.get("finished"))


def _из_журнала(run_id: str, kind: str | None):
    """Все события прогона из журнала, по порядку. Пусто — журнала нет.

    Журнал живёт в ОБЩЕМ хранилище (`store/journals`) — значит догон работает и
    с чужой машины, а не только между воркерами одного контейнера. Прогоны
    эпохи файлового журнала читаются откатом на диск: спросить хранилище, потом
    файл. Без отката они перестали бы открываться в тот же миг, когда включили
    новое хранилище.
    """
    from finengine.store import journals  # noqa: PLC0415

    candidates: Iterable[str] = (kind,) if kind else KNOWN_KINDS
    for k in candidates:
        if k is None:
            continue
        события = journals.read(вид_журнала(k), owner=_ВЛАДЕЛЕЦ, key=run_id)
        if события:
            yield from события
            return
        # Прогон из эпохи файлового журнала. Откат тот же, что у текста
        # документа: спросить хранилище, потом диск — иначе старые прогоны
        # перестали бы открываться в тот же миг, когда переключили хранилище.
        path = event_file(run_id, k)
        if not path.exists():
            continue
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue
        return


# ── House-keeping ─────────────────────────────────────────────────────────
def cleanup_finished(max_age_seconds: float = 24 * 3600) -> int:
    """Drop finished runs older than `max_age_seconds` from the in-memory
    registry. Journal files on disk stay — they're append-only audit
    and cheap. Returns count of evicted runs."""
    now = time.time()
    evicted = 0
    for rid in list(_runs.keys()):
        s = _runs[rid]
        if s.finished and s.finished_at and (now - s.finished_at) > max_age_seconds:
            del _runs[rid]
            evicted += 1
    return evicted


# ── Orphan-run recovery on backend startup ───────────────────────────────
def recover_orphan_runs() -> dict[str, int]:
    """Scan every kind's event directory and finalise journals that died
    mid-stream — append synthetic `session_lost` + `done`. Idempotent:
    journals that CONTAIN `done` / `session_lost` are skipped (not just those
    ending with one: `artifact` is written after `done`).

    Returns a per-kind dict of newly-marked counts.
    """
    result: dict[str, int] = {}
    for kind in KNOWN_KINDS:
        marked = 0
        for run_id in _незакрытые_прогоны(kind):
            _пометить_оборванным(run_id, kind)
            marked += 1
        if marked:
            result[kind] = marked
    return result


def _незакрытые_прогоны(kind: str) -> list[str]:
    """Прогоны этого вида, у которых в журнале нет терминала.

    🔴 ЖИВОЙ ПРОГОН СОСЕДА — НЕ СИРОТА (правило 3.2). Воркеров несколько, и
    упавший перезапускается по одному: его старт пробегает ЧУЖИЕ журналы. Без
    проверки общего состояния он дописал бы «прогон оборвался при перезапуске»
    идущему прогону соседа — пользователь увидел бы отказ посреди работы.

    🔴 «СОДЕРЖИТ», А НЕ «ЗАКАНЧИВАЕТСЯ» (25.08.2026). Здесь проверялось
    ПОСЛЕДНЕЕ событие. Но `artifact` (api/main.py) пишется ПОСЛЕ `done`, и
    завершённый прогон выглядел оборванным: рестарт дописывал ему отказ, и
    владелец, переоткрыв успешный прогон, читал приговор. На проде так были
    помечены 15 журналов из 69 — 22% успешных.
    """
    from finengine.store import journals  # noqa: PLC0415

    вид = вид_журнала(kind)
    известные = set(journals.keys(вид, owner=_ВЛАДЕЛЕЦ))
    # Старые прогоны на томе: их надо закрыть тоже, иначе подписка на них
    # будет крутиться вечно. Каталог `chain_events` — легаси того же вида.
    каталоги = [_events_dir(kind)]
    if kind == "chain":
        каталоги.append(settings.docs_dir / "chain_events")
    for d in каталоги:
        if d.exists():
            известные.update(p.stem for p in d.glob("*.jsonl"))

    итог: list[str] = []
    for run_id in sorted(известные):
        запись = состояние_прогона(run_id)
        if запись is not None and not запись.get("finished"):
            continue
        события = list(_из_журнала(run_id, kind))
        if any(e.get("type") in {"done", "session_lost"} for e in события):
            continue
        итог.append(run_id)
    return итог


def _пометить_оборванным(run_id: str, kind: str) -> None:
    """Дописать синтетический терминал, чтобы подписка не крутилась вечно."""
    append_event(run_id, kind, {
        "type": "session_lost",
        "message": ("Прогон оборвался при перезапуске сервера. "
                    "Запустите его заново — старые шаги остались в журнале."),
    })
    append_event(run_id, kind, {"type": "done"})


__all__ = [
    "KNOWN_KINDS",
    "RunState",
    "append_event",
    "cleanup_finished",
    "event_file",
    "get_run",
    "list_active",
    "recover_orphan_runs",
    "владелец_прогона",
    "чужой_прогон",
    "start_run",
    "subscribe",
    "subscribe_versioned",
]
