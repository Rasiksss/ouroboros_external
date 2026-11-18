"""Перенос документов: реестр из пикла в базу, файлы — в объектное хранилище.

Запускать там, где лежат и код, и том с данными, — в контейнере бэкенда:

    docker compose exec backend python -m finengine.store.migrate_documents --dry-run
    docker compose exec backend python -m finengine.store.migrate_documents

ДВА ПРЕДМЕТА В ОДНОМ ПРОХОДЕ, И ЭТО НАМЕРЕННО. Реестр помнит `file_path` —
путь к файлу на томе. Перенеси реестр без файлов, и записи будут указывать в
никуда; перенеси файлы без реестра, и объекты станут безымянными. Порядок
внутри одного документа: сперва объект, потом запись о нём. Упадём посередине —
в хранилище окажется лишний объект, а не запись без файла; лишнее безобиднее
битой ссылки.

ДОПОЛНЯЕМ, А НЕ ПЕРЕЗАПИСЫВАЕМ. Скрипт запустят минимум дважды (сухой прогон и
настоящий), а чаще трижды — после того, как на базе уже поработали. Документ,
который в базе есть, не трогаем; объект, который в хранилище есть, не заливаем
заново. Файлы на томе остаются: они и есть страховка на время переезда.

⚠️ `file_path` В ЗАПИСИ ПЕРЕПИСЫВАЕТСЯ НА КЛЮЧ ОБЪЕКТА. Иначе после переезда
код продолжил бы открывать файл напрямую и правило 3.1 осталось бы нарушенным —
переезд был бы только на бумаге. Прежний путь сохраняется в `meta_card` под
`legacy_file_path`: если что-то пойдёт не так, видно, откуда пришли.
"""
from __future__ import annotations

import argparse
import logging
from pathlib import Path

from finengine.config import settings
from finengine.models.document import DocumentMeta
from finengine.store.blobs import BlobStore, document_key, get_blob_store
from finengine.store.documents import SqlDocumentStore

log = logging.getLogger(__name__)


class ПиклРеестр:
    """Читатель СТАРОГО файлового реестра — только для этого переезда.

    🔴 ЖИВЁТ ЗДЕСЬ, А НЕ В БОЕВОМ ПУТИ, И ЭТО РЕШЕНИЕ. Боевой класс поверх
    `meta.pkl` снесён 26.08.2026 вместе с остальными файловыми запасными
    путями: пока он был, три потока извлечения поднимали его напрямую и читали
    пустой файл вместо базы. Но у прода реестр всё ещё в пикле, и дорогу в базу
    отнимать нельзя — поэтому читатель остался, отвязанный от боевого пути и
    только на чтение.

    Каталог зовётся `index` по эпохе FAISS: рядом лежал векторный индекс.
    Индекса нет с 15.08.2026, имя каталога — последнее, что от него осталось.
    """

    def __init__(self, каталог: Path | None = None) -> None:
        self._файл = (каталог or settings.data_dir / "index") / "meta.pkl"

    def существует(self) -> bool:
        return self._файл.is_file()

    def list_documents(self) -> list[DocumentMeta]:
        if not self._файл.is_file():
            return []
        import pickle  # noqa: PLC0415 — только этот переезд, боевому пути не нужен

        with open(self._файл, "rb") as f:
            return list((pickle.load(f).get("docs") or {}).values())

# Префикс ключей объектов. Один на все документы: владелец лежит в реестре, а
# не в пути, — иначе смена владельца требовала бы перекладывать файл.
ПРЕФИКС = "documents/"


def _ключ(meta: DocumentMeta) -> str:
    """Ключ объекта по документу. Формируется ОДНОЙ функцией на весь код.

    Своя формула здесь означала бы, что новые загрузки лягут мимо перенесённых,
    и обнаружилось бы это только глазами.
    """
    имя = Path(meta.file_path or meta.filename or meta.doc_id).name
    return document_key(meta.doc_id, имя)


def _перевести(meta: DocumentMeta, ключ: str) -> DocumentMeta:
    """Старая запись → сегодняшняя модель, с ключом объекта вместо пути.

    🔴 ПОЧЕМУ ЧЕРЕЗ СЛОВАРЬ, А НЕ `model_copy` (поймано живым переездом 25.08).
    В пикле лежат объекты, записанные ДО появления полей — `meta_card` завели
    позже. Питон восстанавливает такой объект БЕЗ этого атрибута: обращение к
    нему падает с `AttributeError`, хотя в сегодняшней модели у поля есть
    значение по умолчанию.

    Это ровно тот дефект, ради ухода от которого переезд и делается: пикл
    разбирается только тем кодом, который его писал. Показательно, что споткнулся
    об него сам переезд.

    Поэтому: собираем словарь ЗАЩИЩЁННО (`getattr` с умолчанием), отдаём его
    модели и получаем запись с сегодняшним набором полей.
    """
    поля = {}
    for имя in DocumentMeta.model_fields:
        значение = getattr(meta, имя, None)
        if значение is not None:
            поля[имя] = значение
    if ключ:
        карта = поля.get("meta_card") or {}
        поля["meta_card"] = {**карта, "legacy_file_path": поля.get("file_path", "")}
        поля["file_path"] = ключ
    return DocumentMeta(**поля)


def migrate(source: ПиклРеестр, target: SqlDocumentStore, blobs: BlobStore,
            dry_run: bool = False) -> dict:
    """Перенести реестр и файлы. Возвращает счётчики; ничего не удаляет."""
    отчёт = {"docs_in_file": 0, "docs_migrated": 0, "docs_already": 0,
             "blobs_uploaded": 0, "blobs_already": 0, "files_missing": 0}

    уже = {d.doc_id for d in target.list_documents()}
    for meta in source.list_documents():
        отчёт["docs_in_file"] += 1
        if meta.doc_id in уже:
            отчёт["docs_already"] += 1
            continue

        ключ = _ключ(meta)
        путь = Path(meta.file_path) if meta.file_path else None
        # Сперва объект, потом запись о нём — см. шапку.
        if путь and путь.is_file():
            if blobs.exists(ключ):
                отчёт["blobs_already"] += 1
            else:
                отчёт["blobs_uploaded"] += 1
                if not dry_run:
                    with open(путь, "rb") as f:
                        blobs.put(ключ, f)
        else:
            # Запись есть, файла нет. Это не повод бросать документ: метаданные
            # нужны интерфейсу, а отсутствие файла и так видно по пустому ключу.
            отчёт["files_missing"] += 1
            ключ = ""

        отчёт["docs_migrated"] += 1
        if dry_run:
            continue
        target.add_document(_перевести(meta, ключ), [])

    return отчёт


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Перенос документов: реестр в базу, файлы в объектное хранилище")
    parser.add_argument("--dry-run", action="store_true",
                        help="только посчитать, ничего не писать")
    args = parser.parse_args(argv)

    цель = (settings.documents_database_url or "").strip()
    if not цель:
        # Адрес не печатаем ни в каком виде — в нём пароль.
        print("База не настроена: пуст FINENGINE_DOCUMENTS_DATABASE_URL. Переносить некуда.")
        return 2

    источник = ПиклРеестр()
    if not источник.существует():
        print("Пиклового реестра нет — переносить нечего.")
        return 0
    blobs = get_blob_store()
    if hasattr(blobs, "ensure_bucket"):
        blobs.ensure_bucket()

    о = migrate(источник, SqlDocumentStore(цель), blobs, dry_run=args.dry_run)
    print(
        ("СУХОЙ ПРОГОН — ничего не записано\n" if args.dry_run else "")
        + f"документов в пикле:   {о['docs_in_file']}\n"
        + f"перенесено записей:   {о['docs_migrated']}\n"
        + f"уже было в базе:      {о['docs_already']}\n"
        + f"залито объектов:      {о['blobs_uploaded']}\n"
        + f"уже было в хранилище: {о['blobs_already']}\n"
        + f"записей без файла:    {о['files_missing']}\n"
        + "исходный пикл и файлы оставлены на месте"
    )
    return 0


if __name__ == "__main__":  # pragma: no cover — точка входа CLI
    raise SystemExit(main())
