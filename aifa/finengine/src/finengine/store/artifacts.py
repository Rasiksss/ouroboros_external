"""Артефакты проекта: файлы на томе или строки в базе. Владелец и проект — в ключе.

Артефакт — сохранённый исход прогона навыка (календарь IR, дайджест новостей,
выгрузка показателей, разбор сценария): структурная нагрузка, которую рисует
интерфейс, плюс карточка (вид, время, параметры, модель, расход токенов).

ЗАЧЕМ ПЕРЕЕЗД. `docs/rules/architecture.md` 3.1 (🔴 MUST): «Нет состояния на
файловой системе контейнера… всё durable — во внешние хранилища». На томе
артефакты живут по три файла на прогон, и перечень проекта читается обходом
каталога с разбором каждой карточки — на реплике с read-only корневой ФС этого
нет вовсе, а на нескольких репликах перечни разъезжаются.

🔴 ГРАНИЦА ВЛАДЕЛЬЦА И ПРОЕКТА — ТА ЖЕ ЛОВУШКА, ЧТО У СЕССИЙ. На томе она
физическая: артефакт лежит в `artifacts/<user>/<project>/<run>/`, и чужой
`run_id` в чужом каталоге не находится. В базе всё в одной таблице, и
разделяют строки ровно два условия в запросе. Разбор цены ошибки — в шапке
`store/sessions.py`; здесь то же самое, и проверяется так же — одним набором
на обеих реализациях.

ПЕРЕКЛЮЧАТЕЛЬ. Пустой `FINENGINE_ARTIFACTS_DATABASE_URL` (по умолчанию) —
файлы, ровно как было. Непустой — база.

⚠️ ЭТОТ МОДУЛЬ ПЕРЕЕХАЛ ИЗ `skills/artifacts.py`. Он всегда был хранилищем, а
не навыком; лежал в `skills/` по историческим причинам. Девять мест импорта
поправлены механически, поведение не менялось.
"""
from __future__ import annotations

import dataclasses
import json
import logging
import time
import uuid
from pathlib import Path
from typing import Protocol

from sqlalchemy import (
    JSON, Column, Float, MetaData, String, Table, delete, select,
)

from finengine.config import settings
from finengine.store.engine import движок

log = logging.getLogger(__name__)


@dataclasses.dataclass
class ArtifactMeta:
    run_id: str
    kind: str  # "ir_calendar" | "ir_daily" | …
    created_at: float
    title: str = ""
    summary: str = ""  # короткая строка для списков
    params: dict | None = None  # параметры запроса, породившего прогон
    counts: dict | None = None  # например {"events": 47, "companies": 72}
    model: str = ""
    tokens: dict | None = None  # {"in": int, "out": int, "cost_usd": float}

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)


def _safe_id(s: str) -> str:
    """Ключ владельца/проекта/прогона. Одна функция на обе реализации.

    Набор символов сужен: значения приходят из запроса и не должны уводить путь
    за пределы корня артефактов. Для базы обход пути не страшен, но ключ обязан
    совпадать в обеих ветках — иначе после переезда перечень проекта окажется
    пустым, и выглядеть это будет как «артефакты пропали».
    """
    return "".join(c for c in s if c.isalnum() or c in ("-", "_")).strip("_-") or "_"


_metadata = MetaData()

ARTIFACT = Table(
    "artifact", _metadata,
    # Ключ составной по той же причине, что у сессий: граница между
    # пользователями не должна опираться на неповторимость идентификатора.
    Column("run_id", String(64), primary_key=True),
    Column("owner_key", String(64), primary_key=True, index=True),
    Column("project_key", String(128), primary_key=True, index=True),
    Column("created_at", Float, nullable=False),
    Column("meta", JSON, nullable=False),
    Column("payload", JSON, nullable=False),
)


class ArtifactStore(Protocol):
    def save(self, owner: str, project: str, meta: ArtifactMeta,
             payload: dict | list) -> None: ...
    def list(self, owner: str, project: str) -> list[ArtifactMeta]: ...
    def load(self, owner: str, project: str, run_id: str) -> dict | None: ...
    def delete(self, owner: str, project: str, run_id: str) -> bool: ...


class LocalArtifactStore:
    """Файлы на томе: `artifacts/<owner>/<project>/<run>/{meta,result}.json`.

    ⚠️ НЕ ЗАГЛУШКА: сегодняшнее боевое поведение и путь отката.
    """

    def _прогон(self, owner: str, project: str, run_id: str) -> Path:
        return Path(settings.data_dir) / "artifacts" / owner / project / run_id

    def save(self, owner: str, project: str, meta: ArtifactMeta,
             payload: dict | list) -> None:
        каталог = self._прогон(owner, project, meta.run_id)
        каталог.mkdir(parents=True, exist_ok=True)
        # Через временный файл и `replace`: перечень никогда не увидит
        # наполовину записанный JSON.
        for имя, значение in (("result.json", payload), ("meta.json", meta.to_dict())):
            врем = каталог / f"{имя}.tmp"
            врем.write_text(json.dumps(значение, ensure_ascii=False, indent=2),
                            encoding="utf-8")
            врем.replace(каталог / имя)

    def list(self, owner: str, project: str) -> list[ArtifactMeta]:
        проект = Path(settings.data_dir) / "artifacts" / owner / project
        if not проект.is_dir():
            return []
        итог: list[ArtifactMeta] = []
        for каталог in проект.iterdir():
            карточка = каталог / "meta.json"
            if not карточка.is_file():
                continue
            try:
                итог.append(ArtifactMeta(**json.loads(карточка.read_text(encoding="utf-8"))))
            except (json.JSONDecodeError, TypeError, ValueError) as e:
                log.warning("пропускаю битую карточку артефакта %s: %s", карточка, e)
        итог.sort(key=lambda m: m.created_at, reverse=True)
        return итог

    def load(self, owner: str, project: str, run_id: str) -> dict | None:
        каталог = self._прогон(owner, project, run_id)
        карточка, нагрузка = каталог / "meta.json", каталог / "result.json"
        if not карточка.is_file() or not нагрузка.is_file():
            return None
        return {"meta": json.loads(карточка.read_text(encoding="utf-8")),
                "payload": json.loads(нагрузка.read_text(encoding="utf-8"))}

    def delete(self, owner: str, project: str, run_id: str) -> bool:
        каталог = self._прогон(owner, project, run_id)
        if not каталог.is_dir():
            return False
        for f in каталог.iterdir():
            f.unlink(missing_ok=True)
        каталог.rmdir()
        return True


class SqlArtifactStore:
    """Строка на артефакт. Владелец и проект — в условии каждого обращения."""

    def __init__(self, url: str):
        # echo=False намеренно: в строке подключения пароль.
        self._engine = движок(url, pool_pre_ping=True, future=True, echo=False)
        _metadata.create_all(self._engine)

    def _где(self, owner: str, project: str, run_id: str | None = None):
        условия = [ARTIFACT.c.owner_key == owner, ARTIFACT.c.project_key == project]
        if run_id is not None:
            условия.append(ARTIFACT.c.run_id == run_id)
        return условия

    def save(self, owner: str, project: str, meta: ArtifactMeta,
             payload: dict | list) -> None:
        with self._engine.begin() as conn:
            conn.execute(delete(ARTIFACT).where(*self._где(owner, project, meta.run_id)))
            conn.execute(ARTIFACT.insert().values(
                run_id=meta.run_id, owner_key=owner, project_key=project,
                created_at=float(meta.created_at), meta=meta.to_dict(), payload=payload))

    def list(self, owner: str, project: str) -> list[ArtifactMeta]:
        # Перечень читает ТОЛЬКО карточки: нагрузка артефакта бывает в мегабайты,
        # а списку она не нужна. На томе так не получалось — там каждый прогон
        # это отдельный файл, и обход каталога всё равно шёл по всем.
        запрос = (select(ARTIFACT.c.meta).where(*self._где(owner, project))
                  .order_by(ARTIFACT.c.created_at.desc()))
        итог: list[ArtifactMeta] = []
        with self._engine.begin() as conn:
            for (карточка,) in conn.execute(запрос):
                d = json.loads(карточка) if isinstance(карточка, str) else карточка
                try:
                    итог.append(ArtifactMeta(**d))
                except TypeError as e:
                    log.warning("пропускаю битую карточку артефакта %s: %s",
                                d.get("run_id"), e)
        return итог

    def load(self, owner: str, project: str, run_id: str) -> dict | None:
        with self._engine.begin() as conn:
            строка = conn.execute(
                select(ARTIFACT.c.meta, ARTIFACT.c.payload)
                .where(*self._где(owner, project, run_id))).first()
        if not строка:
            return None
        карточка, нагрузка = строка
        return {"meta": json.loads(карточка) if isinstance(карточка, str) else карточка,
                "payload": json.loads(нагрузка) if isinstance(нагрузка, str) else нагрузка}

    def delete(self, owner: str, project: str, run_id: str) -> bool:
        with self._engine.begin() as conn:
            итог = conn.execute(delete(ARTIFACT).where(*self._где(owner, project, run_id)))
        return bool(итог.rowcount)


_cached: tuple[str, ArtifactStore] | None = None


def get_artifact_store() -> ArtifactStore:
    """ПЕРЕКЛЮЧАТЕЛЬ: база — если настроена, иначе файлы, как было."""
    global _cached
    url = (settings.artifacts_database_url or "").strip()
    # Каталог данных — в ключе кэша: в тестах он подменяется, и хранилище,
    # запомненное со старым каталогом, писало бы мимо.
    # 🔴 В КЛЮЧЕ И СХЕМА КОНТУРА. Она ставится ПОДКЛЮЧЕНИЮ (store/engine.py),
    # то есть живёт внутри движка: по одному адресу с двумя схемами кэш отдавал
    # бы хранилище, смотрящее в ЧУЖУЮ. Тот же урок уже куплен news/scenarios
    # первым прогоном тестов против настоящего Postgres.
    from finengine.store.engine import схема  # noqa: PLC0415 — разрыв цикла: engine не должен тянуть хранилища

    ключ = f"{url}|{settings.data_dir}|{схема()}"
    if _cached is not None and _cached[0] == ключ:
        return _cached[1]
    хранилище: ArtifactStore = SqlArtifactStore(url) if url else LocalArtifactStore()
    # Адрес не печатаем — в нём пароль.
    log.info("хранилище артефактов: %s", "база" if url else "файлы на томе")
    _cached = (ключ, хранилище)
    return хранилище


# ─────────────────────────── Открытый вызов ───────────────────────────
# Подписи не менялись при переезде: девять потребителей зовут эти функции,
# а не хранилище напрямую.


def save_artifact(
    user_id: int | str, project_id: str, kind: str, payload: dict | list, *,
    title: str = "", summary: str = "", params: dict | None = None,
    counts: dict | None = None, model: str = "", tokens: dict | None = None,
) -> ArtifactMeta:
    """Сохранить артефакт, вернуть его карточку."""
    meta = ArtifactMeta(
        run_id=uuid.uuid4().hex, kind=kind, created_at=time.time(),
        title=title, summary=summary, params=params or {}, counts=counts or {},
        model=model, tokens=tokens or {},
    )
    get_artifact_store().save(_safe_id(str(user_id)), _safe_id(project_id), meta, payload)
    log.info("сохранён артефакт user=%s project=%s kind=%s run=%s",
             user_id, project_id, kind, meta.run_id)
    return meta


def list_artifacts(user_id: int | str, project_id: str) -> list[ArtifactMeta]:
    """Все артефакты проекта, новые первыми."""
    return get_artifact_store().list(_safe_id(str(user_id)), _safe_id(project_id))


def load_artifact(user_id: int | str, project_id: str, run_id: str) -> dict:
    """`{meta, payload}` одного артефакта. Нет такого — `FileNotFoundError`."""
    итог = get_artifact_store().load(
        _safe_id(str(user_id)), _safe_id(project_id), _safe_id(run_id))
    if итог is None:
        raise FileNotFoundError(f"artifact {run_id} not found in project {project_id}")
    return итог


def delete_artifact(user_id: int | str, project_id: str, run_id: str) -> bool:
    """Удалить. `False` — такого артефакта не было."""
    return get_artifact_store().delete(
        _safe_id(str(user_id)), _safe_id(project_id), _safe_id(run_id))
