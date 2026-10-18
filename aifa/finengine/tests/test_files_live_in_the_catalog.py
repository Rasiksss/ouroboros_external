"""Оригиналы документов, источники и файлы-результаты лежат в каталоге ouroboros (ADR-0012, ADR-0026).

Ручки службы — настоящие (`TestClient` на настоящем приложении); каталог — внешняя граница, его подставной
сервер говорит по HTTP (`tests/fake_catalog.py`). Служба хранит соответствие своего идентификатора файлу
каталога и `sha256`, распознанный текст и карточки — у себя. Тот же путь на настоящем ouroboros —
`aifa/tests/contract/test_files_end_to_end.py`.

Закрывают: C1.4 и C6.3 (XLS/DOC отклоняет служба до обращения к каталогу), C6.1 (круговая поездка через
ручки), C6.4 (удаление документа удаляет файл каталога).
"""
from __future__ import annotations

import hashlib

import pytest
from fastapi.testclient import TestClient

import finengine.api.main as m
from finengine.api.main import get_current_user
from finengine.auth.db import User
from finengine.ingestion.document_store import get_store
from finengine.skills import result_events, sources
from finengine.store.blobs import document_key, document_text, get_blob_store

OWNER = 7
MIN_PDF = b"%PDF-1.4\n" + b"0" * 2000
OOXML = b"PK\x03\x04" + b"0" * 200
REFUSAL = "Сохраните файл как XLSX/DOCX и загрузите снова"


@pytest.fixture()
def client(tmp_path, monkeypatch, catalog):
    from finengine.config import settings

    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr(settings, "s3_endpoint", "")
    monkeypatch.setattr(m.settings, "docs_dir", tmp_path)
    from finengine.store import blobs

    blobs._cached = None
    m.app.dependency_overrides[get_current_user] = lambda: User(id=OWNER, username="t")
    try:
        yield TestClient(m.app)
    finally:
        m.app.dependency_overrides.pop(get_current_user, None)
        blobs._cached = None


def upload_document(client, name="отчёт.pdf", blob=MIN_PDF, defer="1"):
    return client.post(
        "/documents/upload", files={"file": (name, blob, "application/octet-stream")}, data={"defer_parse": defer}
    )


def upload_source(client, name="план.csv", blob=b"a;b\n1;2\n"):
    return client.post("/projects/p1/sources/upload", files={"file": (name, blob, "text/csv")})


@pytest.mark.usefixtures("бд_хранилищ")
class TestDocuments:
    def test_an_uploaded_document_lands_in_the_catalog_and_the_service_keeps_the_pair(self, client, catalog):
        got = upload_document(client)

        assert got.status_code == 200, got.text
        doc_id = got.json()["doc_id"]
        (file_id, blob), = catalog.files.items()
        assert blob.owner == str(OWNER) and blob.name == "отчёт.pdf" and blob.content == MIN_PDF
        meta = get_store().get_document(doc_id)
        assert meta.catalog_file_id == file_id
        assert meta.sha256 == hashlib.sha256(MIN_PDF).hexdigest()
        assert not get_blob_store().exists(document_key(doc_id, "отчёт.pdf")), (
            "оригинал остался и в хранилище службы: два источника правды о байтах"
        )

    def test_the_original_is_served_from_the_catalog(self, client, catalog):
        doc_id = upload_document(client).json()["doc_id"]

        got = client.get(f"/documents/{doc_id}/file")

        assert got.status_code == 200
        assert got.content == MIN_PDF
        assert got.headers["content-type"] == "application/pdf"
        assert got.headers["content-disposition"].startswith("inline")
        assert catalog.asked("GET")[-1].endswith("/content")

    def test_deleting_a_document_deletes_its_file_in_the_catalog(self, client, catalog):
        """C6.4."""
        doc_id = upload_document(client).json()["doc_id"]

        assert client.delete(f"/documents/{doc_id}").status_code == 200

        assert catalog.files == {}
        assert client.get(f"/documents/{doc_id}/file").status_code == 404

    def test_a_catalog_that_cannot_delete_keeps_the_document_so_the_delete_can_be_repeated(self, client, catalog):
        doc_id = upload_document(client).json()["doc_id"]
        catalog.down = True

        assert client.delete(f"/documents/{doc_id}").status_code == 503

        catalog.down = False
        assert get_store().get_document(doc_id) is not None
        assert client.delete(f"/documents/{doc_id}").status_code == 200
        assert catalog.files == {}

    @pytest.mark.parametrize("name", ["баланс.xls", "баланс.XLS", "письмо.doc", "письмо.DOC"])
    def test_xls_and_doc_are_refused_before_the_catalog_is_asked(self, client, catalog, name):
        """C1.4 + C6.3: прежний код ошибки загрузки (400), понятный текст, ни одного обращения к каталогу."""
        got = upload_document(client, name, b"\xd0\xcf\x11\xe0" + b"0" * 100)

        assert got.status_code == 400, got.text
        assert REFUSAL in got.json()["detail"] and name in got.json()["detail"]
        assert catalog.requests == []
        assert get_store().list_documents(owner_id=OWNER) == []

    @pytest.mark.parametrize(
        ("name", "blob"),
        [("отчёт.pdf", MIN_PDF), ("отчёт.docx", OOXML), ("баланс.xlsx", OOXML), ("заметки.txt", b"text")],
        ids=["pdf", "docx", "xlsx", "txt"],
    )
    def test_formats_from_the_catalog_list_pass(self, client, catalog, name, blob):
        """C6.3, вторая половина: форматы из списка каталога проходят."""
        got = upload_document(client, name, blob)

        assert got.status_code == 200, got.text
        assert [b.name for b in catalog.files.values()] == [name]

    def test_a_format_the_catalog_refuses_is_a_400_with_a_text_and_registers_nothing(self, client, catalog):
        got = upload_document(client, "архив.zip", b"PK\x03\x04" + b"0" * 100)

        assert got.status_code == 400, got.text
        assert "формат" in got.json()["detail"] and "архив.zip" in got.json()["detail"]
        assert get_store().list_documents(owner_id=OWNER) == []

    def test_a_catalog_that_is_down_stops_the_upload_and_registers_nothing(self, client, catalog):
        catalog.down = True

        got = upload_document(client)

        assert got.status_code == 503, got.text
        assert "Повторите" in got.json()["detail"]
        assert get_store().list_documents(owner_id=OWNER) == []

    def test_parsing_reads_the_original_through_the_catalog(self, client, catalog, monkeypatch):
        """Распознавание берёт байты из каталога; текст остаётся в хранилище службы."""
        async def no_card(_text):
            return {}

        monkeypatch.setattr("finengine.skills.meta_card.build", no_card)

        doc_id = upload_document(client, "заметки.txt", "текст отчёта".encode(), defer="").json()["doc_id"]

        assert document_text(doc_id) == "текст отчёта"
        assert any(path.endswith("/content") for path in catalog.asked("GET")), "текст взят не из каталога"

    def test_the_document_of_another_user_is_not_served(self, client, catalog):
        doc_id = upload_document(client).json()["doc_id"]
        m.app.dependency_overrides[get_current_user] = lambda: User(id=OWNER + 1, username="other")

        assert client.get(f"/documents/{doc_id}/file").status_code == 404
        assert client.delete(f"/documents/{doc_id}").status_code == 404
        assert len(catalog.files) == 1


class TestSources:
    def test_an_uploaded_source_lands_in_the_catalog_and_its_text_stays_in_the_service(self, client, catalog):
        got = upload_source(client)

        assert got.status_code == 200, got.text
        card = got.json()
        (file_id, blob), = catalog.files.items()
        assert blob.owner == str(OWNER) and blob.content == b"a;b\n1;2\n"
        assert sources.read_source_text(OWNER, "p1", card["file_id"]) == "a;b\n1;2\n"
        assert card["catalog_file_id"] == file_id
        assert card["sha256"] == hashlib.sha256(b"a;b\n1;2\n").hexdigest()
        store, key = get_blob_store(), f"users/7/projects/p1/sources/{card['file_id']}"
        assert store.exists(f"{key}.txt") and store.exists(f"{key}.meta.json")
        assert not store.exists(f"{key}.csv"), "оригинал остался и в хранилище службы"

    def test_the_source_file_is_served_from_the_catalog(self, client, catalog):
        card = upload_source(client).json()

        got = client.get(f"/projects/p1/sources/{card['file_id']}/file")

        assert got.status_code == 200 and got.content == b"a;b\n1;2\n"
        assert got.headers["content-type"].startswith("text/csv")
        assert "filename*=UTF-8''" in got.headers["content-disposition"]

    def test_deleting_a_source_deletes_its_file_in_the_catalog(self, client, catalog):
        card = upload_source(client).json()

        assert client.delete(f"/projects/p1/sources/{card['file_id']}").status_code == 200

        assert catalog.files == {}
        store, key = get_blob_store(), f"users/7/projects/p1/sources/{card['file_id']}"
        assert not store.exists(f"{key}.txt") and not store.exists(f"{key}.meta.json")
        assert sources.get_source_meta(OWNER, "p1", card["file_id"]) is None

    @pytest.mark.parametrize("name", ["баланс.xls", "письмо.doc"])
    def test_xls_and_doc_are_refused_before_the_catalog_is_asked_for_sources_too(self, client, catalog, name):
        got = upload_source(client, name, b"\xd0\xcf\x11\xe0")

        assert got.status_code == 400 and REFUSAL in got.json()["detail"]
        assert catalog.requests == []

    def test_a_forbidden_extension_is_still_refused_by_the_service_list(self, client, catalog):
        got = upload_source(client, "вирус.exe", b"x")

        assert got.status_code == 400 and "not allowed" in got.json()["detail"]
        assert catalog.requests == []

    def test_a_generated_file_is_in_the_catalog_and_the_screen_id_is_the_service_id(self, client, catalog):
        """Кнопка файла в чате: `file_id` экрана — идентификатор службы, за ним файл каталога."""
        meta, location = sources.save_generated_bytes(OWNER, "p1", "итоги.xlsx", OOXML, "application/octet-stream")

        assert [b.name for b in catalog.files.values()] == ["итоги.xlsx"]
        assert result_events.source_file_id(location) == meta.file_id
        got = client.get(f"/projects/p1/sources/{meta.file_id}/file")
        assert got.status_code == 200 and got.content == OOXML

    def test_a_source_of_another_project_or_user_is_not_served(self, client, catalog):
        card = upload_source(client).json()

        assert client.get(f"/projects/other/sources/{card['file_id']}/file").status_code == 404
        m.app.dependency_overrides[get_current_user] = lambda: User(id=OWNER + 1, username="other")
        assert client.get(f"/projects/p1/sources/{card['file_id']}/file").status_code == 404
