"""Исход прогона станции: СТРОКА В БАЗЕ, а не рукопожатие с открытой вкладкой.

ДЕФЕКТ, который здесь закрывается (18.08.2026, два живых прогона). Исход хода
жил ровно в двух местах: в потоке SSE открытой вкладки и в localStorage
браузера. Прогон ДОМ.РФ (нонс `060f41c1`) и прогон ГК «Самолёт» (`4ce68a11`,
13 событий разом) потеряны ЦЕЛИКОМ: ящик хода закрылся по молчанию очереди,
приём отбросил события с «прогресс станции некуда доставить … подписчика нет»,
комната станции истекла, а `extract_runs.jsonl` пуст — журнал пишется только
из `finalize_*`, то есть только на успешной доставке. Час работы движка и
деньги на модель ушли в никуда, и восстановить их было неоткуда.

Причина у потери структурная: единственным местом хранения исхода был ЖИВОЙ
СЛУШАТЕЛЬ. Нет слушателя — нет и исхода. Здесь появляется место, которое
слушателя не требует: строка, адресуемая нонсом прогона.

ПОЧЕМУ SQLAlchemy Core и почему форма как у `dialogs.py` — по тем же причинам,
что там (см. его шапку): движок уже в зависимостях, тот же класс исполняется и
на Postgres, и на временной файловой базе теста. Объект `MetaData` СВОЙ, а не
общий с диалогами: общий `create_all` завёл бы таблицу прогонов на стенде, где
включены только диалоги, и наоборот.

⚠️ `create_all` создаёт ОТСУТСТВУЮЩИЕ таблицы, но НЕ МИГРИРУЕТ существующие.
Alembic в репозитории нет. Колонка, добавленная сюда после первого боевого
запуска, потребует ручного ALTER на стенде — и до него код будет падать на
каждом запросе. У диалогов ровно та же мина, и она нигде не записана. Поэтому
`screen` заведён СРАЗУ, хотя сегодняшний шаг его не пишет: его пишет следующий
(собранный экран после `finalize_*`), и дешевле держать пустую колонку, чем
чинить прод ручным ALTER.

ЧЕГО ЗДЕСЬ НЕТ И ПОЧЕМУ:
  * файлового варианта хранилища. У диалогов пустая настройка означала «файлы,
    как раньше», потому что файлы там были боевым хранилищем. Здесь его нет, и
    заводить второй формат значило бы добавить ещё одно место с теми же
    числами — ровно то, что запрещает #144;
  * строк прогона (`metrics`/`segment_rows`/`group_rows`) в отдельной таблице.
    Точка приёма их не видит: они приезжают событиями раньше терминала и
    оседают только в `data/station_events.jsonl`. Копить ли их второй
    append-only таблицей — решение владельца, а не побочный эффект этой правки.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Mapping, Protocol

from sqlalchemy import (
    JSON,
    BigInteger,
    Column,
    Index,
    MetaData,
    Table,
    Text,
    insert,
    select,
    update,
)
from sqlalchemy.dialects.postgresql import JSONB

from finengine.config import settings
from finengine.store.engine import движок

log = logging.getLogger(__name__)

# Статусы строки. Значение хранилище не валидирует — виды прогонов живут в
# `api/station_turn`, а слой store импортировать слой api не может.
STATUS_RUNNING = "running"
STATUS_DONE = "done"
STATUS_FAILED = "failed"
# ЧЕТВЁРТЫЙ СТАТУС, И ОН НЕ ИЗ ЭСТЕТИКИ (20.08.2026). Канал прогона взводится на
# КАЖДЫЙ ход обоих видов, а запускают в ходе обычно один. Канал, не увидевший ни
# одного события, оставался `running` НАВСЕГДА: в базе 12 строк `analyze`, все
# «идут», ни одна никогда не закрывалась. По такой строке нельзя отличить «разбор
# не запускали» от «разбор завис» — а это ровно тот вопрос, который владелец
# задал про два прогона Северстали.
#
# ЦЕНА НОВОГО ЗНАЧЕНИЯ: его обязан знать каждый, кто читает статус. Трёх
# имеющихся не хватает по существу — `failed` означает, что прогон сломался
# (его пошли бы разбирать), `done` что он дал результат (его пошли бы искать).
# Записать «не запускался» любым из них значит соврать, а оставить `running` —
# соврать молча, что и делалось до сих пор.
STATUS_NOT_STARTED = "not_started"
_TERMINAL = (STATUS_DONE, STATUS_FAILED, STATUS_NOT_STARTED)

_metadata = MetaData()

# JSONB на Postgres (бинарный, индексируемый), обычный JSON на прочих движках —
# один и тот же код так работает и в проде, и на временной базе теста.
_JSON = JSON().with_variant(JSONB(), "postgresql")

STATION_RUN = Table(
    "station_run",
    _metadata,
    # Нонс — адрес прогона и его первичный ключ: он уникален на ход (uuid4 в
    # `/chat/turns`), у анализа того же хода свой (суффикс `-a`). Ключа по
    # документу быть не может — прогонов по одному документу столько, сколько
    # раз пользователь нажал кнопку, и второй не смеет затирать первый.
    Column("nonce", Text, primary_key=True),
    Column("kind", Text, nullable=False),
    Column("status", Text, nullable=False),
    # Владелец в схеме НЕ часть ключа (ключ уже уникален), поэтому защита от
    # чтения чужого исхода переезжает в сигнатуры чтения: `user_id` —
    # обязательный именованный аргумент и входит в КАЖДЫЙ читающий WHERE.
    Column("user_id", BigInteger, nullable=False),
    Column("project_id", Text, nullable=False, default=""),
    Column("session_id", Text, nullable=False, default=""),
    Column("run_id", Text, nullable=False, default=""),
    # Известен НЕ СРАЗУ: связь нонс→документ ставится, когда комната забирает
    # текст, то есть уже после взвода канала.
    Column("doc_id", Text),
    Column("payload", _JSON),
    Column("screen", _JSON),
    Column("error", Text),
    Column("started_at", BigInteger, nullable=False),
    # 0 = прогон ещё идёт. По нему же отбирается «последний по документу»:
    # незавершённый исходом не является.
    Column("finished_at", BigInteger, nullable=False, default=0),
    # Сколько раз строку писали. Приложению не нужен — нужен проверке: без него
    # «повторный терминал не стёр собранный экран» доказать нечем.
    Column("rev", BigInteger, nullable=False),
    Index("ix_station_run_doc", "user_id", "doc_id", "kind", "finished_at"),
    Index("ix_station_run_project", "user_id", "project_id", "started_at"),
)


@dataclass(frozen=True)
class RunOutcome:
    """Исход прогона одной записью. `rev` заполняется только на чтении."""

    nonce: str
    kind: str
    status: str
    user_id: int
    project_id: str = ""
    session_id: str = ""
    run_id: str = ""
    doc_id: str | None = None
    payload: dict | None = None
    screen: dict | None = None
    error: str | None = None
    started_at: int = 0
    finished_at: int = 0
    rev: int = 0


class RunStore(Protocol):
    """Что обязано уметь хранилище исходов.

    `save` и `finish` — РАЗНЫЕ операции, а не флаг одной. Взвод знает владельца
    и заводит строку; терминал приезжает в точку приёма, где владельца нет
    вовсе (`ProgressBatch` несёт только нонс и события) — он лишь дополняет уже
    заведённую строку и честно возвращает `False`, если её нет.
    """

    def save(self, outcome: RunOutcome) -> None: ...

    def finish(self, nonce: str, *, status: str, payload: dict | None = None,
               error: str | None = None, doc_id: str = "",
               finished_at: int = 0) -> bool: ...

    def load(self, nonce: str, *, user_id: int) -> RunOutcome | None: ...

    def latest_for_doc(self, doc_id: str, *, user_id: int,
                       kind: str = "") -> RunOutcome | None: ...


class NullRunStore:
    """Хранилище выключено: ничего не пишем, ничего не отдаём.

    ДЕФОЛТ. Пустой `FINENGINE_RUNS_DATABASE_URL` = поведение ровно как до этой
    правки: живая доставка и журналы `extract_runs.jsonl`/`analysis_runs.jsonl`
    работают как работали, подключения к базе не создаётся вовсе. Момент
    переезда живых пользователей выбирает владелец — код не решает это за него.
    """

    def save(self, outcome: RunOutcome) -> None:
        return None

    def finish(self, nonce: str, *, status: str, payload: dict | None = None,
               error: str | None = None, doc_id: str = "",
               finished_at: int = 0) -> bool:
        return False

    def load(self, nonce: str, *, user_id: int) -> RunOutcome | None:
        return None

    def latest_for_doc(self, doc_id: str, *, user_id: int,
                       kind: str = "") -> RunOutcome | None:
        return None


# «Мне про это поле нечего сказать»: у строки ТРИ писателя (взвод, терминал,
# сборка экрана), и каждый знает свою часть. Без этого правила терминал,
# пришедший в точку приёма, затёр бы `None`-ами проект, сессию и владельца,
# которых он не видит.
_SILENT = (None, "", 0)


def _merged_status(stored: str, incoming: str) -> str:
    """Статус МОНОТОНЕН: терминал не понижается до «идёт», успех — до отказа,
    а «не запускался» уступает любому приехавшему исходу.

    ЗАЧЕМ (дубль прогона, #185: 18.08 в комнате ДОМ.РФ шли ТРИ копии
    извлечения). Копия, добежавшая позже и упавшая, не имеет права стереть уже
    доставленный результат: на экране у пользователя таблица, а в базе оказался
    бы `failed`. Обратное направление разрешено — отказ, за которым пришёл
    успех, был неокончательным.
    """
    if not incoming or incoming == stored:
        return stored
    if stored in _TERMINAL and incoming == STATUS_RUNNING:
        return stored
    if stored == STATUS_DONE and incoming == STATUS_FAILED:
        return stored
    # «Не запускался» — самый слабый терминал: он говорит об отсутствии событий
    # к концу хода, а прогон переживает ход и присылает терминал позже (отцепка,
    # #276). Приехавший исход всегда сильнее вывода из тишины; обратное затёрло
    # бы доставленный результат тем, что его в своё время не увидели.
    if incoming == STATUS_NOT_STARTED and stored in _TERMINAL:
        return stored
    return incoming


def _merged(row: Mapping[str, Any], changes: dict) -> dict:
    """Слить приехавшее с записанным. Молчание не затирает, `rev` растёт."""
    values: dict = {"rev": int(row["rev"]) + 1}
    for column, new in changes.items():
        if column == "status":
            values[column] = _merged_status(str(row[column] or ""), str(new or ""))
            continue
        values[column] = row[column] if new in _SILENT else new
    return values


def _as_outcome(row: Mapping[str, Any]) -> RunOutcome:
    return RunOutcome(
        nonce=row["nonce"], kind=row["kind"], status=row["status"],
        user_id=int(row["user_id"]), project_id=row["project_id"] or "",
        session_id=row["session_id"] or "", run_id=row["run_id"] or "",
        doc_id=row["doc_id"], payload=row["payload"], screen=row["screen"],
        error=row["error"], started_at=int(row["started_at"] or 0),
        finished_at=int(row["finished_at"] or 0), rev=int(row["rev"] or 0))


class SqlRunStore:
    """Строка на прогон. В проде — Postgres, движок берётся из строки подключения."""

    def __init__(self, url: str):
        # echo=False намеренно: в строке подключения пароль, а с echo=True
        # SQLAlchemy печатает её в лог.
        self._engine = движок(url, pool_pre_ping=True, future=True, echo=False)
        # Alembic в репозитории нет — таблица создаётся при первом обращении.
        # create_all идемпотентен, но существующую таблицу НЕ мигрирует (см. шапку).
        _metadata.create_all(self._engine)

    def save(self, outcome: RunOutcome) -> None:
        """Завести строку прогона или дополнить её. Владелец обязателен."""
        with self._engine.begin() as conn:
            row = conn.execute(
                select(STATION_RUN).where(STATION_RUN.c.nonce == outcome.nonce)
            ).mappings().first()
            changes = {
                "kind": outcome.kind, "status": outcome.status,
                "project_id": outcome.project_id, "session_id": outcome.session_id,
                "run_id": outcome.run_id, "doc_id": outcome.doc_id,
                "payload": outcome.payload, "screen": outcome.screen,
                "error": outcome.error, "started_at": outcome.started_at,
                "finished_at": outcome.finished_at,
            }
            if row is None:
                conn.execute(insert(STATION_RUN).values(
                    nonce=outcome.nonce, user_id=outcome.user_id, rev=1, **changes))
                return
            # UPDATE, а при отсутствии строки — INSERT (а не ON CONFLICT):
            # переносимо между движками, и достаточно при одном процессе
            # бэкенда (uvicorn --workers 1). Владельца не трогаем: он ставится
            # на взводе и меняться не может.
            conn.execute(update(STATION_RUN)
                         .where(STATION_RUN.c.nonce == outcome.nonce)
                         .values(**_merged(row, changes)))

    def finish(self, nonce: str, *, status: str, payload: dict | None = None,
               error: str | None = None, doc_id: str = "",
               finished_at: int = 0) -> bool:
        """Закрыть СУЩЕСТВУЮЩУЮ строку терминалом. `False` = строки нет.

        Строку не заводим: у приёма нет ни владельца, ни проекта, а безымянный
        исход не сможет прочитать никто — он был бы мусором, выглядящим как
        сохранённый результат.
        """
        with self._engine.begin() as conn:
            row = conn.execute(
                select(STATION_RUN).where(STATION_RUN.c.nonce == nonce)
            ).mappings().first()
            if row is None:
                return False
            conn.execute(update(STATION_RUN).where(STATION_RUN.c.nonce == nonce)
                         .values(**_merged(row, {
                             "status": status, "payload": payload, "error": error,
                             "doc_id": doc_id, "finished_at": finished_at})))
        return True

    def load(self, nonce: str, *, user_id: int) -> RunOutcome | None:
        """Исход по нонсу. Чужой нонс = `None`, а не отказ: существование
        чужого прогона — тоже сведения о чужом прогоне."""
        with self._engine.connect() as conn:
            row = conn.execute(
                select(STATION_RUN).where(STATION_RUN.c.nonce == nonce,
                                          STATION_RUN.c.user_id == user_id)
            ).mappings().first()
        return _as_outcome(row) if row is not None else None

    def latest_for_doc(self, doc_id: str, *, user_id: int,
                       kind: str = "") -> RunOutcome | None:
        """Последний ЗАВЕРШЁННЫЙ прогон по документу. «Идёт» — не исход."""
        query = (select(STATION_RUN)
                 .where(STATION_RUN.c.user_id == user_id,
                        STATION_RUN.c.doc_id == doc_id,
                        STATION_RUN.c.finished_at > 0)
                 .order_by(STATION_RUN.c.finished_at.desc(),
                           STATION_RUN.c.started_at.desc(),
                           STATION_RUN.c.nonce)
                 .limit(1))
        if kind:
            query = query.where(STATION_RUN.c.kind == kind)
        with self._engine.connect() as conn:
            row = conn.execute(query).mappings().first()
        return _as_outcome(row) if row is not None else None


# Собранное хранилище кэшируется: create_engine держит пул соединений, поднимать
# его на каждый запрос нельзя. Ключ — только адрес базы: больше выбор ни от чего
# не зависит.
_cached: tuple[str, RunStore] | None = None


def get_run_store() -> RunStore:
    """ПЕРЕКЛЮЧАТЕЛЬ: база — если настроена, иначе выключено.

    Адрес НЕ наследуется от диалогов сознательно: иначе один тумблер владельца
    включал бы две миграции разом.
    """
    global _cached
    url = (settings.runs_database_url or "").strip()
    # 🔴 В КЛЮЧЕ И СХЕМА КОНТУРА. Она ставится ПОДКЛЮЧЕНИЮ (store/engine.py),
    # то есть живёт внутри движка: по одному адресу с двумя схемами кэш отдавал
    # бы хранилище, смотрящее в ЧУЖУЮ. Тот же урок уже куплен news/scenarios
    # первым прогоном тестов против настоящего Postgres.
    from finengine.store.engine import схема  # noqa: PLC0415 — разрыв цикла: engine не должен тянуть хранилища

    ключ = f"{url}|{схема()}"
    if _cached is not None and _cached[0] == ключ:
        return _cached[1]
    store: RunStore = SqlRunStore(url) if url else NullRunStore()
    # Адрес не печатаем — в нём пароль.
    log.info("исходы прогонов: %s", "база (строка на прогон)" if url else "выключено")
    _cached = (ключ, store)
    return store


def is_enabled() -> bool:
    """Включено ли хранилище исходов.

    Нужно ПИСАТЕЛЯМ, чтобы отличить «строки прогона нет» (беда, о которой надо
    сказать вслух) от «хранилище выключено» (штатный дефолт, о котором говорить
    на каждом событии значит залить лог шумом).
    """
    return bool((settings.runs_database_url or "").strip())


def save_best_effort(outcome: RunOutcome) -> None:
    """Записать исход так, чтобы отказ хранилища не уронил прогон.

    ПОСЛЕДНЯЯ СЕТЬ, и она здесь по той же причине, что у `_call_destination` в
    стоке: журнал не вправе уронить ход, ради которого пишется. Но и молчать
    нельзя — молчаливо проглоченный отказ базы неотличим от «всё сохранилось»,
    а именно эта неразличимость и стоила двух прогонов 18.08. Поэтому громко и
    со стеком, у ВСЕХ писателей одинаково: правило живёт в одном месте.
    """
    try:
        get_run_store().save(outcome)
    except Exception:  # noqa: BLE001 — запись исхода не смеет уронить прогон
        log.exception("исход прогона не записан: нонс=%s статус=%s",
                      outcome.nonce, outcome.status)


def finish_best_effort(nonce: str, *, status: str, payload: dict | None = None,
                       error: str | None = None, doc_id: str = "",
                       finished_at: int = 0) -> bool:
    """Терминал в хранилище с той же последней сетью. `False` = не записано."""
    try:
        return get_run_store().finish(
            nonce, status=status, payload=payload, error=error,
            doc_id=doc_id, finished_at=finished_at)
    except Exception:  # noqa: BLE001 — запись исхода не смеет уронить прогон
        log.exception("терминал прогона не записан: нонс=%s статус=%s", nonce, status)
        return False
