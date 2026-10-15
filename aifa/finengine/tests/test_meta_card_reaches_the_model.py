"""Метакарта доезжает до модели, а не лежит в базе (#189).

ЗАЧЕМ ЭТОТ ФАЙЛ. Карта снимается при приёме и хранится с документом — но сама
по себе она бесполезна: пока агент её не ВИДИТ, он по-прежнему знает про
документ только имя файла, путает документы между собой и запускает извлечение
полного реестра по пресс-релизу.

Это ровно та болезнь, которой за 05.08 набралось пять случаев: сделанное не
доезжает до места работы. Поэтому доставка карты проверяется отдельно от её
снятия — снятое и показанное это два разных факта.

ПРОВЕРКА СТОРОЖА ОТКАТОМ: убрать строку карты из `_build_context` — тест
`test_card_is_in_the_document_header` обязан покраснеть.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from finengine.models.document import DocumentMeta
from finengine.orchestrator import agent as A

CARD = {
    "chars": 213, "lines": 9, "pages_marked": 2, "has_page_markers": True,
    "doc_kind": "отчётность", "issuer": "ПАО «ФосАгро»", "issuer_aliases": ["PhosAgro"],
    "period": "2024", "summary": "годовая отчётность по МСФО",
}


class _Index:
    def __init__(self, metas: dict[str, DocumentMeta]) -> None:
        self._metas = metas

    def get_document(self, doc_id: str):
        return self._metas.get(doc_id)

    def search(self, *a, **kw):
        return []


@pytest.fixture
def stand(tmp_path, monkeypatch):
    """Документ с готовым текстом на диске + индекс, знающий его метаданные."""
    def _make(card: dict) -> str:
        doc_id = "doc-1"
        (tmp_path / f"{doc_id}.txt").write_text("===PAGE 1===\nВыручка 100\n",
                                                encoding="utf-8")
        meta = DocumentMeta(doc_id=doc_id, filename="phosagro_2024.pdf",
                            company_name="ФосАгро", meta_card=card)
        monkeypatch.setattr(A, "get_store", lambda: _Index({doc_id: meta}))
        from finengine.config import settings
        monkeypatch.setattr(settings, "docs_dir", Path(tmp_path))
        return doc_id
    return _make


def test_card_is_in_the_document_header(stand):
    """ГЛАВНОЕ: то, что сняли при приёме, модель видит в ходе."""
    doc_id = stand(CARD)
    context, _ = A._build_context("вопрос", [doc_id])
    assert "отчётность" in context, "вид документа до модели не доехал"
    assert "ПАО «ФосАгро»" in context
    assert "2024" in context
    assert "стр. 1–2" in context, "объём и разметка страниц не показаны"
    assert "годовая отчётность по МСФО" in context


def test_card_stands_before_the_text(stand):
    """Карта — под шапкой, до текста: иначе она не поможет решить, читать ли."""
    doc_id = stand(CARD)
    context, _ = A._build_context("вопрос", [doc_id])
    assert context.index("отчётность") < context.index("Выручка 100")


def test_document_without_a_card_still_works(stand):
    """Документ принят до появления карты — ход не ломается и лишнего не пишет."""
    doc_id = stand({})
    context, _ = A._build_context("вопрос", [doc_id])
    assert "Выручка 100" in context
    assert "карта не снята" not in context, (
        "пустая карта не повод писать про неё в промпт каждого хода"
    )
