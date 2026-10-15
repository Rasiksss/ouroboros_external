"""`doc.probe` — дешёвый технический паспорт документа до OCR и до финансовой
классификации (A6/A5, docs/build-plan/11-srez-01-run-passport.md).

Переиспользует `ingestion.pdf_classifier.classify_pdf` (постраничный digital/
scanned сигнал) и не дублирует его эвристику. Вид документа определяется в два
этапа по карточке: сначала дешёвые детерминированные признаки (имя файла,
затем первые символы текстового слоя первой страницы — без полного OCR);
модельный второй этап при низкой уверенности сюда НЕ подключён в этой сессии —
карточка не даёт для него ни промпта, ни шлюза, строить его без заказа было бы
спекулятивным расширением (CLAUDE.md §2). При низкой уверенности
`doc_type_labels` просто остаётся пустым, а не угадывается моделью втихую.
"""
from __future__ import annotations

import hashlib
import re
import tempfile
from collections.abc import Callable
from pathlib import Path

from finengine.contracts import DocumentProbe, DocumentText
from finengine.ingestion.pdf_classifier import PDFClassification, classify_pdf
from finengine.skills.sources import MAX_BYTES as _MAX_UPLOAD_BYTES

TEXTIFY_VERSION = "1.0.0"

# (байты PDF, страницы 1-based, которым нужен OCR) -> {страница: (текст, качество 0..1)}.
# Одним вызовом на ВСЕ страницы пакета — не по одной, чтобы не плодить сетевых
# круглых поездок там, где реальный клиент умеет отдать несколько страниц сразу.
OcrPagesFn = Callable[[bytes, list[int]], dict[int, tuple[str, float]]]

_FILENAME_TYPE_MARKERS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"transcript|транскрипт|звонок|call", re.I), "TRANSCRIPT"),
    (re.compile(r"press|пресс|release|релиз", re.I), "PRESS_RELEASE"),
    (re.compile(r"presentation|презентация|investor", re.I), "PRESENTATION"),
)
_TEXT_TYPE_MARKERS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"мсфо|ifrs|международные стандарты", re.I), "IFRS_REPORT"),
    (re.compile(r"рсбу|rsbu|российские стандарты", re.I), "RSBU_REPORT"),
)


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def classify_doc_type_labels(filename: str, text_sample: str) -> list[str]:
    """Детерминированные метки вида документа — общая логика `doc.probe` и
    тонкой обёртки `ingestion.pdf_parser.detect_doc_type`.

    Несколько меток допустимы (составной годовой отчёт — карточка `doc.probe`);
    имя файла проверяется первым как самый дешёвый и надёжный сигнал.
    """
    by_filename = [label for pattern, label in _FILENAME_TYPE_MARKERS if pattern.search(filename)]
    if by_filename:
        return by_filename
    return [label for pattern, label in _TEXT_TYPE_MARKERS if pattern.search(text_sample)]


def probe_document(
    path: str | Path,
    *,
    filename: str | None = None,
    pdf_classification: PDFClassification | None = None,
) -> DocumentProbe:
    """Технический паспорт файла: сигнатура, размер, страницы, текстовый слой.

    `sha256` считается ВСЕГДА, даже для нечитаемых/повреждённых файлов (по
    доступным байтам) — версия документа определяется содержимым, а не именем
    файла (инженерный принцип карточки `doc.probe`).

    `pdf_classification` — вызывающий, которому и так нужна постраничная
    `classify_pdf()` (например, `pdf_parser.parse_pdf` для постраничной
    разметки чанков), передаёт уже посчитанный результат, чтобы не открывать
    и не сканировать PDF дважды; сам атом при отдельном вызове считает его
    заново.
    """
    path = Path(path)
    name = filename or path.name

    try:
        raw = path.read_bytes()
    except OSError as exc:
        return DocumentProbe(
            sha256=_sha256_bytes(name.encode("utf-8")),
            byte_size=0,
            technical_status="corrupted",
            total_pages=0,
            reason=f"файл не читается: {exc}",
        )

    digest = _sha256_bytes(raw)
    size = len(raw)

    if size == 0:
        return DocumentProbe(sha256=digest, byte_size=0, technical_status="empty", total_pages=0, reason="файл пуст")
    if size > _MAX_UPLOAD_BYTES:
        return DocumentProbe(
            sha256=digest, byte_size=size, technical_status="too_large", total_pages=0,
            reason=f"{size} байт больше лимита {_MAX_UPLOAD_BYTES}",
        )
    if not raw.startswith(b"%PDF-"):
        return DocumentProbe(
            sha256=digest, byte_size=size, technical_status="not_a_pdf", total_pages=0,
            reason="сигнатура файла не %PDF- (переименованный не-PDF)",
            doc_type_labels=classify_doc_type_labels(name, ""),
        )

    import fitz  # PyMuPDF

    try:
        doc = fitz.open(stream=raw, filetype="pdf")
    except Exception as exc:  # noqa: BLE001 — PyMuPDF кидает разные типы на битый PDF
        return DocumentProbe(
            sha256=digest, byte_size=size, technical_status="corrupted", total_pages=0,
            reason=f"PyMuPDF не смог открыть файл: {exc}",
        )
    try:
        if doc.is_encrypted and not doc.authenticate(""):
            return DocumentProbe(
                sha256=digest, byte_size=size, technical_status="encrypted", total_pages=0,
                reason="документ защищён паролем",
            )
        total_pages = len(doc)
        if total_pages == 0:
            return DocumentProbe(
                sha256=digest, byte_size=size, technical_status="empty", total_pages=0,
                reason="в PDF нет страниц",
            )
        head_text = doc[0].get_text("text")[:4000]
    finally:
        doc.close()

    # Постраничный digital/scanned сигнал — существующий классификатор
    # (не переизобретаем эвристику качества текстового слоя).
    classification = pdf_classification or classify_pdf(path)
    needs_ocr = classification.origin != "digital"

    return DocumentProbe(
        sha256=digest,
        byte_size=size,
        technical_status="ok",
        total_pages=total_pages,
        pdf_origin=classification.origin,
        needs_ocr=needs_ocr,
        doc_type_labels=classify_doc_type_labels(name, head_text),
    )


def _classify_pages_from_bytes(pdf_bytes: bytes) -> PDFClassification | None:
    """Постраничная digital/scanned/empty классификация — тот же
    `ingestion.pdf_classifier.classify_pdf`, что уже использует `doc.probe`
    (не переизобретаем эвристику). `classify_pdf` умеет только путь, не
    байты — мост через временный файл; сам разбор текста ниже идёт из байтов
    напрямую (PyMuPDF умеет открывать поток), временный файл существует
    ровно на время классификации."""
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
        tmp.write(pdf_bytes)
        tmp_path = Path(tmp.name)
    try:
        return classify_pdf(tmp_path)
    except Exception:  # noqa: BLE001 — классификация необязательна: без неё все страницы просто идут в OCR (консервативно)
        return None
    finally:
        tmp_path.unlink(missing_ok=True)


def _default_ocr_pages(pdf_bytes: bytes, page_nums: list[int]) -> dict[int, tuple[str, float]]:
    """Реальный OCR — облачный IDP-сервис (`aifa_baseline.idp_ocr_client`).

    🔴 Шлёт на OCR ВЕСЬ документ (сегодняшний `call_idp_ocr` не умеет
    запрашивать отдельные страницы) и оставляет из ответа только запрошенные
    `page_nums` — `textify_document` вызывает эту функцию, только когда
    список непуст, и только с постранично отфильтрованными номерами, так что
    экономия сетевого ЗАПРОСА здесь не достигается, а инвариант «в OCR не
    уходят страницы с готовым цифровым текстовым слоем» — достигается: они
    просто не читаются из ответа. Постраничный запрос к сервису — отдельная
    доработка `idp_ocr_client`, вне карточки `doc.textify`.
    """
    import asyncio

    from finengine.skills.aifa_baseline.idp_ocr_client import call_idp_ocr

    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
        tmp.write(pdf_bytes)
        tmp_path = Path(tmp.name)
    try:
        result = asyncio.run(call_idp_ocr(tmp_path))
    finally:
        tmp_path.unlink(missing_ok=True)
    wanted = set(page_nums)
    # Уверенность самого IDP-ответа постранично не приходит — 0.9 такой же
    # плейсхолдер, каким `pdf_classifier` метит уверенно распознанный скан.
    return {page.page: (page.markdown, 0.9) for page in result.pages if page.page in wanted}


def textify_document(
    document_id: str,
    document_version: str,
    pdf_bytes: bytes,
    *,
    ocr_pages: OcrPagesFn | None = None,
) -> list[DocumentText]:
    """`doc.textify` — постраничный нормализованный текст с сохранением
    происхождения каждой страницы и факта применения OCR (карточка
    `doc.textify`, 15a).

    Идемпотентна: чистая функция от `(document_id, document_version,
    pdf_bytes, ocr_pages)` — тот же вход даёт тот же список страниц, без
    скрытого состояния. Ошибка одной страницы (сбой OCR-пакета целиком, сбой
    чтения конкретной цифровой страницы) остаётся ЛОКАЛЬНЫМ статусом этой
    страницы и не мешает вернуть успешно обработанные соседние.
    """
    ocr_pages = ocr_pages or _default_ocr_pages
    try:
        import fitz  # PyMuPDF

        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    except Exception as exc:  # noqa: BLE001 — битый/нечитаемый PDF целиком: один явный статус, не падение атома
        return [DocumentText(
            document_id=document_id, document_version=document_version, page=1, text="",
            extraction_method="digital", quality=0.0, status="error",
            error=f"PyMuPDF не смог открыть документ: {exc}",
        )]

    classification = _classify_pages_from_bytes(pdf_bytes)
    page_class_by_num = {p.page_num: p for p in (classification.pages if classification else [])}

    digital_pages: dict[int, str] = {}
    pages: list[DocumentText] = []
    ocr_needed: list[int] = []
    try:
        total_pages = len(doc)
        for i in range(total_pages):
            page_num = i + 1
            page_cls = page_class_by_num.get(page_num)
            origin = page_cls.origin if page_cls is not None else "scanned"  # без классификации — консервативно на OCR
            if origin == "digital":
                try:
                    digital_pages[page_num] = doc[i].get_text("text")
                except Exception as exc:  # noqa: BLE001 — сбой ОДНОЙ страницы, не всего документа
                    pages.append(DocumentText(
                        document_id=document_id, document_version=document_version, page=page_num, text="",
                        extraction_method="digital", quality=0.0, status="error", error=str(exc),
                    ))
            elif origin == "empty":
                pages.append(DocumentText(
                    document_id=document_id, document_version=document_version, page=page_num, text="",
                    extraction_method="digital",
                    quality=page_cls.confidence if page_cls is not None else 1.0,
                    status="ok",
                ))
            else:  # "scanned" (или классификация недоступна)
                ocr_needed.append(page_num)
    finally:
        doc.close()

    ocr_results: dict[int, tuple[str, float]] = {}
    ocr_failed_message = ""
    if ocr_needed:
        try:
            ocr_results = ocr_pages(pdf_bytes, ocr_needed) or {}
        except Exception as exc:  # noqa: BLE001 — сбой OCR-пакета: каждая ждавшая его страница получает явный статус
            ocr_failed_message = f"OCR не выполнен: {exc}"

    for page_num, text in digital_pages.items():
        confidence = page_class_by_num[page_num].confidence if page_num in page_class_by_num else 1.0
        pages.append(DocumentText(
            document_id=document_id, document_version=document_version, page=page_num, text=text,
            extraction_method="digital", quality=confidence, status="ok",
        ))

    for page_num in ocr_needed:
        if ocr_failed_message:
            pages.append(DocumentText(
                document_id=document_id, document_version=document_version, page=page_num, text="",
                extraction_method="ocr", quality=0.0, status="error", error=ocr_failed_message,
            ))
            continue
        result = ocr_results.get(page_num)
        if result is None:
            pages.append(DocumentText(
                document_id=document_id, document_version=document_version, page=page_num, text="",
                extraction_method="ocr", quality=0.0, status="error", error="OCR не вернул эту страницу",
            ))
            continue
        raw_text, quality = result
        from finengine.ingestion.pdf_parser import _fix_ocr_numbers  # noqa: PLC0415 — тяжёлый OCR-модуль только когда реально нужен

        fixed_text = _fix_ocr_numbers(raw_text)
        pages.append(DocumentText(
            document_id=document_id, document_version=document_version, page=page_num, text=fixed_text,
            raw_ocr_text=raw_text if fixed_text != raw_text else None,
            extraction_method="ocr", quality=quality, status="ok",
        ))

    pages.sort(key=lambda p: p.page)
    return pages
