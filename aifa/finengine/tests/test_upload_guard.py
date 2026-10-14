"""Битый PDF отбивается на загрузке, а не через пять минут в OCR.

РЕАЛЬНЫЙ СЛУЧАЙ (01.08.2026). Пользователь загрузил указатель Git LFS вместо
самого файла — 132 байта текста «version https://git-lfs.github.com/spec/v1».
Система приняла это как документ; OCR упал на pymupdf («Failed to open file as
type pdf»), но ход об этом не узнал: он ждал появления распознанного текста и
через 5 минут сказал «PDF не удалось обработать — попробуйте загрузить ещё
раз». Совет отправлял человека грузить тот же мусор по кругу.

⚠️ ПЕРЕПИСАН 21.08.2026 (ревизия набора). До этого дня файл НЕ ЗВАЛ РУЧКУ: он
повторял её проверки своей копией кода («тот же порядок проверок, что в
ручке») и отдельным тестом искал в `main.py` подстроку `git-lfs.github.com`.
Такой набор зелен и на пустой ручке — копия в тесте всегда согласна сама с
собой, а подстрока живёт в комментарии. Проверено мутацией: снятие ворот
из `upload_document` целиком оставляло все пять тестов зелёными.

Теперь загрузка идёт через `POST /documents/upload` настоящим `TestClient`:
красное здесь означает, что мусор доедет до OCR.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import finengine.api.main as m
from finengine.api.main import get_current_user
from finengine.auth.db import User

# Файловых запасных путей у новостей, сценариев и реестра документов больше нет
# (снесены 26.08.2026) — файлу нужна настоящая база.
pytestmark = pytest.mark.usefixtures("бд_хранилищ")


MIN_PDF = b"%PDF-1.4\n" + b"0" * 2000
LFS_POINTER = (
    b"version https://git-lfs.github.com/spec/v1\n"
    b"oid sha256:c2cced9220328ec9f0f6a934b63a399718ef2a1c2f18b92a0e8df844c95ef47d\n"
    b"size 2819740\n"
)


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """Настоящее приложение; файлы уезжают в tmp_path, разбор отложен."""
    monkeypatch.setattr(m.settings, "docs_dir", tmp_path)
    m.app.dependency_overrides[get_current_user] = lambda: User(
        id=1, username="t", is_admin=True)
    try:
        yield TestClient(m.app)
    finally:
        m.app.dependency_overrides.pop(get_current_user, None)


def _upload(client, name: str, blob: bytes):
    # defer_parse=1 — фоновый OCR тут ни при чём: проверяются ВОРОТА до него.
    return client.post(
        "/documents/upload",
        files={"file": (name, blob, "application/pdf")},
        data={"defer_parse": "1"},
    )


def test_lfs_pointer_is_refused(client, tmp_path):
    got = _upload(client, "ИКС-5 2023.pdf", LFS_POINTER)
    assert got.status_code == 400, got.text[:300]
    detail = got.json()["detail"]
    assert "LFS" in detail, detail
    # Отказ обязан говорить, ЧТО делать: без этого человек грузит тот же мусор
    # по кругу — ровно то, чем кончился случай 01.08.
    assert "git lfs pull" in detail, detail
    # И мусор не остаётся на диске: иначе он подхватится следующим ходом.
    assert not list(tmp_path.glob("*.pdf")), "отвергнутый файл остался на диске"


def test_not_a_pdf_is_refused(client):
    got = _upload(client, "отчёт.pdf",
                  b"<html><body>404 Not Found</body></html>" + b" " * 2000)
    assert got.status_code == 400, got.text[:300]
    assert "%PDF-" in got.json()["detail"]


def test_truncated_pdf_is_refused(client):
    got = _upload(client, "обрыв.pdf", b"%PDF-1.4\n")
    assert got.status_code == 400, got.text[:300]
    assert "не целиком" in got.json()["detail"]


def test_unknown_document_type_is_refused_before_the_file_is_saved(client, tmp_path):
    got = client.post(
        "/documents/upload",
        files={"file": ("ok.pdf", MIN_PDF, "application/pdf")},
        data={"defer_parse": "1", "doc_type": "accounting_statements"},
    )

    assert got.status_code == 422, got.text[:300]
    assert "doc_type" in got.json()["detail"]
    assert not list(tmp_path.glob("*.pdf")), "невалидная мета не должна оставлять файл"


def test_real_pdf_passes(client, tmp_path):
    """Отрицательный контроль: ворота, отбивающие всё, — это не ворота.

    🔴 ПРОВЕРКА ПЕРЕЕХАЛА ВМЕСТЕ С ФАЙЛОМ (25.08.2026). Здесь стояло
    «нормальный PDF лёг НА ДИСК» — `tmp_path.glob("*ok.pdf")`. После перевода
    загрузок в объектное хранилище (architecture.md 3.1: «загрузки — в
    объектное хранилище… никогда на диск») файл на диске больше не остаётся:
    он пишется временно, ради ворот, которым нужно прочитать первые байты, и
    сразу уезжает объектом.

    Смысл теста прежний — «принятый файл где-то ЕСТЬ», — сменилось только где.
    """
    got = _upload(client, "ok.pdf", MIN_PDF)
    assert got.status_code == 200, got.text[:300]
    doc_id = got.json()["doc_id"]
    assert doc_id

    from finengine.store.blobs import document_key, get_blob_store
    хранилище = get_blob_store()
    ключ = document_key(doc_id, "ok.pdf")
    assert хранилище.exists(ключ), "принятый PDF не попал в объектное хранилище"
    assert хранилище.get(ключ) == MIN_PDF, "содержимое объекта не совпало с загруженным"
    assert not list(tmp_path.glob("*ok.pdf")), (
        "PDF остался лежать на диске — временный файл ворот не убран, и правило "
        "«никогда на диск» держится только на словах")
