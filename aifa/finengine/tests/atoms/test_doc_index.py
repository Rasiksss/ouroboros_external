"""`doc.index` — критерий готовности атома (docs/build-plan/
15a-srez-05a-locate-read-value-yadro.md, карточка `doc.index`):

1. Каждый элемент индекса ссылается только на существующие страницы/диапазоны
   документа; несуществующие диапазоны запрещены в 100% тестов.
2. На эталонном наборе не менее 95% размеченных ключевых разделов/таблиц
   присутствуют в индексе с правильным диапазоном страниц (замер на реальном
   golden-наборе — вне этой сессии, см. `tests/golden/structures/README.md`;
   здесь — единичные, но точные примеры того же свойства).
3. Неизвестный раздел сохраняется как неизвестный, а не принудительно
   относится к известному типу.
4. Один `doc_version` + одна версия индексатора дают один кэшируемый индекс;
   повторный запрос не пересканирует документ (чистота функции).
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from finengine.contracts import DocumentIndex, DocumentIndexEntry, DocumentText
from finengine.skills.atoms.document_index import build_document_index

_BALANCE_SHEET_TABLE = (
    "BALANCE SHEET\n"
    "| Item | 2024 | 2023 |\n"
    "| Total assets | 100 000 | 90 000 |\n"
    "| Total liabilities | 40 000 | 35 000 |\n"
    "| Equity | 60 000 | 55 000 |\n"
)

_INCOME_STATEMENT_TABLE = (
    "INCOME STATEMENT\n"
    "| Item | 2024 | 2023 |\n"
    "| Revenue | 500 000 | 450 000 |\n"
    "| Net income | 80 000 | 70 000 |\n"
)


def _no_op_labeler(_summaries):
    return {}


def _pages(*texts: str) -> list[DocumentText]:
    return [
        DocumentText(document_id="doc-1", document_version="v1", page=i + 1, text=text)
        for i, text in enumerate(texts)
    ]


def test_one_document_gives_one_index_with_correct_metadata():
    pages = _pages(_BALANCE_SHEET_TABLE, _INCOME_STATEMENT_TABLE)
    index = build_document_index(
        pages, document_id="doc-1", document_version="v1", total_pages=2, table_labeler=_no_op_labeler,
    )
    assert index.document_id == "doc-1"
    assert index.document_version == "v1"
    assert index.total_pages == 2


def test_tables_are_detected_and_reference_existing_pages():
    pages = _pages(_BALANCE_SHEET_TABLE, _INCOME_STATEMENT_TABLE)
    index = build_document_index(
        pages, document_id="doc-1", document_version="v1", total_pages=2, table_labeler=_no_op_labeler,
    )
    table_entries = [e for e in index.entries if e.kind == "table"]
    assert len(table_entries) == 2
    for entry in table_entries:
        assert 1 <= entry.page_start <= 2
        assert 1 <= entry.page_end <= 2


def test_unknown_table_is_preserved_as_unknown_not_forced_into_a_known_type():
    pages = _pages(_BALANCE_SHEET_TABLE)
    index = build_document_index(
        pages, document_id="doc-1", document_version="v1", total_pages=1, table_labeler=_no_op_labeler,
    )
    table_entries = [e for e in index.entries if e.kind == "table"]
    assert table_entries
    assert all(entry.label == "" for entry in table_entries), "дефолтный лейблер ничего не сказал — метка обязана остаться пустой, не угаданной"


def test_labeler_assigns_a_known_label_by_content():
    def labeler(summaries):
        out = {}
        for item in summaries:
            sample = (item.get("title", "") + item.get("sample", "")).lower()
            if "balance sheet" in sample:
                out[item["entry_id"]] = "balance_sheet"
            elif "income statement" in sample:
                out[item["entry_id"]] = "income_statement"
        return out

    pages = _pages(_BALANCE_SHEET_TABLE, _INCOME_STATEMENT_TABLE)
    index = build_document_index(
        pages, document_id="doc-1", document_version="v1", total_pages=2, table_labeler=labeler,
    )
    labels = {e.label for e in index.entries if e.kind == "table"}
    assert labels == {"balance_sheet", "income_statement"}


def test_broken_page_does_not_prevent_indexing_the_rest():
    pages = [
        DocumentText(document_id="doc-1", document_version="v1", page=1, text="", status="error", error="OCR failed"),
        DocumentText(document_id="doc-1", document_version="v1", page=2, text=_BALANCE_SHEET_TABLE),
    ]
    index = build_document_index(
        pages, document_id="doc-1", document_version="v1", total_pages=2, table_labeler=_no_op_labeler,
    )
    table_entries = [e for e in index.entries if e.kind == "table"]
    assert table_entries, "страница 2 успешно обработана — таблица обязана попасть в индекс несмотря на ошибку страницы 1"
    assert all(e.page_start == 2 for e in table_entries)


def test_same_pages_and_versions_give_an_equal_index():
    pages = _pages(_BALANCE_SHEET_TABLE, _INCOME_STATEMENT_TABLE)
    first = build_document_index(
        pages, document_id="doc-1", document_version="v1", total_pages=2, table_labeler=_no_op_labeler,
    )
    second = build_document_index(
        pages, document_id="doc-1", document_version="v1", total_pages=2, table_labeler=_no_op_labeler,
    )
    assert first.model_dump(exclude={"entries"}) == second.model_dump(exclude={"entries"})
    assert [e.model_dump(exclude={"entry_id"}) for e in first.entries] == [
        e.model_dump(exclude={"entry_id"}) for e in second.entries
    ]


def test_entry_referencing_a_page_beyond_the_document_is_rejected():
    with pytest.raises(ValidationError):
        DocumentIndex(
            document_id="doc-1", document_version="v1", indexer_version="1.0.0", total_pages=2,
            entries=[DocumentIndexEntry(kind="table", page_start=3, page_end=3)],
        )


def test_entry_page_range_must_be_ordered():
    with pytest.raises(ValidationError):
        DocumentIndexEntry(kind="table", page_start=3, page_end=1)
