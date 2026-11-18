"""Файлы временного атома: проверка, сохранение и краткое описание структуры (часть 16).

Код атома пишет файлы в `out/` комнаты; клиент комнаты (`sealed_room`) приносит их байты. Здесь байты
становятся результатом узла: каждый файл — `FileArtifact` с родословной от `DynamicArtifact` того же узла,
хранится у настоящего владельца и описан одной строкой (листы, слайды, …), которую видит планировщик.

Формат не ограничен списком: платформа не знает, что внутри, пока файл не распознан по расширению и сигнатуре.
Исполняемые расширения отвергаются, содержимое офисного формата проверяется по сигнатуре контейнера, чтобы
«чужой формат под видом xlsx» не дошёл до пользователя.
"""
from __future__ import annotations

import hashlib
import html
import io
import mimetypes
import re
import zipfile
from dataclasses import dataclass

from finengine.contracts import DynamicArtifact, FileArtifact
from finengine.contracts.results import EXECUTABLE_FILE_KINDS, ResultReference, canonical_result_hash
from finengine.skills import sources
from finengine.skills.spec.sealed_room import RoomFile

_MIME_OVERRIDES = {
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "xlsm": "application/vnd.ms-excel.sheet.macroEnabled.12",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}
_SIGNATURES = {
    "xlsx": b"PK", "xlsm": b"PK", "pptx": b"PK", "docx": b"PK", "zip": b"PK",
    "pdf": b"%PDF", "png": b"\x89PNG", "jpg": b"\xff\xd8", "jpeg": b"\xff\xd8",
}
_KIND = re.compile(r"[a-z0-9]{1,8}")
_SHEET_NAME = re.compile(r'<sheet\b[^>]*\bname="([^"]*)"')
_FORMULA = re.compile(r"<f[\s>/]")
_PARAGRAPH = re.compile(r"<w:p[\s>/]")
_PARTS_IN_SUMMARY = re.compile(r"(?:листов|слайдов) (\d+)")
_FORMULAS_IN_SUMMARY = re.compile(r"формул (\d+)")
_FORMULA_TEXT = re.compile(r"<f[^>]*>([^<]+)</f>")
_QUOTED_SHEET = re.compile(r"'[^']*'!")
# Код показателя (`TOTAL_ASSETS`) в формуле: имён функций Excel с подчёркиванием нет, ссылка на лист — с «!».
_METRIC_CODE = re.compile(r"(?<![\w.])[A-Z][A-Z0-9]*_[A-Z0-9_]+(?![\w!(])")


@dataclass(frozen=True)
class FileSpec:
    """Файл, который планировщик обещал в плане: формат и то, что в нём должно быть (часть 16а)."""

    kind: str               # расширение: xlsx, pptx, docx, …
    parts: int = 0          # листов Excel или слайдов, не меньше; 0 — не задано
    formulas: bool = False  # рассчитанные значения записаны формулами Excel

    def text(self) -> str:
        wishes = [f"листов или слайдов не меньше {self.parts}"] if self.parts else []
        if self.formulas:
            wishes.append("рассчитанные значения — формулами в ячейках")
        return f"{self.kind} ({'; '.join(wishes)})" if wishes else self.kind


def file_kind_of(filename: str) -> str:
    return filename.lower().rsplit(".", 1)[1] if "." in filename else ""


def files_problem(files: tuple[RoomFile, ...]) -> str | None:
    """Причина, по которой файлы нельзя выдать, либо None. Одно правило — и для самопроверки автора, и для узла."""
    for file in files:
        kind = file_kind_of(file.name)
        if kind in EXECUTABLE_FILE_KINDS:
            return f"файл {file.name}: исполняемые файлы не выдаются"
        if not _KIND.fullmatch(kind):
            return f"файл {file.name}: нужно расширение формата (латиница и цифры, до 8 знаков)"
        signature = _SIGNATURES.get(kind)
        if signature and not file.content.startswith(signature):
            return f"файл {file.name}: содержимое не похоже на {kind} (контейнер этого формата начинается иначе)"
        code = _metric_code_in_formulas(file.content) if kind in ("xlsx", "xlsm") else None
        if code:
            return (
                f"файл {file.name}: в формуле остался код показателя {code} вместо адреса ячейки — Excel покажет "
                "#NAME?; в формуле допустимы только адреса ячеек, числа и функции, а показатель, у которого нет "
                "ячейки хотя бы для одного входа, запиши готовым числом"
            )
    return None


def _metric_code_in_formulas(content: bytes) -> str | None:
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            sheets = [
                archive.read(part).decode("utf-8", errors="replace")
                for part in archive.namelist() if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", part)
            ]
    except (zipfile.BadZipFile, OSError):
        return None
    for sheet in sheets:
        for formula in _FORMULA_TEXT.findall(sheet):
            found = _METRIC_CODE.search(_QUOTED_SHEET.sub("", html.unescape(formula)))
            if found:
                return found.group(0)
    return None


def describe_file(filename: str, content: bytes) -> str:
    """Одна строка о структуре файла; сбой разбора не мешает: остаётся размер."""
    kind = file_kind_of(filename)
    head = f"{kind}, {len(content)} байт"
    try:
        body = {"xlsx": _describe_xlsx, "xlsm": _describe_xlsx, "pptx": _describe_pptx, "docx": _describe_docx}[kind](content)
    except KeyError:
        body = ""
    except (zipfile.BadZipFile, ValueError, OSError):
        body = ""
    return f"{head}; {body}" if body else head


def spec_problem(filename: str, summary: str, spec: FileSpec) -> str | None:
    """Чем файл беднее обещанного. Сверяется описание структуры (`describe_file`): его же видит планировщик."""
    parts = _PARTS_IN_SUMMARY.search(summary)
    if spec.parts and parts and int(parts.group(1)) < spec.parts:
        return f"файл {filename}: листов или слайдов {parts.group(1)}, по плану нужно не меньше {spec.parts}"
    formulas = _FORMULAS_IN_SUMMARY.search(summary)
    if spec.formulas and formulas and int(formulas.group(1)) == 0:
        return (
            f"файл {filename}: формул 0, а по плану рассчитанные значения записываются формулами в ячейках — "
            'строкой с «=» и ссылками на ячейки с исходными значениями (`ws["B2"] = "=B3/B4"`), не числом и не текстом'
        )
    return None


def expected_files_problem(files: tuple[RoomFile, ...], expected: tuple[FileSpec, ...]) -> str | None:
    """Самопроверка автора: файл обещанного формата не беднее плана. Атом без такого файла не виноват."""
    for spec in expected:
        for file in files:
            if file_kind_of(file.name) == spec.kind:
                problem = spec_problem(file.name, describe_file(file.name, file.content), spec)
                if problem:
                    return problem
    return None


def _describe_xlsx(content: bytes) -> str:
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        names = [html.unescape(name) for name in _SHEET_NAME.findall(archive.read("xl/workbook.xml").decode("utf-8"))]
        formulas = sum(
            len(_FORMULA.findall(archive.read(part).decode("utf-8", errors="replace")))
            for part in archive.namelist() if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", part)
        )
    return f"листов {len(names)} ({', '.join(names)}); формул {formulas}"


def _describe_pptx(content: bytes) -> str:
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        slides = sum(1 for part in archive.namelist() if re.fullmatch(r"ppt/slides/slide\d+\.xml", part))
    return f"слайдов {slides}"


def _describe_docx(content: bytes) -> str:
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        text = archive.read("word/document.xml").decode("utf-8", errors="replace")
    return f"абзацев {len(_PARAGRAPH.findall(text))}"


def persist_room_files(
    files: tuple[RoomFile, ...], *, produced: DynamicArtifact, template_id: str, template_version: str, ctx,
) -> list[FileArtifact]:
    """Сохранить файлы узла у владельца прогона и вернуть их артефакты. Нарушение правил — `ValueError`."""
    if not files:
        return []
    problem = files_problem(files)
    if problem:
        raise ValueError(problem)
    owner = ctx.inputs.extra.get("owner_id")
    if not owner:
        raise ValueError("не известен владелец файла: файл нельзя выдать пользователю (owner_id не задан)")
    project = ctx.inputs.extra.get("project_id") or ctx.run_id
    reference = ResultReference(
        result_type="DynamicArtifact", result_id=produced.evidence_id, schema_version="1.0",
        run_id=ctx.run_id, evidence_version="1.0", result_hash=canonical_result_hash(produced),
    )
    artifacts = []
    for file in files:
        kind = file_kind_of(file.name)
        mime = _MIME_OVERRIDES.get(kind) or mimetypes.guess_type(file.name)[0] or "application/octet-stream"
        _meta, location = sources.save_generated_bytes(owner, project, file.name, file.content, mime)
        artifacts.append(FileArtifact(
            run_id=ctx.run_id, file_kind=kind, mime_type=mime, filename=file.name,
            template_id=template_id, template_version=template_version, source_result=reference,
            content_hash=hashlib.sha256(file.content).hexdigest(), object_store_location=location,
            size_bytes=len(file.content), summary=describe_file(file.name, file.content),
        ))
    return artifacts
