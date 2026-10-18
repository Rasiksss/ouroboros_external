"""Оригинал документа: файл каталога ouroboros или объект хранилища службы (ADR-0012).

Документ, принятый ручкой загрузки, хранит `catalog_file_id`; документ, принятый через web-поиск
(`document_acquisition`), — ключ объекта в `file_path`. Где лежат байты, решает запись документа, а не догадка
по виду строки.
"""

from __future__ import annotations

import logging
import tempfile
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path

import httpx

from finengine.assistant.ouro_client import OuroUnavailable
from finengine.assistant.ouro_files import FileStream, catalog
from finengine.models.document import DocumentMeta
from finengine.store.blobs import document_file

log = logging.getLogger(__name__)

# Паузы между попытками чтения оригинала для фонового разбора: переждать перезапуск ouroboros. Ошибку
# фонового разбора пользователь не видит, а без повтора короткий сбой оставил бы документ без текста до
# следующего хода (ход заново просит разбор — `POST /documents/{id}/parse`).
PARSE_RETRY_PAUSES_SEC: tuple[float, ...] = (5.0, 20.0, 60.0)


def original_stream(meta: DocumentMeta) -> FileStream | None:
    """Поток оригинала из каталога; `None` — файла там нет. Только для документа с `catalog_file_id`."""
    return catalog().open(str(meta.owner_id), meta.catalog_file_id)


@contextmanager
def document_original(meta: DocumentMeta, *, pauses: Sequence[float] = ()) -> Iterator[Path]:
    """Путь к байтам оригинала на время разбора; временная копия убирается всегда, в том числе при падении.

    Парсерам нужен путь, а не поток (`store/blobs.py`: `materialize`/`release`); из каталога файл ложится во
    временный каталог целиком. `FileNotFoundError` — файла нет ни в каталоге, ни в хранилище;
    `OuroUnavailable` — каталог не ответил и после попыток с паузами `pauses`.
    """
    if not meta.catalog_file_id:
        with document_file(meta.file_path) as path:
            yield path
        return
    with tempfile.TemporaryDirectory(prefix="original-") as directory:
        path = Path(directory) / f"original{Path(meta.filename).suffix}"
        _download(meta, path, pauses)
        yield path


def _download(meta: DocumentMeta, path: Path, pauses: Sequence[float]) -> None:
    for pause in (*pauses, None):
        try:
            _download_once(meta, path)
            return
        except OuroUnavailable as failure:
            if pause is None:
                raise
            log.warning("оригинал %s: каталог недоступен (%s), повтор через %.0f с", meta.doc_id, failure, pause)
            time.sleep(pause)


def _download_once(meta: DocumentMeta, path: Path) -> None:
    stream = original_stream(meta)
    if stream is None:
        raise FileNotFoundError(meta.catalog_file_id)
    try:
        with path.open("wb") as target:
            for chunk in stream.chunks():
                target.write(chunk)
    except httpx.HTTPError as failure:  # обрыв посреди потока — тот же сбой каталога
        raise OuroUnavailable(type(failure).__name__) from failure
    finally:
        stream.close()
