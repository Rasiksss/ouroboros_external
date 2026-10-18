"""Служба ↔ каталог файлов ouroboros: C6.1–C6.4 на настоящем ouroboros профиля `contract`.

Клиент — настоящий `CatalogFiles` службы, каталог — настоящий (проверка содержимого, владельцы,
потоковая отдача). Тот же набор на подставном каталоге —
`finengine/tests/assistant/test_catalog_files.py`.
"""

from __future__ import annotations

import hashlib
import io
import uuid
from collections.abc import Callable

import pytest
from finengine.assistant.ouro_files import CatalogFiles, FileRefused, StoredFile

from stand import ContractStand

pytestmark = pytest.mark.contract


def pdf_bytes() -> bytes:
    import pymupdf

    document = pymupdf.open()  # type: ignore[no-untyped-call]
    document.new_page().insert_text((72, 72), "contract report")
    try:
        return bytes(document.tobytes())  # type: ignore[no-untyped-call]
    finally:
        document.close()  # type: ignore[no-untyped-call]


def xlsx_bytes() -> bytes:
    from openpyxl import Workbook

    book, buffer = Workbook(), io.BytesIO()
    book.active.append(["показатель", "значение"])
    book.save(buffer)
    return buffer.getvalue()


SAMPLES: dict[str, Callable[[], bytes]] = {
    "отчёт.pdf": pdf_bytes,
    "баланс.xlsx": xlsx_bytes,
    "данные.csv": lambda: b"a,b\n1,2\n",
    "заметки.txt": lambda: "текст".encode(),
}


@pytest.fixture
def files(stand: ContractStand) -> CatalogFiles:
    return CatalogFiles(stand.ouro_http_url, timeout_sec=120.0)


@pytest.fixture
def owner() -> str:
    """Свой владелец на тест: каталог общий на прогон, чужие файлы не мешают."""
    return f"c6-{uuid.uuid4().hex[:10]}"


def stored(result: StoredFile | FileRefused) -> StoredFile:
    assert isinstance(result, StoredFile), result
    return result


@pytest.mark.parametrize("name", list(SAMPLES), ids=["pdf", "xlsx", "csv", "txt"])
def test_c6_1_a_file_comes_back_from_the_catalog_with_the_same_hash(
    files: CatalogFiles, owner: str, name: str
) -> None:
    """C6.1: круговая поездка — загрузить → прочитать → sha256 совпадает; формат из списка каталога
    проходит (C6.3, вторая половина)."""
    content = SAMPLES[name]()

    result = stored(files.upload(owner, name, content))

    back = files.read(owner, result.file_id)
    assert (
        back is not None
        and hashlib.sha256(back).hexdigest() == result.sha256 == hashlib.sha256(content).hexdigest()
    )


def test_c6_1_the_file_is_streamed_with_the_media_type_of_its_format(
    files: CatalogFiles, owner: str
) -> None:
    result = stored(files.upload(owner, "отчёт.pdf", pdf_bytes()))

    stream = files.open(owner, result.file_id)

    assert stream is not None
    try:
        assert stream.media_type == "application/pdf"
        assert hashlib.sha256(b"".join(stream.chunks())).hexdigest() == result.sha256
    finally:
        stream.close()


def test_c6_2_another_owner_gets_nothing_and_cannot_delete(files: CatalogFiles, owner: str) -> None:
    result = stored(files.upload(owner, "заметки.txt", b"mine"))
    other = owner + "-other"

    assert files.read(other, result.file_id) is None
    assert files.open(other, result.file_id) is None
    assert files.delete(other, result.file_id) is False
    assert files.read(owner, result.file_id) == b"mine"


def test_c6_3_a_format_the_catalog_does_not_take_is_a_refusal(
    files: CatalogFiles, owner: str
) -> None:
    refused = files.upload(owner, "архив.zip", b"PK\x03\x04" + b"0" * 64)

    assert isinstance(refused, FileRefused) and "формат" in refused.text


def test_c6_3_a_file_that_is_not_what_its_name_says_is_a_refusal(
    files: CatalogFiles, owner: str
) -> None:
    refused = files.upload(owner, "отчёт.pdf", b"this is not a pdf at all")

    assert isinstance(refused, FileRefused) and "повреждён" in refused.text


def test_c6_4_a_deleted_file_is_gone_for_its_owner(files: CatalogFiles, owner: str) -> None:
    result = stored(files.upload(owner, "заметки.txt", b"x"))

    assert files.delete(owner, result.file_id) is True

    assert files.read(owner, result.file_id) is None
    assert files.delete(owner, result.file_id) is True, (
        "повтор удаления своего файла каталог принимает (204)"
    )
