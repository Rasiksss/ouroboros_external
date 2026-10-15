"""Источники проекта: файлы, которые пользователь приложил к проекту.

Три предмета на источник, и это не изменилось при переезде:

    <file_id>.meta.json  — {file_id, filename, content_type, size, uploaded_at, text_status}
    <file_id>.<ext>      — байты оригинала
    <file_id>.txt        — извлечённый текст (best-effort, для тула read_source)

`file_id` — uuid4; исходное имя лежит в карточке. Разделение имени и ключа
хранения снимает и совпадения имён, и странные символы в пути.

Извлечение текста ленивое: одна попытка при загрузке (PDF / XLSX / TXT / CSV /
MD). Отказ попадает в `text_status`, чтобы агент мог решить — повторить или
пропустить.

ЗАЧЕМ ПЕРЕЕЗД В ОБЪЕКТНОЕ ХРАНИЛИЩЕ. `docs/rules/architecture.md` 3.1
(🔴 MUST) называет этот случай прямее всех остальных: «загрузки/скачивания — в
объектное хранилище через стриминг, никогда на диск… парсинг —
`materialize(key)->Path` + `release`». Карточки и текст здесь тоже объекты, а
не строки базы: они всегда читаются вместе с байтами и по тому же ключу, и
разводить их по двум хранилищам значило бы завести рассинхрон на пустом месте.

⚠️ РАЗБОР PDF И XLSX ТРЕБУЕТ ПУТИ НА ДИСКЕ — библиотеки берут файл, а не поток.
Поэтому на время разбора объект кладётся во временный файл парой
`materialize`/`release`: уборка становится обязанностью вызывающего и видна в
коде. Без неё правило «никогда на диск» превращается в «на диск, но тайком».

ПЕРЕКЛЮЧАТЕЛЬ — общий с прочими объектами, `FINENGINE_S3_ENDPOINT`. Пусто =
файлы на томе, как было.
"""
from __future__ import annotations

import dataclasses
import json
import logging
import re
import time
import uuid
from pathlib import Path

from finengine.contracts.results import EXECUTABLE_FILE_KINDS
from finengine.store import blobs

log = logging.getLogger(__name__)

# Whitelist of file extensions we'll accept. Others get a 400. PPTX deferred.
ALLOWED_EXT = {".pdf", ".xlsx", ".xls", ".csv", ".txt", ".md", ".json"}

# Max upload size — 50MB per the existing /documents/upload conventions.
MAX_BYTES = 50 * 1024 * 1024


@dataclasses.dataclass
class SourceMeta:
    file_id: str
    filename: str           # original name (with possibly Cyrillic / spaces)
    content_type: str
    size: int
    uploaded_at: float      # unix seconds
    text_status: str = ""   # "ok" | "failed: <reason>" | "" (not attempted)

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)


def _префикс(user_id: int | str, project_id: str) -> str:
    """Префикс ключей проекта. Собирается ОДНОЙ функцией.

    Раскладка повторяет прежнюю файловую один в один — это и делает переезд
    переносом, а не пересборкой: старые файлы ложатся под теми же именами.
    """
    from finengine.llm.tools import _safe_path_segment  # noqa: PLC0415

    uid = _safe_path_segment(str(user_id))
    pid = _safe_path_segment(str(project_id))
    return f"users/{uid}/projects/{pid}/sources/"


def _safe_ext(filename: str) -> str:
    """Lowercased extension or empty string."""
    name = filename.lower()
    if "." not in name:
        return ""
    return "." + name.rsplit(".", 1)[1]


async def save_upload(
    user_id: int | str,
    project_id: str,
    filename: str,
    content: bytes,
    content_type: str = "",
) -> SourceMeta:
    """Persist an uploaded file + meta + extract text. Raises on
    bad extension or oversize."""
    if len(content) > MAX_BYTES:
        raise ValueError(f"file too large: {len(content)} > {MAX_BYTES}")
    ext = _safe_ext(filename)
    if ext not in ALLOWED_EXT:
        raise ValueError(f"extension {ext!r} not allowed; whitelist={sorted(ALLOWED_EXT)}")

    file_id = uuid.uuid4().hex
    префикс = _префикс(user_id, project_id)
    store = blobs.get_blob_store()
    store.put(f"{префикс}{file_id}{ext}", content)

    # Разбор требует пути на диске — кладём объект во временный файл и убираем
    # за собой. См. шапку: без пары materialize/release правило «никогда на
    # диск» превращается в «на диск, но тайком».
    временный = store.materialize(f"{префикс}{file_id}{ext}")
    try:
        text, text_status = _extract_text(временный, ext)
    finally:
        store.release(временный)
    if text:
        store.put(f"{префикс}{file_id}.txt", text.encode("utf-8"))

    meta = SourceMeta(
        file_id=file_id,
        filename=filename,
        content_type=content_type or _guess_ct(ext),
        size=len(content),
        uploaded_at=time.time(),
        text_status=text_status,
    )
    store.put(f"{префикс}{file_id}.meta.json",
              json.dumps(meta.to_dict(), ensure_ascii=False, indent=2).encode("utf-8"))
    log.info("источник сохранён: user=%s project=%s file=%s (%d байт, текст=%s)",
             user_id, project_id, file_id, len(content), text_status)
    return meta


def save_generated_bytes(
    user_id: int | str,
    project_id: str,
    filename: str,
    content: bytes,
    content_type: str,
) -> tuple[SourceMeta, str]:
    """Persist an already-rendered artifact without parsing it again.

    Exporters own presentation only. Reopening their XLSX/PDF through the
    ingestion extractor would create a second, accidental processing path and
    waste work; generated files therefore get bytes + metadata only.
    """
    if not content:
        raise ValueError("generated file is empty")
    if len(content) > MAX_BYTES:
        raise ValueError(f"file too large: {len(content)} > {MAX_BYTES}")
    ext = _generated_ext(filename)
    file_id = uuid.uuid4().hex
    prefix = _префикс(user_id, project_id)
    object_key = f"{prefix}{file_id}{ext}"
    store = blobs.get_blob_store()
    store.put(object_key, content)
    meta = SourceMeta(
        file_id=file_id,
        filename=filename,
        content_type=content_type or _guess_ct(ext),
        size=len(content),
        uploaded_at=time.time(),
        text_status="not_applicable: generated artifact",
    )
    store.put(
        f"{prefix}{file_id}.meta.json",
        json.dumps(meta.to_dict(), ensure_ascii=False, indent=2).encode("utf-8"),
    )
    return meta, object_key


def _generated_ext(filename: str) -> str:
    """Расширение сгенерированного файла: любой документный формат, не привязанный к списку (pptx, docx, ...);
    исполняемые и не-расширения отвергаются. Загрузки пользователя по-прежнему идут по `ALLOWED_EXT`."""
    ext = _safe_ext(filename)
    kind = ext.lstrip(".")
    if not re.fullmatch(r"[a-z0-9]{1,8}", kind):
        raise ValueError(f"extension {ext!r} is not a file extension")
    if kind in EXECUTABLE_FILE_KINDS:
        raise ValueError(f"extension {ext!r} not allowed: executables are not delivered")
    return ext


def list_sources(user_id: int | str, project_id: str) -> list[SourceMeta]:
    """Источники проекта, новые первыми."""
    префикс = _префикс(user_id, project_id)
    store = blobs.get_blob_store()
    items: list[SourceMeta] = []
    for ключ in store.iter_keys(префикс):
        if not ключ.endswith(".meta.json"):
            continue
        # Битую карточку пропускаем: один недописанный источник не должен
        # прятать остальные из перечня проекта.
        try:
            items.append(SourceMeta(**json.loads(store.get(ключ).decode("utf-8"))))
        except (json.JSONDecodeError, TypeError, ValueError, UnicodeDecodeError) as e:
            log.warning("пропускаю битую карточку источника %s: %s", ключ, e)
    items.sort(key=lambda m: m.uploaded_at, reverse=True)
    return items


def get_source_meta(user_id: int | str, project_id: str, file_id: str) -> SourceMeta | None:
    ключ = f"{_префикс(user_id, project_id)}{file_id}.meta.json"
    store = blobs.get_blob_store()
    if not store.exists(ключ):
        return None
    try:
        return SourceMeta(**json.loads(store.get(ключ).decode("utf-8")))
    except (json.JSONDecodeError, TypeError, ValueError, UnicodeDecodeError):
        return None


def read_source_text(user_id: int | str, project_id: str, file_id: str, max_chars: int = 200_000) -> str | None:
    """Return extracted text if available, else None."""
    ключ = f"{_префикс(user_id, project_id)}{file_id}.txt"
    store = blobs.get_blob_store()
    if not store.exists(ключ):
        return None
    text = store.get(ключ).decode("utf-8", errors="ignore")
    return text[:max_chars] if len(text) > max_chars else text


def read_source_bytes(user_id: int | str, project_id: str, file_id: str) -> tuple[bytes, SourceMeta] | None:
    """Return original file bytes + meta. Used by the /sources/{id}/file
    endpoint for download/preview in the frontend."""
    meta = get_source_meta(user_id, project_id, file_id)
    if not meta:
        return None
    ключ = f"{_префикс(user_id, project_id)}{file_id}{_safe_ext(meta.filename)}"
    store = blobs.get_blob_store()
    if not store.exists(ключ):
        return None
    return store.get(ключ), meta


def delete_source(user_id: int | str, project_id: str, file_id: str) -> bool:
    """Убрать все три предмета источника. `False` — такого источника не было."""
    meta = get_source_meta(user_id, project_id, file_id)
    if not meta:
        return False
    префикс = _префикс(user_id, project_id)
    store = blobs.get_blob_store()
    for хвост in (_safe_ext(meta.filename), ".txt", ".meta.json"):
        store.delete(f"{префикс}{file_id}{хвост}")
    return True


# ─────────────────── text extraction ──────────────────


def _extract_text(path: Path, ext: str) -> tuple[str, str]:
    """Best-effort text extraction. Returns (text, status)."""
    try:
        if ext == ".pdf":
            return _extract_pdf(path), "ok"
        if ext in (".xlsx", ".xls"):
            return _extract_xlsx(path), "ok"
        if ext in (".txt", ".md", ".csv", ".json"):
            return path.read_text(encoding="utf-8", errors="replace"), "ok"
        return "", f"unsupported ext {ext}"
    except Exception as e:  # noqa: BLE001
        log.warning("text extraction failed for %s: %s", path, e)
        return "", f"failed: {e.__class__.__name__}: {e}"


def _extract_pdf(path: Path) -> str:
    """Use PyMuPDF (already in deps for the legacy /documents/upload path)."""
    import fitz  # PyMuPDF
    doc = fitz.open(path)
    try:
        parts: list[str] = []
        for page in doc:
            parts.append(page.get_text() or "")
        return "\n\n".join(parts).strip()
    finally:
        doc.close()


def _extract_xlsx(path: Path) -> str:
    """Render each non-empty cell row as TSV; sheets separated by header."""
    import openpyxl  # already in deps
    wb = openpyxl.load_workbook(path, data_only=True)
    parts: list[str] = []
    for sname in wb.sheetnames:
        ws = wb[sname]
        parts.append(f"=== Sheet: {sname} ===")
        for row in ws.iter_rows(values_only=True):
            cells = [
                "" if v is None else str(v).replace("\t", " ").replace("\n", " ")
                for v in row
            ]
            if any(cells):
                parts.append("\t".join(cells))
    return "\n".join(parts).strip()


def _guess_ct(ext: str) -> str:
    return {
        ".pdf": "application/pdf",
        ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ".xls": "application/vnd.ms-excel",
        ".csv": "text/csv",
        ".txt": "text/plain",
        ".md": "text/markdown",
        ".json": "application/json",
    }.get(ext, "application/octet-stream")
