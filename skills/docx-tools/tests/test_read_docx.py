from __future__ import annotations

import pathlib
import zipfile

import pytest
from conftest import RunScript

SOURCE = (
    "# Отчёт\n\n"
    "Абзац с **жирным** и *курсивом*.\n\n"
    "- первый\n- второй\n\n"
    "1. шаг\n2. шаг\n\n"
    "| Регион | Сумма |\n| --- | --- |\n| Москва | 10 |"
)


def build(run_script: RunScript, workspace: pathlib.Path, text: str) -> str:
    # In parts, the way a long document arrives: Linux caps one argv entry at 128 KiB.
    parts = [text[start : start + 60_000] for start in range(0, len(text), 60_000)]
    text_args = [arg for part in parts for arg in ("--text", part)]
    code, payload = run_script("make_docx.py", workspace, "--out", "doc.docx", *text_args)
    assert (code, payload["status"]) == (0, "ok")
    return "doc.docx"


@pytest.mark.parametrize(
    "source",
    [
        SOURCE,
        "| *Регион* | Сумма |\n| --- | --- |\n| Москва | **10** |",
        "| Регион |  |\n| --- | --- |\n| Москва | 10 |",
        "1\\. Предмет договора\n\n2 \\* 3 \\* 4\n\n\\# не заголовок\n\n\\- не пункт\n\nпуть C:\\\\dir и a \\| b\n\n**жирная \\*звезда\\***",
    ],
    ids=["every block kind", "emphasis inside cells", "empty header cell", "text that looks like markup"],
)
def test_reads_back_the_markup_make_docx_understands(
    run_script: RunScript, tmp_path: pathlib.Path, source: str
) -> None:
    built = build(run_script, tmp_path, source)

    code, payload = run_script("read_docx.py", tmp_path, "--in", built)

    assert (code, payload["status"]) == (0, "ok")
    assert payload["text"] == source
    assert payload["truncated"] is False


@pytest.mark.parametrize(
    "name", ["missing.docx", "uploads/missing.docx"], ids=["bare name", "drive path passed through"]
)
def test_refuses_an_input_that_is_not_among_the_call_inputs(
    run_script: RunScript, tmp_path: pathlib.Path, name: str
) -> None:
    code, payload = run_script("read_docx.py", tmp_path, "--in", name)

    assert code == 2
    assert payload["status"] == "error"
    assert payload["error"].startswith(f"{name} is not among this call's inputs")


def write_package(path: pathlib.Path, members: dict[str, str]) -> str:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, body in members.items():
            archive.writestr(name, body)
    return path.name


@pytest.mark.parametrize(
    "members",
    [
        None,
        {"word/document.xml": "<w:document/>"},
        {"[Content_Types].xml": "<Types/>", "_rels/.rels": "<Relationships/>", "word/document.xml": "not xml <<<"},
    ],
    ids=["not a zip", "document part alone", "document part is not xml"],
)
def test_refuses_a_package_python_docx_cannot_open(
    run_script: RunScript, tmp_path: pathlib.Path, members: dict[str, str] | None
) -> None:
    fake = tmp_path / "fake.docx"
    if members is None:
        fake.write_text("plain text pretending to be a package", encoding="utf-8")
    else:
        write_package(fake, members)

    code, payload = run_script("read_docx.py", tmp_path, "--in", fake.name)

    assert code == 2
    assert payload["status"] == "invalid_input"


def test_refuses_a_package_that_unpacks_far_beyond_its_size(run_script: RunScript, tmp_path: pathlib.Path) -> None:
    # A few KB on disk, megabytes once inflated: the size check on the file alone would let it through.
    bomb = write_package(tmp_path / "bomb.docx", {"word/document.xml": "<w:document>" + " " * 5_000_000})

    code, payload = run_script("read_docx.py", tmp_path, "--in", bomb)

    assert code == 2
    assert payload["status"] == "input_too_large"


def test_refuses_input_over_the_size_limit_before_opening_it(run_script: RunScript, tmp_path: pathlib.Path) -> None:
    huge = tmp_path / "huge.docx"
    with huge.open("wb") as handle:
        handle.truncate(50 * 1024 * 1024 + 1)

    code, payload = run_script("read_docx.py", tmp_path, "--in", huge.name)

    assert code == 2
    assert payload["status"] == "input_too_large"


def test_truncates_long_text_and_says_so(run_script: RunScript, tmp_path: pathlib.Path) -> None:
    # Varied words, not one repeated letter: a document that inflates 200:1 is refused as a package bomb.
    built = build(run_script, tmp_path, " ".join(f"слово{index}" for index in range(15_000)))

    code, payload = run_script("read_docx.py", tmp_path, "--in", built)

    assert (code, payload["status"]) == (0, "ok")
    assert payload["truncated"] is True
    assert payload["text"].endswith("[TRUNCATED]")
    assert len(payload["text"]) <= 100_000
