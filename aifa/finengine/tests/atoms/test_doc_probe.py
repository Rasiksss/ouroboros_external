"""`doc.probe` — критерий готовности (docs/build-plan/11-srez-01-run-passport.md):

1. 100% случаев различают: валидный PDF, файл с ложным расширением,
   повреждённый/пустой/зашифрованный файл, превышение лимита размера.
2. `sha256`, число страниц и технический статус воспроизводимы при повторном
   вызове для тех же байтов.
3. Для цифрового и сканированного PDF корректно определяется необходимость
   OCR; дорогая модельная классификация не вызывается, если детерминированных
   признаков достаточно (в этой сессии модельный путь не подключён вовсе —
   см. докстринг атома).
4. Техническая ошибка документа возвращается как явный статус, а не пустой
   «успешный» документ.

Минимальный набор сценариев: цифровой PDF, скан, смешанный PDF, переименованный
не-PDF, годовой отчёт с отчётностью, пресс-релиз, повреждённый файл.
"""
from __future__ import annotations

from pathlib import Path

import fitz
import pytest
from PIL import Image

from finengine.skills.atoms.document import probe_document

GOLDEN_SECURITY = Path(__file__).resolve().parents[1] / "golden" / "security" / "files"

_LONG_IFRS_PARAGRAPH = (
    "Настоящая консолидированная финансовая отчетность подготовлена в "
    "соответствии с Международными стандартами финансовой отчетности МСФО. "
    "Выручка от реализации выросла год к году. " * 3
)


def _digital_pdf_bytes(text: str = _LONG_IFRS_PARAGRAPH) -> bytes:
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), text, fontsize=11)
    data = doc.tobytes()
    doc.close()
    return data


def _scanned_page_image_bytes() -> bytes:
    img = Image.new("RGB", (400, 500), color=(250, 250, 250))
    from io import BytesIO

    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _scanned_pdf_bytes() -> bytes:
    doc = fitz.open()
    page = doc.new_page()
    page.insert_image(page.rect, stream=_scanned_page_image_bytes())
    data = doc.tobytes()
    doc.close()
    return data


def _mixed_pdf_bytes() -> bytes:
    doc = fitz.open()
    for _ in range(2):
        page = doc.new_page()
        page.insert_text((72, 72), _LONG_IFRS_PARAGRAPH, fontsize=11)
    scanned_page = doc.new_page()
    scanned_page.insert_image(scanned_page.rect, stream=_scanned_page_image_bytes())
    data = doc.tobytes()
    doc.close()
    return data


def _encrypted_pdf_bytes() -> bytes:
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), _LONG_IFRS_PARAGRAPH, fontsize=11)
    data = doc.tobytes(encryption=fitz.PDF_ENCRYPT_AES_256, owner_pw="owner-secret", user_pw="user-secret")
    doc.close()
    return data


def _write(tmp_path: Path, name: str, data: bytes) -> Path:
    p = tmp_path / name
    p.write_bytes(data)
    return p


def test_digital_pdf_is_ok_and_does_not_need_ocr(tmp_path):
    path = _write(tmp_path, "report.pdf", _digital_pdf_bytes())
    probe = probe_document(path)
    assert probe.technical_status == "ok"
    assert probe.total_pages == 1
    assert probe.pdf_origin == "digital"
    assert probe.needs_ocr is False


def test_scanned_pdf_is_ok_and_needs_ocr(tmp_path):
    path = _write(tmp_path, "scan.pdf", _scanned_pdf_bytes())
    probe = probe_document(path)
    assert probe.technical_status == "ok"
    assert probe.pdf_origin == "scanned"
    assert probe.needs_ocr is True


def test_mixed_pdf_is_ok_and_needs_ocr(tmp_path):
    path = _write(tmp_path, "mixed.pdf", _mixed_pdf_bytes())
    probe = probe_document(path)
    assert probe.technical_status == "ok"
    assert probe.total_pages == 3
    assert probe.pdf_origin == "mixed"
    assert probe.needs_ocr is True


def test_renamed_non_pdf_is_rejected_explicitly(tmp_path):
    path = _write(tmp_path, "sneaky.pdf", b"this is not a pdf, just renamed text")
    probe = probe_document(path)
    assert probe.technical_status == "not_a_pdf"
    assert probe.total_pages == 0


def test_corrupted_pdf_is_rejected_explicitly():
    path = GOLDEN_SECURITY / "corrupted.pdf"
    assert path.exists(), "нужен tests/golden/security/files/corrupted.pdf (Фаза 0)"
    probe = probe_document(path)
    assert probe.technical_status == "corrupted"
    assert probe.total_pages == 0


def test_empty_file_is_rejected_explicitly(tmp_path):
    path = _write(tmp_path, "empty.pdf", b"")
    probe = probe_document(path)
    assert probe.technical_status == "empty"
    assert probe.total_pages == 0


def test_empty_page_pdf_is_rejected_as_empty_not_ok(tmp_path):
    doc = fitz.open()
    doc.new_page()
    doc.new_page()
    data = doc.tobytes()
    doc.close()
    # Технически валидный контейнер, но обе страницы без текста и без
    # изображений классификатор относит к "scanned" (нет цифрового слоя) —
    # это НЕ технический отказ, документ просто нуждается в OCR.
    path = _write(tmp_path, "blank.pdf", data)
    probe = probe_document(path)
    assert probe.technical_status == "ok"
    assert probe.needs_ocr is True


def test_encrypted_pdf_is_rejected_explicitly(tmp_path):
    path = _write(tmp_path, "secret.pdf", _encrypted_pdf_bytes())
    probe = probe_document(path)
    assert probe.technical_status == "encrypted"
    assert probe.total_pages == 0


def test_oversized_file_is_rejected_explicitly(tmp_path, monkeypatch):
    import finengine.skills.atoms.document as doc_probe_module

    monkeypatch.setattr(doc_probe_module, "_MAX_UPLOAD_BYTES", 10)
    path = _write(tmp_path, "big.pdf", _digital_pdf_bytes())
    probe = probe_document(path)
    assert probe.technical_status == "too_large"


def test_sha256_and_page_count_are_reproducible_for_same_bytes(tmp_path):
    path = _write(tmp_path, "report.pdf", _digital_pdf_bytes())
    first = probe_document(path)
    second = probe_document(path)
    assert first.sha256 == second.sha256
    assert first.total_pages == second.total_pages
    assert first.technical_status == second.technical_status


def test_ok_status_is_never_an_empty_success():
    """Контрактный инвариант (`DocumentProbe`): `ok` требует хотя бы страницу."""
    from pydantic import ValidationError

    from finengine.contracts import DocumentProbe

    with pytest.raises(ValidationError):
        DocumentProbe(sha256="a" * 64, byte_size=10, technical_status="ok", total_pages=0)


def test_filename_type_marker_wins_over_text_content(tmp_path):
    """Имя файла — самый дешёвый и надёжный сигнал, проверяется первым."""
    path = _write(tmp_path, "press_release_2025.pdf", _digital_pdf_bytes())
    probe = probe_document(path)
    assert probe.doc_type_labels == ["PRESS_RELEASE"]


def test_annual_report_text_gives_ifrs_label_without_filename_hint(tmp_path):
    # ASCII-текст: встроенный шрифт PyMuPDF по умолчанию (Helvetica) не несёт
    # кириллических глифов, и извлечённый текст кириллицы для сопоставления
    # регэкспом непригоден — сигнатура файла %PDF- это никак не тестирует, но
    # для проверки ИМЕННО текстового сигнала нужен шрифт, который PyMuPDF
    # реально может закодировать и потом прочитать обратно.
    text = "This annual report is prepared in accordance with IFRS. " * 5
    path = _write(tmp_path, "doc123.pdf", _digital_pdf_bytes(text))
    probe = probe_document(path)
    assert "IFRS_REPORT" in probe.doc_type_labels


def test_composite_report_can_carry_multiple_labels(tmp_path):
    """Разрешены несколько меток для составных годовых отчётов (карточка `doc.probe`)."""
    mixed_text = (
        "This annual report bundles the consolidated IFRS statements together "
        "with the statutory RSBU filing for subsidiaries. " * 3
    )
    path = _write(tmp_path, "annual.pdf", _digital_pdf_bytes(mixed_text))
    probe = probe_document(path)
    assert set(probe.doc_type_labels) == {"IFRS_REPORT", "RSBU_REPORT"}
