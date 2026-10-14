"""ZIP intake: pull the supported documents out of an archive, drop the rest.

Users usually keep a reporting pack as one .zip. The upload endpoint unpacks
it server-side and registers every supported member as its own document.
Anything the pipeline cannot parse — spreadsheets, images, nested archives,
macOS resource forks — is silently skipped instead of failing the upload.

Two hard caps guard the single worker against a zip bomb: the member count and
the total uncompressed size. Header-declared sizes are checked up front (fast
rejection of an honest oversized archive) and the actual reads are capped too,
because the header is attacker-controlled.
"""
from __future__ import annotations

import zipfile
from pathlib import Path, PurePosixPath
from typing import IO

# Keep in sync with what `_parse_doc_in_background` can actually parse.
SUPPORTED_SUFFIXES = (".pdf", ".txt", ".md")

MAX_MEMBERS = 50
MAX_TOTAL_BYTES = 200 * 1024 * 1024

_COPY_CHUNK = 1024 * 1024


class ArchiveError(Exception):
    """Archive is unreadable or its contents blow past the caps.

    Carries the `{code, message}` pair the API turns into an HTTP detail so
    the frontend can localize by code.
    """

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def member_path_parts(arcname: str) -> list[str]:
    """Split an archive path. Zips written on Windows use backslashes."""
    return [p for p in arcname.replace("\\", "/").split("/") if p]


def member_display_name(arcname: str) -> str:
    """Flatten an archive path to the plain filename we store it under.

    Only the basename survives, which both drops the folder structure (the
    point of the feature — one archive becomes N standalone documents) and
    neutralizes `../` traversal.
    """
    return PurePosixPath(arcname.replace("\\", "/")).name


def _is_os_junk(arcname: str) -> bool:
    """True for entries the operating system added, not the user.

    macOS Finder ships a `__MACOSX/` shadow tree with one `._name` AppleDouble
    fork per entry — file *and* folder — so a 20-document archive with nested
    folders easily carries 30+ of them. They are invisible to the person who
    made the archive, so they must not be reported as rejected files: the user
    packed 20 documents and would be told that 32 were thrown away.
    """
    parts = member_path_parts(arcname)
    if "__MACOSX" in parts:
        return True
    name = parts[-1] if parts else ""
    return name.startswith(".") or name.lower() in {"thumbs.db", "desktop.ini"}


def _is_dir_entry(info: zipfile.ZipInfo) -> bool:
    """`ZipInfo.is_dir()` only looks for a trailing slash. Archivers that omit
    it still set the directory bit in the Unix mode, and a folder counted as a
    rejected file is the same false alarm as the `__MACOSX` forks above.
    """
    return info.is_dir() or bool(info.external_attr >> 16 & 0o040000)


def _is_supported(arcname: str) -> bool:
    name = member_display_name(arcname)
    return bool(name) and name.lower().endswith(SUPPORTED_SUFFIXES)


def list_supported(zf: zipfile.ZipFile) -> tuple[list[zipfile.ZipInfo], list[str]]:
    """Split the archive's central directory into keepers and junk.

    Returns the members worth ingesting plus the arcnames worth *telling the
    user* were dropped. Directories and OS metadata are left out of both — the
    skipped list is meant to read as "here is what you packed that we can't
    take", so anything the user did not knowingly put there is noise.
    """
    kept: list[zipfile.ZipInfo] = []
    skipped: list[str] = []
    for info in zf.infolist():
        if _is_dir_entry(info) or _is_os_junk(info.filename):
            continue
        # An empty member has nothing to OCR — it would only ever surface as a
        # document stuck in "processing".
        if not _is_supported(info.filename) or info.file_size <= 0:
            skipped.append(info.filename)
            continue
        kept.append(info)

    if len(kept) > MAX_MEMBERS:
        raise ArchiveError(
            "archive_too_many_files",
            f"В архиве больше {MAX_MEMBERS} подходящих файлов — загрузите их частями.",
        )
    if sum(info.file_size for info in kept) > MAX_TOTAL_BYTES:
        raise ArchiveError(
            "archive_too_large",
            f"Содержимое архива больше {MAX_TOTAL_BYTES // (1024 * 1024)} МБ в распакованном виде.",
        )
    return kept, skipped


def copy_member(zf: zipfile.ZipFile, info: zipfile.ZipInfo, dest: Path, *, budget: int) -> int:
    """Stream one member to `dest`, writing at most `budget` bytes.

    Returns the number of bytes written so the caller can shrink the budget for
    the next member. Raises ArchiveError when the member outgrows the budget,
    which only happens if the central directory understated its size — an
    honest archive is already rejected by `list_supported`.
    """
    written = 0
    try:
        with zf.open(info) as src, open(dest, "wb") as out:
            while True:
                chunk = src.read(min(_COPY_CHUNK, budget - written + 1))
                if not chunk:
                    break
                written += len(chunk)
                if written > budget:
                    raise ArchiveError(
                        "archive_too_large",
                        f"Содержимое архива больше {MAX_TOTAL_BYTES // (1024 * 1024)} МБ "
                        "в распакованном виде.",
                    )
                out.write(chunk)
    except ArchiveError:
        # Never leave a half-written member on disk — for a cap breach that
        # partial file is exactly the disk the cap exists to protect.
        dest.unlink(missing_ok=True)
        raise
    except (RuntimeError, zipfile.BadZipFile, OSError) as exc:
        # Encrypted or corrupt entry — the one failure the central directory
        # cannot warn us about. Fail loudly instead of silently producing an
        # archive that unpacked to nothing.
        dest.unlink(missing_ok=True)
        raise ArchiveError(
            "archive_member_unreadable",
            f"Не удалось распаковать «{member_display_name(info.filename)}»: {exc}",
        ) from exc
    return written


def open_archive(source: IO[bytes] | Path) -> zipfile.ZipFile:
    """Open an uploaded archive, mapping a bad container to ArchiveError."""
    try:
        return zipfile.ZipFile(source)
    except (zipfile.BadZipFile, OSError) as exc:
        raise ArchiveError(
            "archive_unreadable",
            "Не удалось прочитать ZIP-архив — файл повреждён или это не ZIP.",
        ) from exc
