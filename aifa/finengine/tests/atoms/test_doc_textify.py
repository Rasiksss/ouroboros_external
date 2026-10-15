"""`doc.textify` — критерий готовности атома (docs/build-plan/
15a-srez-05a-locate-read-value-yadro.md, карточка `doc.textify`):

1. Количество и порядок страниц `DocumentText` совпадают с исходным
   документом в 100% тестов.
2. Каждый фрагмент текста содержит номер страницы и способ получения;
   смешанный PDF отправляет в OCR только страницы, где это действительно
   требуется.
3. Повторная обработка той же версии документа идемпотентна и не создаёт
   другой текстовый артефакт без изменения версии обработчика.
4. Ошибка отдельной страницы сохраняется как локальный статус и не удаляет
   успешно обработанные страницы.
"""
from __future__ import annotations

from pathlib import Path

import fitz

from finengine.skills.atoms.document import textify_document

GOLDEN_SECURITY = Path(__file__).resolve().parents[1] / "golden" / "security" / "files"

_ASCII_PAGE_TEXT = "Annual report page text for testing digital extraction. " * 3


def _digital_pdf_bytes(pages: int = 1) -> bytes:
    doc = fitz.open()
    for i in range(pages):
        page = doc.new_page()
        page.insert_text((72, 72), f"{_ASCII_PAGE_TEXT} page {i + 1}", fontsize=11)
    data = doc.tobytes()
    doc.close()
    return data


def _scanned_page_image_bytes() -> bytes:
    from io import BytesIO

    from PIL import Image

    img = Image.new("RGB", (400, 500), color=(250, 250, 250))
    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _mixed_pdf_bytes() -> bytes:
    """Две цифровые страницы + одна сканированная (без текстового слоя)."""
    doc = fitz.open()
    for i in range(2):
        page = doc.new_page()
        page.insert_text((72, 72), f"{_ASCII_PAGE_TEXT} page {i + 1}", fontsize=11)
    scanned_page = doc.new_page()
    scanned_page.insert_image(scanned_page.rect, stream=_scanned_page_image_bytes())
    data = doc.tobytes()
    doc.close()
    return data


def _no_op_ocr_pages(pdf_bytes: bytes, page_nums: list[int]) -> dict[int, tuple[str, float]]:
    raise AssertionError(f"OCR не должен вызываться для полностью цифрового PDF, но вызван для {page_nums}")


def test_digital_pdf_all_pages_use_local_text_and_never_call_ocr():
    pages = textify_document("doc-1", "v1", _digital_pdf_bytes(2), ocr_pages=_no_op_ocr_pages)
    assert len(pages) == 2
    assert [p.page for p in pages] == [1, 2]
    for page in pages:
        assert page.extraction_method == "digital"
        assert page.status == "ok"
        assert "page" in page.text
        assert page.quality > 0.0


def test_mixed_pdf_sends_only_scanned_pages_to_ocr():
    calls: list[list[int]] = []

    def fake_ocr(pdf_bytes: bytes, page_nums: list[int]):
        calls.append(list(page_nums))
        return {p: (f"ocr text for page {p}", 0.8) for p in page_nums}

    pages = textify_document("doc-1", "v1", _mixed_pdf_bytes(), ocr_pages=fake_ocr)
    assert len(pages) == 3
    assert [p.page for p in pages] == [1, 2, 3]
    assert calls == [[3]], "OCR обязан получить ровно те страницы, у которых нет цифрового текстового слоя"

    by_page = {p.page: p for p in pages}
    assert by_page[1].extraction_method == "digital"
    assert by_page[2].extraction_method == "digital"
    assert by_page[3].extraction_method == "ocr"
    assert by_page[3].status == "ok"
    assert by_page[3].text == "ocr text for page 3"


def test_repeat_call_is_idempotent():
    def fake_ocr(pdf_bytes: bytes, page_nums: list[int]):
        return {p: (f"ocr text for page {p}", 0.8) for p in page_nums}

    pdf_bytes = _mixed_pdf_bytes()
    first = textify_document("doc-1", "v1", pdf_bytes, ocr_pages=fake_ocr)
    second = textify_document("doc-1", "v1", pdf_bytes, ocr_pages=fake_ocr)
    assert [p.model_dump() for p in first] == [p.model_dump() for p in second]


def test_ocr_missing_page_becomes_local_error_without_removing_others():
    """Постранично: OCR вернул страницу 3, но не страницу 4 — сбой одной
    страницы не должен уничтожать успешно обработанную соседнюю."""
    doc = fitz.open()
    for _ in range(2):
        page = doc.new_page()
        page.insert_image(page.rect, stream=_scanned_page_image_bytes())
    pdf_bytes = doc.tobytes()
    doc.close()

    def fake_ocr(pdf_bytes: bytes, page_nums: list[int]):
        return {2: ("ocr text for page 2", 0.8)}  # страница 1 отсутствует в ответе

    pages = textify_document("doc-1", "v1", pdf_bytes, ocr_pages=fake_ocr)
    by_page = {p.page: p for p in pages}
    assert by_page[1].status == "error"
    assert by_page[1].error
    assert by_page[2].status == "ok"
    assert by_page[2].text == "ocr text for page 2"


def test_ocr_batch_failure_gives_every_waiting_page_a_local_error():
    def failing_ocr(pdf_bytes: bytes, page_nums: list[int]):
        raise RuntimeError("облачный OCR недоступен")

    pages = textify_document("doc-1", "v1", _mixed_pdf_bytes(), ocr_pages=failing_ocr)
    by_page = {p.page: p for p in pages}
    assert by_page[1].status == "ok"
    assert by_page[2].status == "ok"
    assert by_page[3].status == "error"
    assert "облачный OCR" in by_page[3].error


def test_corrupted_pdf_returns_a_single_explicit_error_page():
    path = GOLDEN_SECURITY / "corrupted.pdf"
    assert path.exists(), "нужен tests/golden/security/files/corrupted.pdf (Фаза 0, тот же файл что у test_doc_probe.py)"
    pages = textify_document("doc-1", "v1", path.read_bytes(), ocr_pages=_no_op_ocr_pages)
    assert len(pages) == 1
    assert pages[0].status == "error"
    assert pages[0].error


def test_ocr_number_correction_keeps_the_original_raw_text():
    """«Исправления чисел не должны уничтожать исходное значение» (карточка
    `doc.textify`) — `_fix_ocr_numbers` нормализует неразрывный пробел."""
    doc = fitz.open()
    doc.new_page().insert_image(doc[0].rect, stream=_scanned_page_image_bytes())
    pdf_bytes = doc.tobytes()
    doc.close()

    raw = "1 234 567"

    def fake_ocr(pdf_bytes: bytes, page_nums: list[int]):
        return {1: (raw, 0.8)}

    pages = textify_document("doc-1", "v1", pdf_bytes, ocr_pages=fake_ocr)
    assert len(pages) == 1
    assert pages[0].text == "1 234 567"
    assert pages[0].raw_ocr_text == raw
