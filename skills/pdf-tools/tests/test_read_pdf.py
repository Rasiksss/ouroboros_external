from __future__ import annotations

import pathlib

import pytest
from conftest import RunScript


def build(run_script: RunScript, workspace: pathlib.Path, text: str, name: str = "doc.pdf") -> pathlib.Path:
    # In parts, the way a long document arrives: Linux caps one argv entry at 128 KiB.
    parts = [text[start : start + 60_000] for start in range(0, len(text), 60_000)]
    text_args = [arg for part in parts for arg in ("--text", part)]
    code, payload = run_script("make_pdf.py", workspace, "--out", name, *text_args)
    assert (code, payload["status"]) == (0, "ok")
    return workspace / name


def test_reads_the_text_back_page_by_page(run_script: RunScript, tmp_path: pathlib.Path) -> None:
    built = build(run_script, tmp_path, "# Quarterly report\n\nFirst paragraph.\n\n- bullet one\n1. step one")

    code, payload = run_script("read_pdf.py", tmp_path, "--in", built.name)

    assert (code, payload["status"]) == (0, "ok")
    assert payload["file"] == "doc.pdf"
    assert payload["pages"] == 1
    assert payload["truncated"] is False
    for fragment in ("Quarterly report", "First paragraph.", "bullet one", "step one"):
        assert fragment in payload["text"]


def test_extracted_text_is_literal_input_for_make_pdf(run_script: RunScript, tmp_path: pathlib.Path) -> None:
    """A PDF keeps no structure, so what comes back is text to rebuild as itself, not markup to re-parse."""
    built = build(run_script, tmp_path, "1\\. Subject\n\n2 \\* 3 \\* 4\n\n\\# not a heading")

    _, first = run_script("read_pdf.py", tmp_path, "--in", built.name)
    rebuilt = build(run_script, tmp_path, first["text"], "rebuilt.pdf")
    _, second = run_script("read_pdf.py", tmp_path, "--in", rebuilt.name)

    for fragment in ("1\\. Subject", "2 \\* 3 \\* 4", "\\# not a heading"):
        assert fragment in first["text"]
    # Line breaks are layout a PDF does not keep, and a `#` that lands mid-line needs no escape any more;
    # the words themselves must come back unchanged.
    assert second["text"].replace("\\", "").split() == first["text"].replace("\\", "").split()


@pytest.mark.parametrize("name", ["missing.pdf", "uploads/missing.pdf"], ids=["bare name", "drive path passed through"])
def test_refuses_an_input_that_is_not_among_the_call_inputs(
    run_script: RunScript, tmp_path: pathlib.Path, name: str
) -> None:
    code, payload = run_script("read_pdf.py", tmp_path, "--in", name)

    assert code == 2
    assert payload["status"] == "error"
    assert payload["error"].startswith(f"{name} is not among this call's inputs")


def test_refuses_an_encrypted_pdf_instead_of_crashing(run_script: RunScript, tmp_path: pathlib.Path) -> None:
    from pypdf import PdfReader, PdfWriter

    plain = build(run_script, tmp_path, "secret", "plain.pdf")
    writer = PdfWriter(clone_from=PdfReader(str(plain)))
    writer.encrypt("owner-password")
    locked = tmp_path / "locked.pdf"
    with locked.open("wb") as handle:
        writer.write(handle)

    code, payload = run_script("read_pdf.py", tmp_path, "--in", locked.name)

    assert code == 2
    assert payload["status"] == "invalid_input"
    assert "encrypted" in payload["error"]


@pytest.mark.parametrize("case", ["not a pdf", "cut in half"])
def test_refuses_a_file_pypdf_cannot_read(run_script: RunScript, tmp_path: pathlib.Path, case: str) -> None:
    fake = tmp_path / "fake.pdf"
    if case == "not a pdf":
        fake.write_text("not a pdf", encoding="utf-8")
    else:
        whole = build(run_script, tmp_path, "some text").read_bytes()
        fake.write_bytes(whole[: len(whole) // 2])

    code, payload = run_script("read_pdf.py", tmp_path, "--in", fake.name)

    assert code == 2
    assert payload["status"] == "invalid_input"


def test_refuses_input_over_the_size_limit_before_opening_it(run_script: RunScript, tmp_path: pathlib.Path) -> None:
    huge = tmp_path / "huge.pdf"
    with huge.open("wb") as handle:
        handle.truncate(50 * 1024 * 1024 + 1)

    code, payload = run_script("read_pdf.py", tmp_path, "--in", huge.name)

    assert code == 2
    assert payload["status"] == "input_too_large"


def test_truncates_long_text_and_says_so(run_script: RunScript, tmp_path: pathlib.Path) -> None:
    built = build(run_script, tmp_path, "\n\n".join(["lorem ipsum dolor sit amet " * 40] * 120))

    code, payload = run_script("read_pdf.py", tmp_path, "--in", built.name)

    assert (code, payload["status"]) == (0, "ok")
    assert payload["truncated"] is True
    assert payload["text"].endswith("[TRUNCATED]")
    assert len(payload["text"]) <= 100_000
