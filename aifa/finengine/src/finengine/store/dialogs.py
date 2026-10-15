"""Хранилище диалогов: СТРОКА НА ДИАЛОГ вместо одного файла на пользователя.

ДЕФЕКТ, который здесь закрывается. Вся переписка пользователя лежала в одном
json-файле `data/dialogs/<user_id>.json`, и файл переписывался ЦЕЛИКОМ на
каждое сохранение — а сохранение идёт по дебаунсу из каждой вкладки, десятки
раз за сеанс. К августу 2026 файл дорос до 2,27 МБ; рядом с ним на стенде
лежат `.bak-docfix`, `.bak-recover`, `.bak-sovcom` — это не «на всякий
случай», это следы уже случившихся потерь. Причина у них общая: единицей
записи были ВСЕ диалоги пользователя сразу, поэтому любая беда в момент
перезаписи (или один затирающий писатель) уносила переписку целиком.

Здесь единица записи — ОДИН диалог. Пуш одного чата трогает одну строку;
остальные строки не переписываются вовсе, и авария на одной строке не может
утащить соседние. Хранилище — PostgreSQL (решение владельца продукта: SQLite
не предлагать).

ПОЧЕМУ SQLAlchemy Core, а не голый psycopg. sqlalchemy>=2.0 уже в
зависимостях (на ней живёт база пользователей), поэтому новый слой не заводит
второй способ ходить в БД. Core, а не ORM: тут две таблицы и десяток запросов,
модели/сессии ORM не упростили бы ни строчки, зато спрятали бы SQL. Драйвер
Postgres — psycopg 3 (`postgresql+psycopg://...`), добавлен в pyproject.
Побочная выгода, ради которой выбор и сделан так: ЭТОТ ЖЕ класс исполняется на
любом движке, который понимает SQLAlchemy, — тесты гоняют его на временной
файловой базе, не поднимая Postgres (постгреса на dev-VPS сейчас нет). Чего
такой тест НЕ доказывает — написано в шапке tests/test_dialogs_store.py.

Слияние снимков (merge-safe) и серверный tripwire остались там же, где были, —
в ручке PUT /dialogs. Здесь только запись и чтение: хранилище не решает, чья
версия сообщения победила, оно лишь кладёт результат построчно.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Iterable, Protocol

from sqlalchemy import (
    JSON,
    BigInteger,
    Column,
    MetaData,
    Table,
    Text,
    delete,
    insert,
    select,
    update,
)
from sqlalchemy.dialects.postgresql import JSONB

from finengine.config import settings
from finengine.store.engine import движок

log = logging.getLogger(__name__)


def _empty_state() -> dict:
    """Пустой ответ ручки GET /dialogs — та же форма, что отдавал файл."""
    return {"projects": [], "folders": [], "last_project_id": None, "saved_at": None}


class DialogStore(Protocol):
    """Что обязано уметь хранилище диалогов.

    `changed_ids` — список диалогов, которых КАСАЛСЯ пуш. Хранилище со строкой
    на диалог обязано записать только их; `None` означает «пиши всё» и нужен
    переносу из файлов, где адресной правки не было.
    """

    def load(self, user_id: int) -> dict: ...

    def save(self, user_id: int, blob: dict, changed_ids: Iterable[str] | None = None) -> None: ...

    def delete_all(self, user_id: int) -> None: ...


class FileDialogStore:
    """Старое хранилище: один json на пользователя, атомарная подмена файла.

    Оставлено рабочим и остаётся ДЕФОЛТОМ: переезд живых пользователей на базу —
    отдельное решение владельца, а не побочный эффект выкладки кода.

    `changed_ids` здесь игнорируется — и это ровно тот дефект, ради которого
    появился SQL-вариант: файл всегда пишется целиком, адресовать одну беседу
    в нём нечем.
    """

    def __init__(self, directory: Path):
        self._dir = directory

    def _path(self, user_id: int) -> Path:
        return self._dir / f"{user_id}.json"

    def load(self, user_id: int) -> dict:
        path = self._path(user_id)
        if not path.exists():
            return _empty_state()
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                raise ValueError("blob is not a dict")
            data.setdefault("projects", [])
            data.setdefault("folders", [])
            return data
        except (OSError, json.JSONDecodeError, ValueError):
            # Битый файл — отдаём пустое, а не 500: клиент перезапишет.
            return _empty_state()

    def save(self, user_id: int, blob: dict, changed_ids: Iterable[str] | None = None) -> None:
        path = self._path(user_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(blob, f, ensure_ascii=False)
            f.flush()
        tmp.replace(path)  # atomic on POSIX — readers never see a partial file

    def delete_all(self, user_id: int) -> None:
        self._path(user_id).unlink(missing_ok=True)


# ── Схема ──────────────────────────────────────────────────────────────
# Две таблицы, обе с user_id в первичном ключе: владелец — часть адреса
# строки, поэтому «прочитать/переписать чужой диалог» не выражается в этой
# схеме даже случайно.
_metadata = MetaData()

# JSONB на Postgres (бинарный, индексируемый), обычный JSON на прочих движках.
# Один и тот же код так работает и в проде, и на временной базе теста.
_JSON = JSON().with_variant(JSONB(), "postgresql")

# Шапка пользователя: всё, что НЕ является диалогом. Папок и надгробий мало,
# они меняются вместе со списком — дробить их на строки смысла нет.
DIALOG_OWNER = Table(
    "dialog_owner",
    _metadata,
    # autoincrement=False обязателен: без него SQLAlchemy считает одиночный
    # целочисленный первичный ключ генерируемым и создаёт в Postgres BIGSERIAL
    # с последовательностью. Здесь user_id — чужой идентификатор из таблицы
    # пользователей, а не наш счётчик; сгенерированный номер тихо привязал бы
    # шапку не к тому человеку.
    Column("user_id", BigInteger, primary_key=True, autoincrement=False),
    Column("last_project_id", Text),
    Column("folders", _JSON, nullable=False),
    # Надгробия удалённых чатов: сервер удаляет диалог ТОЛЬКО по явному списку
    # клиента, и этот же реестр гасит воскрешение чата вкладкой со старым
    # снимком (логика — в ручке PUT /dialogs).
    Column("deleted", _JSON, nullable=False),
    Column("saved_at", Text),
)

DIALOG = Table(
    "dialog",
    _metadata,
    Column("user_id", BigInteger, primary_key=True),
    Column("project_id", Text, primary_key=True),
    # Карточка диалога целиком — схему внутри владеет клиент, сервер её не
    # интерпретирует (кроме id/updated_at). Разбирать сообщения по строкам
    # смысла нет: сообщения меняются только вместе со своим диалогом.
    Column("payload", _JSON, nullable=False),
    Column("updated_at", BigInteger, nullable=False),
    # Сколько раз строку писали. Приложению не нужен — нужен проверке: тест на
    # изоляцию соседей доказывает через него, что пуш одного диалога не сдвинул
    # rev остальных. Без такого следа «строка на диалог» осталась бы
    # декларацией, которую нечем подтвердить.
    Column("rev", BigInteger, nullable=False),
)


def _as_int(value: object) -> int:
    """updated_at приходит от клиента и бывает чем угодно — в колонку кладём
    число, иначе строка «1754…» уронит вставку в Postgres."""
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


class SqlDialogStore:
    """Строка на диалог. В проде — Postgres, движок берётся из строки подключения."""

    def __init__(self, url: str):
        # echo=False намеренно и осознанно: в строке подключения пароль, а с
        # echo=True SQLAlchemy печатает её в лог. Секрет живёт в окружении и
        # не должен всплыть ни в логе, ни в трейсе.
        self._engine = движок(url, pool_pre_ping=True, future=True, echo=False)
        # Alembic в репозитории нет, а таблиц две — создаём при первом обращении.
        # create_all идемпотентен (CREATE TABLE IF NOT EXISTS).
        _metadata.create_all(self._engine)

    def load(self, user_id: int) -> dict:
        with self._engine.connect() as conn:
            head = conn.execute(
                select(DIALOG_OWNER).where(DIALOG_OWNER.c.user_id == user_id)
            ).mappings().first()
            rows = conn.execute(
                select(DIALOG.c.payload)
                .where(DIALOG.c.user_id == user_id)
                .order_by(DIALOG.c.updated_at, DIALOG.c.project_id)
            ).scalars().all()
        state = _empty_state()
        state["projects"] = [r for r in rows if isinstance(r, dict)]
        if head is not None:
            state["folders"] = head["folders"] or []
            state["deleted"] = head["deleted"] or {}
            state["last_project_id"] = head["last_project_id"]
            state["saved_at"] = head["saved_at"]
        return state

    def save(self, user_id: int, blob: dict, changed_ids: Iterable[str] | None = None) -> None:
        touched = None if changed_ids is None else {str(i) for i in changed_ids}
        tombs = blob.get("deleted") or {}
        # Одна транзакция на пуш: либо доехали все затронутые диалоги, либо ни
        # один. Полу-применённого пуша — состояния, из-за которого и заводили
        # .bak-файлы, — быть не может.
        with self._engine.begin() as conn:
            for project in blob.get("projects") or []:
                if not isinstance(project, dict) or not project.get("id"):
                    continue
                pid = str(project["id"])
                if touched is not None and pid not in touched:
                    # СУТЬ ПЕРЕЕЗДА: диалог, которого пуш не касался, не
                    # переписывается вовсе. В файле это было невыразимо.
                    continue
                stamp = _as_int(project.get("updated_at"))
                # UPDATE, а при отсутствии строки — INSERT. Это переносимо между
                # движками (ON CONFLICT пишется по-разному) и достаточно: пуши
                # одного пользователя сериализованы блокировкой в ручке, а
                # бэкенд запущен одним процессом (uvicorn --workers 1).
                done = conn.execute(
                    update(DIALOG)
                    .where(DIALOG.c.user_id == user_id, DIALOG.c.project_id == pid)
                    .values(payload=project, updated_at=stamp, rev=DIALOG.c.rev + 1)
                )
                if done.rowcount == 0:
                    conn.execute(
                        insert(DIALOG).values(
                            user_id=user_id,
                            project_id=pid,
                            payload=project,
                            updated_at=stamp,
                            rev=1,
                        )
                    )
            if tombs:
                conn.execute(
                    delete(DIALOG).where(
                        # Владелец в условии удаления обязателен: список надгробий
                        # приходит ОТ КЛИЕНТА, и без скоупа чужой чат с тем же id
                        # сносится вместе со своим (сторож —
                        # test_tombstone_of_one_user_does_not_delete_the_same_id_of_another).
                        DIALOG.c.user_id == user_id,
                        DIALOG.c.project_id.in_([str(k) for k in tombs]),
                    )
                )
            head = dict(
                last_project_id=blob.get("last_project_id"),
                folders=blob.get("folders") or [],
                deleted=tombs,
                saved_at=blob.get("saved_at"),
            )
            done = conn.execute(
                update(DIALOG_OWNER).where(DIALOG_OWNER.c.user_id == user_id).values(**head)
            )
            if done.rowcount == 0:
                conn.execute(insert(DIALOG_OWNER).values(user_id=user_id, **head))

    def delete_all(self, user_id: int) -> None:
        with self._engine.begin() as conn:
            conn.execute(delete(DIALOG).where(DIALOG.c.user_id == user_id))
            conn.execute(delete(DIALOG_OWNER).where(DIALOG_OWNER.c.user_id == user_id))


# Собранное хранилище кэшируется: create_engine держит пул соединений, поднимать
# его на каждый запрос нельзя. Ключ кэша — то, что определяет выбор (адрес базы
# и папка данных), поэтому смена настроек подхватывается без отдельной ручки
# сброса.
_cached: tuple[tuple[str, str], DialogStore] | None = None


def get_dialog_store() -> DialogStore:
    """ПЕРЕКЛЮЧАТЕЛЬ: база — если она настроена, иначе файлы, как раньше.

    Пустой `FINENGINE_DIALOGS_DATABASE_URL` (дефолт) = файловое хранилище.
    Момент переезда живых пользователей выбирает владелец — код не решает это
    за него и ничего не включает сам.
    """
    global _cached
    url = (settings.dialogs_database_url or "").strip()
    # 🔴 В КЛЮЧЕ И СХЕМА КОНТУРА. Она ставится ПОДКЛЮЧЕНИЮ (store/engine.py),
    # то есть живёт внутри движка: по одному адресу с двумя схемами кэш отдавал
    # бы хранилище, смотрящее в ЧУЖУЮ. Тот же урок уже куплен news/scenarios
    # первым прогоном тестов против настоящего Postgres.
    from finengine.store.engine import схема  # noqa: PLC0415 — разрыв цикла: engine не должен тянуть хранилища

    key = (url, str(settings.data_dir), схема())
    if _cached is not None and _cached[0] == key:
        return _cached[1]
    store: DialogStore = SqlDialogStore(url) if url else FileDialogStore(settings.data_dir / "dialogs")
    # Адрес базы не печатаем — в нём пароль.
    log.info("хранилище диалогов: %s", "база (строка на диалог)" if url else "файлы")
    _cached = (key, store)
    return store
