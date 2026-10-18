"""Оригинал документа: файл каталога ouroboros или объект хранилища службы (ADR-0012).

Документ, принятый ручкой загрузки, хранит `catalog_file_id`; документ, принятый через web-поиск
(`document_acquisition`), — ключ объекта в `file_path`. Где лежат байты, решает запись документа, а не догадка
по виду строки.
"""

from __future__ import annotations

import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from finengine.assistant.ouro_files import FileStream, catalog
from finengine.models.document import DocumentMeta
from finengine.store.blobs import document_file


def original_stream(meta: DocumentMeta) -> FileStream | None:
    """Поток оригинала из каталога; `None` — файла там нет. Только для документа с `catalog_file_id`."""
    return catalog().open(str(meta.owner_id), meta.catalog_file_id)


@contextmanager
def document_original(meta: DocumentMeta) -> Iterator[Path]:
    """Путь к байтам оригинала на время разбора; временная копия убирается всегда, в том числе при падении.

    Парсерам нужен путь, а не поток (`store/blobs.py`: `materialize`/`release`); из каталога файл ложится во
    временный каталог целиком. `FileNotFoundError` — файла нет ни в каталоге, ни в хранилище.
    """
    if not meta.catalog_file_id:
        with document_file(meta.file_path) as path:
            yield path
        return
    stream = original_stream(meta)
    if stream is None:
        raise FileNotFoundError(meta.catalog_file_id)
    with tempfile.TemporaryDirectory(prefix="original-") as directory:
        path = Path(directory) / f"original{Path(meta.filename).suffix}"
        try:
            with path.open("wb") as target:
                for chunk in stream.chunks():
                    target.write(chunk)
        finally:
            stream.close()
        yield path
