"""Персональная методика пользователя: хранение правок и их сборка на прогон (#123).

ЧТО ЗДЕСЬ. База методики живёт в git и одна на всех (`methodology.BASE_ELEMENTS`).
Здесь — ЛИЧНЫЙ слой: набор дельт одного пользователя, лежащий в его каталоге и
никуда за его пределы не уходящий. Хранятся именно дельты, а не копия методики,
поэтому наши исправления базовых формул продолжают до него доезжать — расходится
только то, что он трогал, и ровно об этом ему и сообщают.

ДВА ГОРИЗОНТА ПРАВКИ (различие Nate 31.07):
  «поправь СЕЙЧАС» → `remember_for_turn` — правка живёт до ближайшего анализа и
      им СЪЕДАЕТСЯ. Не таймер, а разовость: сказали «в этот раз посчитай без
      разовой продажи» — это не новая методика;
  «считай так ВСЕГДА» → `save_delta` — попадает на диск и применяется ко всем
      будущим прогонам этого пользователя.

ПРОВЕРКА ДО ЗАПИСИ. Дельта сначала применяется к базе и только потом сохраняется:
сломанная правка не должна лечь на диск и сломать все последующие анализы.

КОГДА ЯКОРЬ ПОТЕРЯН. Мы переписали базу, и правка ссылается на элемент, которого
больше нет. В момент СОЗДАНИЯ правки это ошибка (агент выбирает элемент заново).
В момент ПРОГОНА — не ошибка: анализ идёт по базе, но потеря громко сообщается в
провенансе. Молча посчитать по базе, пока пользователь уверен, что считают по
его методике, — худший из вариантов; уронить ему анализ из-за нашей же правки
базы — тоже плохой.
"""
from __future__ import annotations

import logging
import time

from finengine.skills.analysis import methodology as md
from finengine.store import state

log = logging.getLogger(__name__)

_VERSION = 1
_TURN_TTL_SEC = 900  # страховка: разовая правка не должна жить вечно, если анализ не пришёл
_turn_deltas: dict[int, tuple[float, list[dict]]] = {}


def load(user_id: int) -> list[dict]:
    doc = state.get(state.METHODOLOGY, owner=int(user_id), key="overlay") or {}
    deltas = doc.get("deltas") if isinstance(doc, dict) else None
    return [d for d in (deltas or []) if isinstance(d, dict)]


def _только_дельты(doc: dict | None) -> list[dict]:
    """Дельты из документа. Разбор один на обе стороны `mutate`."""
    deltas = (doc or {}).get("deltas") if isinstance(doc, dict) else None
    return [d for d in (deltas or []) if isinstance(d, dict)]


def save_delta(user_id: int, delta: dict) -> dict:
    """Записать правку «считать так всегда». Возвращает применённую дельту.

    Одна правка на элемент: повторная замена того же id вытесняет прежнюю, а не
    копится стопкой, из которой побеждает последняя по случайности порядка.
    """
    delta = dict(delta)
    eid = str(delta.get("id") or "").strip()
    if not eid:
        raise md.MethodologyError("правка без id — непонятно, что менять")

    base = md.base()
    if delta.get("op") == "replace" and "base_was" not in delta:
        cur = base.elements.get(eid)
        if cur is not None:
            # Запоминаем, ПРОТИВ ЧЕГО правили: иначе потом не показать пользователю,
            # что именно у нас поменялось, и предложить смёржить будет нечего.
            delta["base_was"] = cur.text
    delta["ts"] = int(time.time())

    # Чтение и запись — ОДНОЙ операцией: между ними не должно вклиниться чужое
    # сохранение, иначе оно потеряется молча. На томе это файловый замок, в
    # базе — транзакция; выбирает `store/state.mutate`, а не этот модуль.
    def _применить(doc: dict | None) -> dict:
        existing = [d for d in _только_дельты(doc) if str(d.get("id")) != eid]
        md.apply([*existing, delta])  # ← падает ДО записи, если правка не применяется
        return {"version": _VERSION, "deltas": [*existing, delta]}

    state.mutate(state.METHODOLOGY, owner=int(user_id), key="overlay",
                 изменить=_применить)
    return delta


def forget(user_id: int, element_id: str = "") -> int:
    """Убрать одну правку (или все, если id не задан). Возвращает, сколько убрано."""
    убрано = 0

    def _убрать(doc: dict | None) -> dict:
        nonlocal убрано
        existing = _только_дельты(doc)
        keep = [] if not element_id else [d for d in existing if str(d.get("id")) != element_id]
        убрано = len(existing) - len(keep)
        return {"version": _VERSION, "deltas": keep}

    state.mutate(state.METHODOLOGY, owner=int(user_id), key="overlay", изменить=_убрать)
    return убрано


def remember_for_turn(user_id: int, delta: dict) -> None:
    """Правка «на этот раз» — живёт до ближайшего анализа и им съедается."""
    md.apply([delta])  # проверяем сразу: разовая правка тоже обязана применяться
    now = time.time()
    _, cur = _turn_deltas.get(int(user_id)) or (now, [])
    _turn_deltas[int(user_id)] = (now, [*cur, dict(delta)])


def one_shot_pending(user_id: int | None) -> list[dict]:
    """Разовые правки, ещё НЕ израсходованные. Не съедает — только смотрит.

    Нужна тому, кто выдаёт методику в станцию: выдача правку уносит, и после
    неё сказать «разовая правка потрачена этим ходом» уже не по чему. Правило
    свежести здесь ОДНО с `_take_turn_deltas` — разъехавшись, они дали бы
    предупреждение о правке, которой в выдаче не было.
    """
    ts, deltas = _turn_deltas.get(int(user_id or 0)) or (0.0, [])
    return [] if not deltas or time.time() - ts > _TURN_TTL_SEC else list(deltas)


def _take_turn_deltas(user_id: int) -> list[dict]:
    fresh = one_shot_pending(user_id)
    _turn_deltas.pop(int(user_id), None)
    return fresh


def deltas_for_run(user_id: int | None, *, consume_turn: bool = True) -> list[dict]:
    """Правки этого прогона: сохранённые + разовые. САМИ дельты, не методика.

    Разбор считает не здесь, а в запечатанной комнате станции, и методику она
    собирает у себя (`md.apply` внутри комнаты). Значит ей нужны правки, а не
    результат их наложения, — иначе методику пришлось бы сериализовать целиком
    и вторым способом.

    Горизонты и порядок — те же, что у `effective`, и это ОДНА функция на обе
    стороны намеренно: разовая правка идёт последней и перекрывает сохранённую,
    и правило это обязано быть одно. Два места, решающих «что действует в этом
    прогоне», разъехались бы молча.
    """
    if not user_id:
        return []
    deltas = [*load(user_id)]
    if consume_turn:
        deltas += _take_turn_deltas(user_id)
    return deltas


def effective(user_id: int | None, *, consume_turn: bool = True) -> md.Methodology:
    """Методика этого прогона: база + сохранённые правки + разовые.

    Не бросает: прогон не должен падать из-за того, что мы переписали базу.
    Потерянные якоря приезжают в `notices` и уходят в провенанс карточки.
    """
    deltas = deltas_for_run(user_id, consume_turn=consume_turn)
    if not deltas:
        return md.base()
    return md.apply(deltas, strict=False)


def describe(user_id: int | None) -> dict:
    """Что у пользователя своё — для показа ему и для провенанса карточки."""
    m = effective(user_id, consume_turn=False)
    return {
        "changes": [dict(c) for c in m.changes],
        "notices": [dict(n) for n in m.notices],
    }
