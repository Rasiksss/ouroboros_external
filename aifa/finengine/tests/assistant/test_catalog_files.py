"""Клиент каталога файлов ouroboros: загрузить, прочитать, удалить (CONTRACTS.md §6, ADR-0012, ADR-0026).

Каталог — внешняя граница: его подставной сервер говорит по настоящему HTTP (`tests/fake_catalog.py`), так что
`CatalogFiles` проходит заголовки, multipart и потоковое чтение. Тот же набор на настоящем ouroboros —
`aifa/tests/contract/test_catalog_files.py`.
"""
from __future__ import annotations

import hashlib

import pytest

from finengine.assistant.ouro_client import OuroUnavailable
from finengine.assistant.ouro_files import CatalogFiles, FileRefused, StoredFile
from tests.fake_catalog import FakeCatalog

REPORT = b"%PDF-1.4\n" + b"report body " * 200


@pytest.fixture
def fake():
    with FakeCatalog() as catalog:
        yield catalog


@pytest.fixture
def files(fake: FakeCatalog) -> CatalogFiles:
    return CatalogFiles(fake.url, timeout_sec=5.0)


def stored(result: StoredFile | FileRefused) -> StoredFile:
    assert isinstance(result, StoredFile), result
    return result


def test_a_file_comes_back_from_the_catalog_with_the_hash_it_went_with(files, fake):
    """C6.1: загрузить → прочитать → sha256 совпадает; каталогу назван владелец."""
    result = stored(files.upload("7", "отчёт.pdf", REPORT))

    assert result.sha256 == hashlib.sha256(REPORT).hexdigest()
    assert result.size == len(REPORT)
    read = files.read("7", result.file_id)
    assert read is not None and hashlib.sha256(read).hexdigest() == result.sha256
    assert {owner for _verb, _path, owner in fake.requests} == {"7"}
    assert fake.files[result.file_id].name == "отчёт.pdf"


def test_another_owner_does_not_get_the_file(files, fake):
    """C6.2: чужой владелец — «нет файла» значением, и чужой файл остаётся цел."""
    result = stored(files.upload("7", "a.txt", b"mine"))

    assert files.read("8", result.file_id) is None
    assert files.open("8", result.file_id) is None
    assert files.delete("8", result.file_id) is False
    assert result.file_id in fake.files


def test_a_file_is_streamed_with_its_media_type(files):
    result = stored(files.upload("7", "отчёт.pdf", REPORT))

    stream = files.open("7", result.file_id)

    assert stream is not None
    try:
        assert stream.media_type == "application/pdf"
        assert b"".join(stream.chunks()) == REPORT
    finally:
        stream.close()


def test_deleting_removes_the_file_and_repeating_it_is_harmless(files, fake):
    """C6.4 на уровне клиента: удалено — True; повтор владельца каталог принимает так же (идемпотентно)."""
    result = stored(files.upload("7", "a.txt", b"x"))

    assert files.delete("7", result.file_id) is True
    assert fake.files == {}
    assert files.read("7", result.file_id) is None
    assert files.delete("7", result.file_id) is True


def test_a_format_the_catalog_does_not_take_is_a_refusal_with_a_text(files):
    """Отказ формата — значение с понятным текстом, а не исключение (PRINCIPLES.md §В, ADR-0026)."""
    result = files.upload("7", "архив.zip", b"PK\x03\x04")

    assert isinstance(result, FileRefused)
    assert "формат" in result.text and "архив.zip" in result.text


@pytest.mark.parametrize(
    ("status", "code", "word"),
    [(413, "attachment_too_large", "большой"), (422, "invalid_attachment", "повреждён")],
)
def test_other_refusals_of_the_catalog_say_what_is_wrong_with_the_file(files, fake, status, code, word):
    fake.refuse_with = (status, code)

    result = files.upload("7", "a.pdf", REPORT)

    assert isinstance(result, FileRefused) and word in result.text


def test_a_catalog_that_is_down_is_an_exception_not_a_refusal(files, fake):
    """Сбой системы (не отказ по файлу) поднимается: пользователю это «повторите позже», а не «файл плох»."""
    result = stored(files.upload("7", "a.txt", b"x"))
    fake.down = True

    with pytest.raises(OuroUnavailable):
        files.upload("7", "b.txt", b"y")
    with pytest.raises(OuroUnavailable):
        files.read("7", result.file_id)
    with pytest.raises(OuroUnavailable):
        files.delete("7", result.file_id)


def test_an_unreachable_catalog_is_an_exception():
    nobody = CatalogFiles("http://127.0.0.1:9", timeout_sec=1.0)

    with pytest.raises(OuroUnavailable):
        nobody.upload("7", "a.txt", b"x")
