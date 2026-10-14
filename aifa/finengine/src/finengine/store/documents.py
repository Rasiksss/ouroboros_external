"""Реестр документов в базе. Строка на документ, владелец — колонкой.

ЗАЧЕМ. До этого реестр жил в `data/index/meta.pkl` — питоновском пикле, куда
пишется весь словарь целиком. Три нарушения `docs/rules/architecture.md`, все
🔴 MUST:

  * 2.1 «Postgres — единственная БД… признак регрессии: файловые БД»;
  * 3.1 «Нет состояния на файловой системе контейнера»;
  * 3.2 «Stateless-реплики; in-process синглтоны… разъезжающиеся на `>1`
    реплике — баги». В `ingestion/document_store.py` прямо: «ОДИН экземпляр
    хранилища на процесс», а в `api/main.py` рядом с загрузкой стоит замок с
    подписью «add_document() is not thread-safe». Сегодня не стреляет ТОЛЬКО
    потому, что бэкенд запущен с `--workers 1`.

Плюс к правилам — три свойства, которых у пикла не было в принципе:

  * ЗАПИСЬ НЕ ЦЕЛИКОМ. Пикл читается и пишется всем словарём: два одновременных
    сохранения — и одно теряется. Здесь строка на документ и транзакция;
  * ВЛАДЕЛЕЦ — КОЛОНКА, а не поле внутри слепка. Проверка «чей документ»
    перестаёт зависеть от файла и становится условием запроса;
  * ФОРМАТ НЕ ПРИВЯЗАН К КЛАССАМ ПИТОНА. Пикл разбирается только тем кодом,
    который его писал: переименуй поле в `DocumentMeta` — старый файл не
    прочитается. Здесь метаданные лежат JSON-ом, и его прочтёт кто угодно.

⚠️ КУСКИ (`chunks`) ПЕРЕНЕСЕНЫ, ХОТЯ БОЕВОЙ ПУТЬ ИХ НЕ ДЕЛАЕТ. Полноконтекстное
извлечение зовёт `add_document(meta, [])` всегда; куски кладёт только демо-роут
(`demo/routes.py`). Выкинуть их из интерфейса значило бы уронить демо ради
экономии одной таблицы — и обнаружилось бы это у того, кто показывает продукт.

ПЕРЕКЛЮЧАТЕЛЬ. Пустой `FINENGINE_DOCUMENTS_DATABASE_URL` (по умолчанию) — пикл,
как было. Непустой — база. Момент переезда выбирает владелец; код ничего не
включает сам, как у диалогов, учёток, прогонов и объектов.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from sqlalchemy import (
    JSON, Column, Integer, MetaData, String, Table, delete, select,
)
from sqlalchemy.dialects.postgresql import insert as pg_insert

from finengine.models.document import Chunk, DocumentMeta, ExtractedItem
from finengine.store.engine import движок

log = logging.getLogger(__name__)

_metadata = MetaData()

DOCUMENT = Table(
    "document", _metadata,
    Column("doc_id", String(64), primary_key=True),
    # Владелец — КОЛОНКА и индекс: по нему фильтруются списки и проверяется
    # доступ. Внутри `meta` он тоже есть, но условие запроса должно опираться
    # на колонку, а не на разбор JSON.
    Column("owner_id", Integer, nullable=False, default=0, index=True),
    Column("meta", JSON, nullable=False),
)

DOCUMENT_CHUNK = Table(
    "document_chunk", _metadata,
    Column("chunk_id", String(64), primary_key=True),
    Column("doc_id", String(64), nullable=False, index=True),
    Column("payload", JSON, nullable=False),
)


def _to_meta(значение: Any) -> DocumentMeta:
    """JSON из базы → модель. SQLite отдаёт строку, Postgres — уже словарь."""
    if isinstance(значение, str):
        значение = json.loads(значение)
    return DocumentMeta(**значение)


class SqlDocumentStore:
    """Строка на документ. Интерфейс совпадает с файловым — сознательно.

    Совпадение до последнего метода нужно затем, чтобы шесть потребителей
    (`api/main.py`, `station_turn.py`, `internal_docs.py`, `llm/tools.py`,
    `orchestrator/agent.py`, `demo/routes.py`) не знали, что под ними
    поменялось. Расхождение поведения здесь — дефект, а не особенность.
    """

    def __init__(self, url: str):
        # echo=False намеренно: в строке подключения пароль, а с echo=True
        # SQLAlchemy печатает её в лог.
        self._engine = движок(url, pool_pre_ping=True, future=True, echo=False)
        # Alembic в репозитории нет — таблицы заводятся при первом обращении.
        _metadata.create_all(self._engine)

    def _upsert(self, conn, таблица, ключ: str, значения: dict) -> None:
        """Вставить или обновить. Диалект решает, каким способом.

        У Postgres есть `ON CONFLICT`, у SQLite в старых версиях — нет; путь
        «посмотреть и решить» работает на обоих, а гонку закрывает транзакция.
        """
        if conn.dialect.name == "postgresql":
            стлб = list(таблица.primary_key.columns)[0]
            conn.execute(
                pg_insert(таблица).values(**значения)
                .on_conflict_do_update(index_elements=[стлб],
                                       set_={k: v for k, v in значения.items()
                                             if k != стлб.name})
            )
            return
        стлб = list(таблица.primary_key.columns)[0]
        есть = conn.execute(select(стлб).where(стлб == ключ)).first()
        if есть:
            conn.execute(таблица.update().where(стлб == ключ).values(**значения))
        else:
            conn.execute(таблица.insert().values(**значения))

    def add_document(self, meta: DocumentMeta, chunks: list[Chunk]) -> None:
        """Зарегистрировать документ. Пустой список кусков — законный вызов."""
        with self._engine.begin() as conn:
            self._upsert(conn, DOCUMENT, meta.doc_id, {
                "doc_id": meta.doc_id, "owner_id": int(meta.owner_id or 0),
                "meta": json.loads(meta.model_dump_json()),
            })
            for c in chunks or ():
                self._upsert(conn, DOCUMENT_CHUNK, c.chunk_id, {
                    "chunk_id": c.chunk_id, "doc_id": c.doc_id,
                    "payload": json.loads(c.model_dump_json()),
                })

    def remove_document(self, doc_id: str) -> None:
        with self._engine.begin() as conn:
            conn.execute(delete(DOCUMENT_CHUNK).where(DOCUMENT_CHUNK.c.doc_id == doc_id))
            conn.execute(delete(DOCUMENT).where(DOCUMENT.c.doc_id == doc_id))

    def get_chunks_by_type(self, chunk_type: str, doc_id: str | None = None
                           ) -> list[ExtractedItem]:
        запрос = select(DOCUMENT_CHUNK.c.payload)
        if doc_id:
            запрос = запрос.where(DOCUMENT_CHUNK.c.doc_id == doc_id)
        итог: list[ExtractedItem] = []
        with self._engine.begin() as conn:
            for (сырьё,) in conn.execute(запрос):
                d = json.loads(сырьё) if isinstance(сырьё, str) else сырьё
                if d.get("chunk_type") != chunk_type:
                    continue
                итог.append(ExtractedItem(
                    content=d.get("content", ""), score=1.0,
                    chunk_type=d.get("chunk_type", ""),
                    metadata={**(d.get("metadata") or {}),
                              "chunk_id": d.get("chunk_id"), "doc_id": d.get("doc_id"),
                              "page": d.get("page"), "section": d.get("section"),
                              "speaker": d.get("speaker")},
                ))
        return итог

    def list_documents(self, owner_id: int | None = None) -> list[DocumentMeta]:
        """Реестр документов. `owner_id=None` — все (миграция/уборка/админ);
        иначе — только документы этого владельца, через колонку-индекс."""
        запрос = select(DOCUMENT.c.meta)
        if owner_id is not None:
            запрос = запрос.where(DOCUMENT.c.owner_id == owner_id)
        with self._engine.begin() as conn:
            return [_to_meta(m) for (m,) in conn.execute(запрос)]

    def get_document(self, doc_id: str) -> DocumentMeta | None:
        with self._engine.begin() as conn:
            строка = conn.execute(
                select(DOCUMENT.c.meta).where(DOCUMENT.c.doc_id == doc_id)).first()
        return _to_meta(строка[0]) if строка else None

    def update_document_meta(self, meta: DocumentMeta) -> None:
        """Заменить метаданные. Незарегистрированный doc_id — ничего не делаем.

        Условие «есть в реестре» сохранено от файловой версии, но здесь оно НЕ
        НЕСУЩЕЕ, и это честнее сказать, чем сделать вид: `UPDATE` по
        отсутствующей строке и так ничего не создаёт — свойство держится
        семантикой SQL. Проверка оставлена как заявление о намерении: если
        однажды кто-то заменит `update()` на upsert (как в `add_document`), она
        станет единственным, что отличает «дополнить» от «завести».

        ⚠️ Мутация «снять проверку» тест НЕ КРАСНИТ — сторож на это свойство
        есть, а вот этих трёх строк он не охраняет.
        """
        with self._engine.begin() as conn:
            есть = conn.execute(
                select(DOCUMENT.c.doc_id).where(DOCUMENT.c.doc_id == meta.doc_id)).first()
            if not есть:
                return
            conn.execute(DOCUMENT.update().where(DOCUMENT.c.doc_id == meta.doc_id).values(
                owner_id=int(meta.owner_id or 0),
                meta=json.loads(meta.model_dump_json())))

    @property
    def total_chunks(self) -> int:
        from sqlalchemy import func  # noqa: PLC0415

        with self._engine.begin() as conn:
            return int(conn.execute(select(func.count()).select_from(DOCUMENT_CHUNK)).scalar() or 0)

    @property
    def total_documents(self) -> int:
        from sqlalchemy import func  # noqa: PLC0415

        with self._engine.begin() as conn:
            return int(conn.execute(select(func.count()).select_from(DOCUMENT)).scalar() or 0)
