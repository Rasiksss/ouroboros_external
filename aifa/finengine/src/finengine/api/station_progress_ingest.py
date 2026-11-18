"""Backend ingest for station progress (#83, Фаза 3).

The sealed station writes progress events to a per-nonce NDJSON file; the
station-bridge daemon relays each line here over HTTP. This module keeps an
in-process registry ``nonce -> asyncio.Queue`` and pushes ingested events onto
the matching queue. A tool handler that drives a station run registers a nonce
before exec, drains its queue into ``ctx.progress`` (→ SSE) while the station
works, and unregisters after. Correlation model: ``PHASE2_PROGRESS_DESIGN.md``.

Isolated for now: the router is additive (own ``/internal/station-progress``
route, own token) and touches no existing endpoint. Wiring the ``_h_fe_*``
handlers to it is the separate, benchmark-gated switchover step.
"""
from __future__ import annotations

import asyncio
import logging
import time

from fastapi import APIRouter, Header
from pydantic import BaseModel

from finengine.skills import grounding_bus, run_logger  # noqa: E402
from finengine.skills.station_run_sink import error_tail  # noqa: E402
from finengine.store import runs  # noqa: E402

log = logging.getLogger(__name__)

router = APIRouter()

# nonce -> ПОДПИСЧИКИ ЭТОЙ РЕПЛИКИ (fan-out, #84 фронт-подводка): у одного нонса
# несколько потребителей — mailbox (сводка в историю) И run-релей (живой
# прогресс на фронт). Каждый register даёт СВОЮ очередь.
#
# 🔴 РЕПЛИКА ДЕМОНА И РЕПЛИКА ПОТРЕБИТЕЛЯ — РАЗНЫЕ. Демон станции шлёт пачку
# по HTTP, и она попадает на любой воркер; потребитель зарегистрирован на том,
# где исполняется тул. Раньше это совпадало по одной причине — воркер был
# один. С несколькими прогресс уходил бы в «no_consumer», а `done` не доезжал
# бы до почтового ящика: фоновое извлечение молча теряло бы сводку.
#
# Поэтому каждое `register` подписывается ещё и на канал шины, а `ingest`
# ПУБЛИКУЕТ. Локальная доставка при этом не убрана: она короче на сетевой
# круг, а на одной реплике (шина внутри процесса) публикация её продублирует
# — от этого спасает отметка о доставке, см. `_разнести`.
_bus: dict[str, list[asyncio.Queue]] = {}
_подписки: dict[int, object] = {}
_MAXQ = 2000


def канал(nonce: str) -> str:
    """Канал шины для нонса. Собирается ОДНОЙ функцией."""
    return f"station:{nonce}"


async def _качать(nonce: str, q: asyncio.Queue, подписка) -> None:
    """Перекладывать события с шины в очередь ЭТОГО потребителя.

    🔴 ПОДПИСКУ ОТКРЫВАЕТ НЕ ЭТА ЗАДАЧА, А `register` — СИНХРОННО. Первая
    редакция открывала её здесь, и получалась гонка: `unregister`, случившийся
    до первого шага задачи, не находил подписки и не закрывал её. Канал
    оставался жив, а приём докладывал «доставлено соседу» о событии, читать
    которое уже некому, — сторож потери 18.08 переставал ловить потерю, ничего
    при этом не краснея.

    Событие, пришедшее с той же реплики, приходит сюда повторно — его отсекает
    пометка `_local`, которую ставит локальная доставка.
    """
    try:
        while id(q) in _подписки:
            сообщение = await подписка.get(timeout=1.0)
            if сообщение is None:
                continue
            if сообщение.get("_local"):
                continue  # уже положили напрямую
            try:
                q.put_nowait(сообщение.get("event") or {})
            except asyncio.QueueFull:
                log.warning("прогресс станции: очередь подписчика нонса %s "
                            "переполнена", nonce)
    except asyncio.CancelledError:
        raise
    finally:
        подписка.close()
        _подписки.pop(id(q), None)


def _token() -> str:
    # Единственная реализация правила — `api/internal_auth`: тот же секрет у
    # бэкенда, демона станции и плагина гейтвея. Здесь остаётся обёртка для
    # тестов и соседей.
    from finengine.api.internal_auth import gateway_token  # noqa: PLC0415

    return gateway_token()


def register(nonce: str) -> asyncio.Queue:
    """Зовётся потребителем ДО старта прогона станции под этим нонсом.

    Возвращает СВОЮ очередь потребителя (fan-out: каждый register — новая) и
    заводит подписку на канал шины: пачка от демона может прийти на другую
    реплику, и без подписки прогресс до этого потребителя не доедет.
    """
    q: asyncio.Queue = asyncio.Queue(maxsize=_MAXQ)
    _bus.setdefault(nonce, []).append(q)
    from finengine.store.bus import get_bus  # noqa: PLC0415

    # Подписка открывается ЗДЕСЬ и синхронно — см. `_качать`.
    подписка = get_bus().subscribe(канал(nonce))
    _подписки[id(q)] = подписка
    try:
        asyncio.get_running_loop().create_task(_качать(nonce, q, подписка))
    except RuntimeError:
        # Вне цикла событий (тесты, синхронный вызов) качать некому — закрываем
        # подписку сразу, чтобы она не висела мёртвой в шине.
        подписка.close()
        _подписки.pop(id(q), None)
        log.debug("прогресс станции: подписка на шину не заведена (нет цикла)")
    return q


def _закрыть_подписку(q: asyncio.Queue) -> None:
    """Отцепить очередь от шины НЕМЕДЛЕННО.

    🔴 НЕ «ПОМЕТИТЬ, ЧТОБЫ КАЧАЛКА ЗАМЕТИЛА». Первая редакция только убирала
    запись, а сама подписка жила до следующего круга — до секунды. В это окно
    приём отвечал «доставлено соседу» о событии, которое читать уже некому:
    сторож потери 18.08 переставал ловить потерю, ничего при этом не краснея.
    """
    подписка = _подписки.pop(id(q), None)
    if подписка is not None:
        подписка.close()  # type: ignore[attr-defined]


def unregister(nonce: str, q: asyncio.Queue | None = None) -> None:
    """Снять СВОЮ очередь (q) — или все очереди нонса (q=None, legacy)."""
    if q is None:
        for очередь in list(_bus.get(nonce) or ()):
            _закрыть_подписку(очередь)
        _bus.pop(nonce, None)
        return
    _закрыть_подписку(q)
    lst = _bus.get(nonce)
    if lst is not None:
        try:
            lst.remove(q)
        except ValueError:
            pass
        if not lst:
            _bus.pop(nonce, None)


def get_queue(nonce: str) -> asyncio.Queue | None:
    """Первая очередь нонса (совместимость; новый код держит свою из register)."""
    lst = _bus.get(nonce)
    return lst[0] if lst else None


class ProgressBatch(BaseModel):
    nonce: str
    events: list[dict]


def _remember_terminal(nonce: str, events: list[dict]) -> None:
    """Исход прогона — В ХРАНИЛИЩЕ, до и независимо от подписчика (#276).

    ЗАЧЕМ ИМЕННО ЗДЕСЬ. Это единственное место, где событие уже в процессе
    бэкенда, но ещё не зависит от того, слушает ли его кто-нибудь. Ниже, где
    очередей нет, пачка просто отбрасывается — и 18.08 этой веткой ушли ЦЕЛИКОМ
    два прогона: ДОМ.РФ (нонс 060f41c1, 10:00:44) и ГК «Самолёт» (4ce68a11,
    16:24:14, 13 событий разом). Врезаться ниже — в сток или в адресата —
    нельзя конструктивно: сток читает очередь, которой в этом случае нет вовсе.

    ПИШЕМ ВСЕГДА, А НЕ ТОЛЬКО КОГДА ПОДПИСЧИКА НЕТ. Наличие очереди — не
    доказательство доставки: переполненная очередь роняет событие молча (ниже
    по коду), а слушатель мог умереть между регистрацией и терминалом. Условие
    «подписчика нет» сузило бы запись ровно до того случая, который мы уже
    умеем видеть, и оставило бы невидимыми остальные.

    ⚠️ ПОЛНОТА ИСХОДА ЗДЕСЬ ЧАСТИЧНАЯ, И ЭТО НАЗВАНО, А НЕ СПРЯТАНО. Строк
    прогона в этой точке нет: они приехали событиями `metric` раньше и осели
    только в `data/station_events.jsonl`. Здесь сохраняется то, что несёт сам
    терминал, — сводка станции со счётчиками. Копить ли строки второй
    append-only таблицей — отдельное решение, принимать его молча нельзя.
    """
    for ev in events:
        # Только `type`: у события `plan_step` бывает `status == "error"` —
        # это красный ШАГ, а не отказ прогона, и хоронить по нему весь исход
        # значило бы записать успешный прогон отказом.
        kind = str(ev.get("type") or "")
        if kind not in ("done", "error"):
            continue
        result = ev.get("result")
        stored = runs.finish_best_effort(
            nonce,
            status=runs.STATUS_DONE if kind == "done" else runs.STATUS_FAILED,
            payload=result if isinstance(result, dict) else None,
            error=error_tail(str(ev.get("message") or "")) if kind == "error" else None,
            doc_id=grounding_bus.worker_doc(nonce) or "",
            finished_at=int(time.time()))
        if not stored and runs.is_enabled():
            # Либо строки прогона нет (нонс не взводился: путь воркера, чужая
            # комната), либо хранилище отказало — тогда причина уже записана
            # стеком строкой выше. Обе ветки называем одной фразой и не
            # угадываем, какая случилась: угаданная причина хуже названного
            # факта. Молчать нельзя — это ровно та потеря, ради которой
            # хранилище и заводилось.
            log.warning("исход прогона не сохранён: нонс=%s, статус=%s — строки "
                        "прогона нет (взвода не было?) либо хранилище отказало",
                        nonce, kind)


async def _разнести(nonce: str, события: list[dict]) -> int:
    """Опубликовать пачку в шину. Возвращает число получателей.

    Число нужно, чтобы отличить «подписчика нет ЗДЕСЬ» (норма при нескольких
    репликах) от «событие не досталось НИКОМУ» (отказ, о котором надо
    кричать). Раньше эти два случая совпадали по единственной причине —
    воркер был один.

    Пометка `_local` нужна на ОДНОЙ реплике: там шина внутри процесса, и
    подписчик получил бы то же событие вторым экземпляром — счётчик на экране
    считал бы вдвое.
    """
    from finengine.store.bus import get_bus  # noqa: PLC0415

    шина = get_bus()
    свои = bool(_bus.get(nonce))
    получателей = 0
    for ev in события:
        try:
            получателей = max(получателей, await шина.publish(
                канал(nonce), {"event": ev, "_local": свои}))
        except Exception:  # noqa: BLE001 — приём важнее рассылки
            log.warning("прогресс станции: публикация в шину не удалась (нонс=%s)",
                        nonce, exc_info=True)
            return получателей
    return получателей


@router.post("/internal/station-progress")
async def ingest(batch: ProgressBatch, authorization: str = Header(default="")) -> dict:
    from finengine.api.internal_auth import require_gateway_token  # noqa: PLC0415

    require_gateway_token(authorization)
    # ОШИБКА СТАНЦИИ ОБЯЗАНА ОСТАТЬСЯ В ЛОГЕ СЕРВЕРА (09.08.2026).
    # До сих пор события проносились на экран и нигде не оседали. Живой случай:
    # прогон по Лукойлу, шаг «Прочитать отчёт и собрать показатели (77 шт.)»
    # упал на 11:22 — пользователь видел красный шаг в плане, а в логе бэкенда
    # за полтора часа не было НИ ОДНОЙ строки об этом. Разбирать было нечего:
    # ход при этом закрылся как успешный, станция исключения не бросила.
    #
    # Логируем ДО проверки подписчика: ошибка ценна и тогда, когда доставлять
    # её уже некому — именно в этом случае она иначе исчезает совсем.
    for ev in batch.events:
        if str(ev.get("type") or "") == "error" or str(ev.get("status") or "") == "error":
            log.error("станция сообщила об ошибке (нонс=%s): %s", batch.nonce, ev)

    # СЫРЬЁ ХОДА ОСЕДАЕТ НА БЭКЕНДЕ (#249). Комната станции живёт ~18 минут и
    # уносит с собой всё: канал прогресса, вывод скриптов, шаг, на котором
    # упало. Пока прогон удачен, это неважно — итог кладут `finalize_*`. Но
    # `finalize_*` зовётся ТОЛЬКО на успехе: когда ход падает на середине (шаг
    # «собрать карточку анализа», живой случай 15.08), не остаётся ничего, и
    # разбор возможен лишь если успеть заглянуть в живую комнату. Дважды за
    # день это стоило разбора: комната истекала раньше, чем до неё доходили
    # руки.
    #
    # Пишем ЗДЕСЬ и ДО проверки подписчика — по той же причине, по которой
    # здесь же логируется ошибка: событие ценно именно тогда, когда доставлять
    # его уже некому.
    run_logger.log_station_events(batch.nonce, batch.events)
    _remember_terminal(batch.nonce, batch.events)

    # В ШИНУ — ВСЕГДА И ПЕРВЫМ ДЕЛОМ. Потребитель может сидеть на другой
    # реплике, и «подписчика нет» здесь означает лишь «нет ЗДЕСЬ».
    получателей = await _разнести(batch.nonce, batch.events)

    queues = list(_bus.get(batch.nonce) or [])
    if not queues and получателей:
        # Потребитель на соседней реплике — доставку ведёт он. Это норма, а не
        # потеря, и говорить о ней голосом отказа нельзя.
        log.info("прогресс станции: подписчик на другой реплике (нонс=%s, "
                 "событий=%d)", batch.nonce, len(batch.events))
        return {"accepted": len(batch.events), "dropped": 0,
                "reason": "delivered_elsewhere"}
    if not queues:
        # Подписчика на этот нонс нет: хендлер уже закончил, либо не
        # зарегистрировался вовсе. Прогресс НЕ косметика — это единственный
        # признак жизни прогона на экране: пропал счётчик «40/77», и человек
        # видит зависшую систему, а не работающую. Раньше дроп был молчаливым
        # и в логе не оставлял ничего, поэтому «прогресс не пришёл» нельзя было
        # отличить от «станция не писала». Теперь говорим вслух.
        # Ни здесь, ни на соседях — вот это и есть настоящая потеря.
        # Прогресс НЕ косметика: это единственный признак жизни прогона на
        # экране. Пропал счётчик «40/77» — и человек видит зависшую систему, а
        # не работающую. Раньше дроп был молчаливым, и «прогресс не пришёл»
        # нельзя было отличить от «станция не писала».
        log.warning(
            "прогресс станции некуда доставить: нонс=%s, событий=%d — подписчика "
            "нет НИ НА ОДНОЙ реплике (хендлер завершился раньше станции, либо "
            "register() не звали). На экране это выглядит как замерший счётчик.",
            batch.nonce, len(batch.events),
        )
        return {"accepted": 0, "dropped": len(batch.events), "reason": "no_consumer"}
    accepted = 0
    for ev in batch.events:
        delivered = False
        for q in queues:
            try:
                q.put_nowait(ev)
                delivered = True
            except asyncio.QueueFull:
                continue  # переполненный подписчик пропускает, остальные получают
        if delivered:
            accepted += 1
    return {"accepted": accepted, "dropped": len(batch.events) - accepted}
