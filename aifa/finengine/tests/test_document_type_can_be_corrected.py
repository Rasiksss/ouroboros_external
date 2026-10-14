"""Вид документа правится руками (#250, вторая часть).

ЗАЧЕМ. Вид проставляется при загрузке автоматически и промахивается: годовой
отчёт по МСФО приезжает «прочим», и дальше по нему выбирается не та ячейка
реестра. На экране стояла НАДПИСЬ — исправить было нечем.

⚠️ ПОЧЕМУ РУЧКА, А НЕ ВЫПАДАШКА НА ФРОНТЕ. Выпадашка без сохранения — обманка:
пользователь выбирает вид, перезагружает страницу и видит прежний. Сначала
место, где выбор ЖИВЁТ, потом控 control.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import finengine.api.main as m
from finengine.api.main import get_current_user
from finengine.auth.db import User
from finengine.models.document import CompanyType, DocType, DocumentMeta

# Файловых запасных путей у новостей, сценариев и реестра документов больше нет
# (снесены 26.08.2026) — файлу нужна настоящая база.
pytestmark = pytest.mark.usefixtures("бд_хранилищ")



@pytest.fixture()
def client(tmp_path, monkeypatch):
    m.app.dependency_overrides[get_current_user] = lambda: User(id=1, username="t", is_admin=True)
    try:
        yield TestClient(m.app)
    finally:
        m.app.dependency_overrides.pop(get_current_user, None)


def _add_doc() -> str:
    meta = DocumentMeta(
        filename="otp-2024.pdf", doc_type=DocType.OTHER,
        company_name="ОТП Банк", company_type=CompanyType.BANK, period="2024",
    )
    from finengine.ingestion.document_store import get_store

    get_store().add_document(meta, [])
    return meta.doc_id


def test_type_is_saved_and_survives_reread(client):
    doc_id = _add_doc()
    got = client.patch(f"/documents/{doc_id}", json={"doc_type": "ifrs_report"})
    assert got.status_code == 200, got.text[:200]
    assert got.json()["doc_type"] == "ifrs_report"
    # Сохранение, а не эхо: перечитываем из хранилища.
    assert m.get_store().get_document(doc_id).doc_type == DocType.IFRS_REPORT


def test_unknown_type_is_refused_with_the_allowed_list(client):
    """Отказ обязан НАЗЫВАТЬ допустимые виды: молчаливое 422 на фронте
    выглядит как «кнопка не работает» — этот класс уже стоил недели (#142)."""
    doc_id = _add_doc()
    got = client.patch(f"/documents/{doc_id}", json={"doc_type": "квартальный"})
    assert got.status_code == 422
    assert "ifrs_report" in got.text

def test_missing_document_is_404(client):
    got = client.patch("/documents/нет-такого", json={"doc_type": "ifrs_report"})
    assert got.status_code == 404


def test_patch_does_not_touch_company_or_period(client):
    """Правим ТОЛЬКО вид: имя компании и период пишет извлечение, и затереть
    их формой значило бы тихо потерять данные прогона."""
    doc_id = _add_doc()
    client.patch(f"/documents/{doc_id}", json={"doc_type": "presentation"})
    doc = m.get_store().get_document(doc_id)
    assert doc.company_name == "ОТП Банк" and doc.period == "2024"
