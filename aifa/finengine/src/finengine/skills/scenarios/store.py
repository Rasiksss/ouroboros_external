"""Хранилище сценариев («Сценарии»). SQLAlchemy Core — SQLite или Postgres.

🔴 ПЕРЕПИСАНО 25.08.2026 (решение владельца: «делай так, чтобы соответствовало
правилам разработки»). Было — голый `sqlite3` с `PRAGMA`, `sqlite_master`,
`executescript` и `INSERT OR REPLACE`: всё это есть ТОЛЬКО у SQLite, и на
Postgres модуль не запускался в принципе. Нарушалось `docs/rules/architecture.md`
2.1 (🔴 MUST) «Postgres — единственная БД… признак регрессии: файловые БД».

Заодно закрыто нарушение `docs/rules/security.md` 2.1 (🔴 MUST): `update` и
`set_status` собирали SQL f-строкой (`SET {', '.join(sets)}`). Имена колонок
брались из белого списка, то есть дырой это не было, — но правило требует
параметризации, а выражения Core её дают по построению.

ГДЕ ЛЕЖИТ. По умолчанию — тот же файл `<data_dir>/scenarios.db`, и SQLAlchemy
читает его как есть: данные никуда не переносятся, схема та же. Непустой
`FINENGINE_SCENARIOS_DATABASE_URL` переводит хранилище в базу; переезд —
`python -m finengine.store.migrate_scenarios`.

Сценарий принадлежит `user_id` и описывает ЧТО запускать (`kind` +
`instructions`) и КОГДА (`freq`/`at_time`/`dows`). Результат прогона здесь НЕ
хранится — он ложится в хранилище артефактов под id сценария (см. `runner.py`);
тут остаётся только сводка последнего запуска для списка.
"""
from __future__ import annotations

import dataclasses
import json
import logging
import time
import uuid
from pathlib import Path

from sqlalchemy import (
    Column, Float, Integer, MetaData, String, Table, Text, delete as sa_delete, inspect, select, update as sa_update,
)
from sqlalchemy.exc import IntegrityError

from finengine.config import settings
from finengine.store.engine import движок, схема

log = logging.getLogger(__name__)

# Allowed enums (validated at the API layer too; kept here as the source of truth).
KINDS = ("ir_calendar", "ir_daily", "agent_prompt", "aifa_monitor")
FREQS = ("manual", "hourly", "daily", "weekly")


_metadata = MetaData()

# Схема повторяет ту, что накопили миграции v1–v3 в SQLite: имена и умолчания
# совпадают, поэтому существующий файл читается как есть, без переноса.
SCENARIOS = Table(
    "scenarios", _metadata,
    Column("id", String(64), primary_key=True),
    Column("user_id", Integer, nullable=False, index=True),
    Column("name", Text, nullable=False),
    Column("kind", Text, nullable=False),
    Column("instructions", Text, nullable=False, default=""),
    Column("freq", Text, nullable=False, default="manual"),
    Column("at_time", Text, nullable=False, default="09:00"),
    Column("dows", Text, nullable=False, default=""),
    Column("paused", Integer, nullable=False, default=0),
    Column("created_at", Float, nullable=False),
    Column("last_run_at", Float),
    Column("last_status", Text, nullable=False, default=""),
    Column("last_run_id", Text, nullable=False, default=""),
    Column("last_error", Text, nullable=False, default=""),
    Column("description", Text, nullable=False, default=""),
    Column("project_id", Text, nullable=False, default=""),
    Column("model", Text, nullable=False, default=""),
    Column("params", Text, nullable=False, default=""),
)


@dataclasses.dataclass
class Scenario:
    id: str
    user_id: int
    name: str
    kind: str
    instructions: str
    freq: str
    at_time: str
    dows: str  # CSV of weekday ints, Mon=0 … Sun=6 (only for freq='weekly')
    paused: int
    created_at: float
    last_run_at: float | None = None
    last_status: str = ""
    last_run_id: str = ""
    last_error: str = ""
    description: str = ""
    project_id: str = ""
    model: str = ""
    params: str = ""  # JSON-строка настроек запуска (см. миграцию v3)

    @property
    def params_dict(self) -> dict:
        try:
            d = json.loads(self.params) if self.params else {}
            return d if isinstance(d, dict) else {}
        except ValueError:
            return {}

    @property
    def dow_list(self) -> list[int]:
        out: list[int] = []
        for tok in self.dows.split(","):
            tok = tok.strip()
            if tok.isdigit():
                n = int(tok)
                if 0 <= n <= 6:
                    out.append(n)
        return out

    def to_dict(self) -> dict:
        """API shape — schedule nested, paused as bool, dows as int list."""
        return {
            "id": self.id,
            "user_id": self.user_id,
            "name": self.name,
            "kind": self.kind,
            "instructions": self.instructions,
            "description": self.description,
            "project_id": self.project_id,
            "model": self.model,
            "params": self.params_dict,
            "schedule": {
                "freq": self.freq,
                "at_time": self.at_time,
                "dows": self.dow_list,
            },
            "paused": bool(self.paused),
            "created_at": self.created_at,
            "last_run_at": self.last_run_at,
            "last_status": self.last_status,
            "last_run_id": self.last_run_id,
            "last_error": self.last_error,
        }


def db_path() -> Path:
    """Путь СТАРОГО файла SQLite — теперь он нужен только переезду.

    Боевого файлового пути нет с 26.08.2026; функцию зовёт migrate_scenarios,
    читающий накопленное. (Прежний докстринг оправдывал её резервными
    копиями — бэкапы её не используют, сверено ревизией 27.08.)
    """
    return settings.data_dir / "scenarios.db"


# Кэш движка — ПО АДРЕСУ, а не «один на процесс».
#
# 🔴 ПОЧЕМУ ТАК (поймано прогоном 25.08). Прежний код открывал соединение по
# `db_path()` НА КАЖДЫЙ вызов, поэтому смена `settings.data_dir` немедленно
# переключала базу. Глобальный кэш это сломал: движок оставался от первого
# обращения, и обращение к другому каталогу молча шло в старую базу. Первыми
# это увидели тесты (каждый со своим tmp_path), но цена не тестовая: так же
# ведёт себя любая правка настроек на живом процессе.
_engines: dict[str, object] = {}


def engine():
    """Движок хранилища: база — если настроена, иначе тот же файл SQLite.

    Файл ТОТ ЖЕ, что и раньше: SQLAlchemy читает его как есть, схема совпадает,
    переносить ничего не требуется. Непустой адрес переводит на базу.
    """
    url = (settings.scenarios_database_url or "").strip()
    if not url:
        # 🔴 ФАЙЛОВЫЙ ЗАПАСНОЙ ПУТЬ СНЕСЁН 26.08.2026: здесь открывался
        # `sqlite:///scenarios.db`. Пока он был, ненастроенный контур работал «как
        # будто получилось» — писал в файл, которого никто не читает.
        raise RuntimeError(
            "база сценариев не настроена: пуст FINENGINE_SCENARIOS_DATABASE_URL. "
            "Файлового запасного пути больше нет (снесён 26.08.2026)")
    # 🔴 В КЛЮЧЕ И СХЕМА КОНТУРА, А НЕ ТОЛЬКО АДРЕС. Схема ставится
    # ПОДКЛЮЧЕНИЮ (`store/engine.py`), то есть живёт внутри движка: по одному
    # адресу с двумя схемами кэш отдавал бы движок, смотрящий в ЧУЖУЮ. Поймано
    # первым же прогоном тестов против настоящего Postgres — «no schema has
    # been selected to create in» на схеме, которую предыдущий тест уже снёс.
    ключ = f"{url}\n{схема()}"
    дв = _engines.get(ключ)
    if дв is None:
        # echo=False намеренно: в строке подключения пароль.
        дв = движок(url, pool_pre_ping=True, future=True, echo=False)
        _engines[ключ] = дв
    return дв


def init_schema() -> None:
    """Идемпотентно, зовётся на каждом старте бэкенда.

    ⚠️ БЕЗ `sqlite_master` И `executescript` — их нет у Postgres. Раньше версия
    схемы хранилась в таблице `schema_version` и миграции применялись строками
    SQL; теперь состав колонок объявлен таблицей, а недостающие в уже
    развёрнутой базе дописываются по инспектору — так же, как в `auth/db.py`.
    """
    дв = engine()
    _metadata.create_all(дв)
    инспектор = inspect(дв)
    если_есть = {c["name"] for c in инспектор.get_columns("scenarios")}
    from sqlalchemy import text as sa_text  # noqa: PLC0415

    with дв.begin() as conn:
        for имя in ("description", "project_id", "model", "params"):
            if имя not in если_есть:
                conn.execute(sa_text(
                    f"ALTER TABLE scenarios ADD COLUMN {имя} TEXT NOT NULL DEFAULT ''"))
                log.info("scenarios: добавлена колонка %s", имя)


def _row_to_scenario(row) -> Scenario:
    return Scenario(
        id=row["id"],
        user_id=row["user_id"],
        name=row["name"],
        kind=row["kind"],
        instructions=row["instructions"],
        freq=row["freq"],
        at_time=row["at_time"],
        dows=row["dows"],
        paused=row["paused"],
        created_at=row["created_at"],
        last_run_at=row["last_run_at"],
        last_status=row["last_status"],
        last_run_id=row["last_run_id"],
        last_error=row["last_error"],
        description=row["description"],
        params=row["params"],
        project_id=row["project_id"],
        model=row["model"],
    )


def _norm_dows(dows: list[int] | str | None) -> str:
    """Normalize a dow spec to a sorted, deduped CSV of 0-6 ints."""
    if dows is None:
        return ""
    if isinstance(dows, str):
        parts = [p.strip() for p in dows.split(",")]
    else:
        parts = [str(int(d)) for d in dows]
    seen: list[int] = []
    for p in parts:
        if p.isdigit():
            n = int(p)
            if 0 <= n <= 6 and n not in seen:
                seen.append(n)
    return ",".join(str(n) for n in sorted(seen))


def create(
    *,
    user_id: int,
    name: str,
    kind: str,
    instructions: str = "",
    description: str = "",
    project_id: str = "",
    model: str = "",
    freq: str = "manual",
    at_time: str = "09:00",
    dows: list[int] | str | None = None,
    paused: bool = False,
    params: dict | None = None,
    scenario_id: str | None = None,
) -> Scenario:
    scn = Scenario(
        id=scenario_id or uuid.uuid4().hex,
        user_id=int(user_id),
        name=name.strip() or "Сценарий",
        kind=kind,
        instructions=instructions or "",
        freq=freq if freq in FREQS else "manual",
        at_time=at_time or "09:00",
        dows=_norm_dows(dows),
        paused=1 if paused else 0,
        created_at=time.time(),
        description=description or "",
        project_id=project_id or "",
        model=model or "",
        params=json.dumps(params or {}, ensure_ascii=False),
    )
    try:
        with engine().begin() as conn:
            conn.execute(SCENARIOS.insert().values(
                id=scn.id, user_id=scn.user_id, name=scn.name, kind=scn.kind,
                instructions=scn.instructions, description=scn.description,
                project_id=scn.project_id, model=scn.model, freq=scn.freq,
                at_time=scn.at_time, dows=scn.dows, paused=scn.paused,
                created_at=scn.created_at, last_run_at=None,
                last_status="", last_run_id="", last_error="", params=scn.params))
    except IntegrityError:
        if scenario_id:
            existing = get(scenario_id)
            if existing is not None:
                return existing
        raise
    log.info("scenario created id=%s user=%s kind=%s freq=%s", scn.id, user_id, kind, freq)
    return scn


def get(scenario_id: str) -> Scenario | None:
    with engine().begin() as conn:
        row = conn.execute(
            select(SCENARIOS).where(SCENARIOS.c.id == scenario_id)).mappings().first()
    return _row_to_scenario(row) if row else None


def list_for_user(user_id: int) -> list[Scenario]:
    with engine().begin() as conn:
        rows = conn.execute(
            select(SCENARIOS).where(SCENARIOS.c.user_id == int(user_id))
            .order_by(SCENARIOS.c.created_at.desc())).mappings().all()
    return [_row_to_scenario(r) for r in rows]


def list_all_active() -> list[Scenario]:
    """Scenarios eligible for the scheduler: not paused, recurring freq."""
    with engine().begin() as conn:
        rows = conn.execute(
            select(SCENARIOS).where(SCENARIOS.c.paused == 0)
            .where(SCENARIOS.c.freq.in_(("hourly", "daily", "weekly")))).mappings().all()
    return [_row_to_scenario(r) for r in rows]


def list_unpaused_kind(kind: str) -> list[Scenario]:
    """Rows for event dispatchers; unlike the clock poller, freq may be manual."""
    with engine().begin() as conn:
        rows = conn.execute(
            select(SCENARIOS).where(SCENARIOS.c.paused == 0)
            .where(SCENARIOS.c.kind == kind)).mappings().all()
    return [_row_to_scenario(r) for r in rows]


# Built-in skills seeded for every user so the two core skills are always
# present as scenarios (they were the old fixed «Routines»). Seeded manual —
# the user adds a schedule via «Изменить».
_DEFAULT_SCENARIOS = (
    {"name": "IR Календарь", "kind": "ir_calendar",
     "description": "Календарь корпоративных событий по покрытию (±14 дней)."},
    {"name": "IR Daily", "kind": "ir_daily",
     "description": "Ежедневный дайджест ключевых новостей рынка."},
)


def ensure_defaults(user_id: int) -> None:
    """Seed the two built-in scenarios for a user who has NONE yet. Idempotent
    and conservative: only seeds when the user has zero scenarios, so deleting
    a default later won't resurrect it (unless the user deletes everything)."""
    if list_for_user(user_id):
        return
    for d in _DEFAULT_SCENARIOS:
        create(
            user_id=user_id, name=d["name"], kind=d["kind"],
            description=d["description"], freq="manual",
        )


_UPDATABLE = {
    "name", "kind", "instructions", "description", "project_id", "model",
    "freq", "at_time", "dows", "paused", "params",
}


def update(scenario_id: str, **fields) -> Scenario | None:
    """Patch updatable columns. Unknown keys ignored. Returns the new row."""
    # ⚠️ БЕЗ f-СТРОК В SQL (security.md 2.1). Имена колонок и раньше брались из
    # белого списка `_UPDATABLE`, то есть дырой это не было; но правило требует
    # параметризации, а выражение Core даёт её по построению.
    значения: dict[str, object] = {}
    for k, v in fields.items():
        if k not in _UPDATABLE or v is None:
            continue
        if k == "dows":
            v = _norm_dows(v)
        elif k == "paused":
            v = 1 if v else 0
        elif k == "freq" and v not in FREQS:
            continue
        elif k == "params" and not isinstance(v, str):
            v = json.dumps(v, ensure_ascii=False)
        значения[k] = v
    if not значения:
        return get(scenario_id)
    with engine().begin() as conn:
        conn.execute(sa_update(SCENARIOS).where(SCENARIOS.c.id == scenario_id)
                     .values(**значения))
    return get(scenario_id)


def set_status(
    scenario_id: str,
    *,
    last_status: str,
    last_run_at: float | None = None,
    last_run_id: str | None = None,
    last_error: str = "",
) -> None:
    """Record a run outcome on the scenario row (used by the runner)."""
    значения: dict[str, object] = {"last_status": last_status,
                                   "last_error": last_error or ""}
    if last_run_at is not None:
        значения["last_run_at"] = last_run_at
    if last_run_id is not None:
        значения["last_run_id"] = last_run_id
    with engine().begin() as conn:
        conn.execute(sa_update(SCENARIOS).where(SCENARIOS.c.id == scenario_id)
                     .values(**значения))


def sweep_stale_running() -> int:
    """Пометить зависшие 'running' как error («прерван перезапуском сервиса»).

    Зовётся на старте бэкенда. Куплено живым случаем 17.08.2026: пересборка
    контейнера при деплое убила процесс посреди прогона ir_calendar
    (accc83b6…) — финализация статуса в runner.py не выполнилась, и строка
    висела 'running' >4,5 часа, владелец видел вечное «выполняется». Прогоны
    живут в том же процессе, что и API, поэтому после рестарта любой
    'running' гарантированно мёртв.
    """
    with engine().begin() as conn:
        cur = conn.execute(
            sa_update(SCENARIOS).where(SCENARIOS.c.last_status == "running")
            .values(last_status="error", last_error="прерван перезапуском сервиса"))
        swept = cur.rowcount
    if swept:
        log.warning("scenarios: swept %d stale 'running' row(s) on startup", swept)
    return swept


def delete(scenario_id: str) -> bool:
    with engine().begin() as conn:
        cur = conn.execute(sa_delete(SCENARIOS).where(SCENARIOS.c.id == scenario_id))
        return cur.rowcount > 0
