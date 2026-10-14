"""Regenerates the two binary fixtures under `files/` — run once, output is checked in.

    .venv/Scripts/python.exe tests/golden/security/make_files.py

Обе не опасны сами по себе: битый PDF — это просто невалидные байты с
PDF-заголовком (никакого шелл-кода), а «архивная бомба» распаковывается всего
в 10 МБ нулей — достаточно, чтобы показать высокий коэффициент сжатия, но не
достаточно, чтобы что-либо исчерпать.
"""
from __future__ import annotations

import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
FILES = HERE / "files"
FILES.mkdir(exist_ok=True)


def make_corrupted_pdf() -> None:
    path = FILES / "corrupted.pdf"
    path.write_bytes(
        b"%PDF-1.4\n"
        b"1 0 obj\n<< /Type /Catalog"  # обрублен незакрытый объект, нет xref/trailer
        b"\x00\x01\x02\xff\xfe\xfd" * 64
    )


def make_archive_bomb() -> None:
    path = FILES / "ratio_bomb.zip"
    payload = b"\x00" * (10 * 1024 * 1024)  # 10 MiB нулей — сжимается почти без остатка
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        zf.writestr("inflated.bin", payload)


if __name__ == "__main__":
    make_corrupted_pdf()
    make_archive_bomb()
    print("wrote", FILES / "corrupted.pdf", "and", FILES / "ratio_bomb.zip")
