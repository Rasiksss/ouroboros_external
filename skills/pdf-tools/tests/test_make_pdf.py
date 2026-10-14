from __future__ import annotations

import pathlib

import pytest
from conftest import RunScript


def read_pdf_text(path: pathlib.Path) -> str:
    from pypdf import PdfReader

    return "\n".join(page.extract_text() or "" for page in PdfReader(str(path)).pages)


def font_entries(path: pathlib.Path) -> list[dict]:
    from pypdf import PdfReader

    fonts = PdfReader(str(path)).pages[0]["/Resources"]["/Font"]
    return [font.get_object() for font in fonts.values()]


def embedded_faces(path: pathlib.Path) -> set[str]:
    """PostScript names of the fonts drawn on the first page."""
    return {str(entry["/BaseFont"]).lstrip("/") for entry in font_entries(path)}


def test_builds_pdf_with_heading_and_paragraph(run_script: RunScript, tmp_path: pathlib.Path) -> None:
    out = tmp_path / "report.pdf"
    text = "# Quarterly report\n\nFirst paragraph.\n\n- bullet one\n1. step one"

    code, payload = run_script("make_pdf.py", tmp_path, "--out", out.name, "--text", text)

    assert (code, payload["status"]) == (0, "ok")
    assert payload["pages"] >= 1
    assert payload["file"] == "report.pdf"

    extracted = read_pdf_text(out)
    for fragment in ("Quarterly report", "First paragraph.", "bullet one", "step one"):
        assert fragment in extracted


def test_renders_cyrillic_text_on_a_host_without_fonts(run_script: RunScript, tmp_path: pathlib.Path) -> None:
    out = tmp_path / "cyrillic.pdf"

    code, payload = run_script("make_pdf.py", tmp_path, "--out", out.name, "--text", "Отчёт за квартал — ёЁъЩ")

    assert (code, payload["status"]) == (0, "ok")
    assert "Отчёт за квартал — ёЁъЩ" in read_pdf_text(out)


def test_embeds_every_face_it_draws(run_script: RunScript, tmp_path: pathlib.Path) -> None:
    """The file must render the same on a pod with no fonts installed, so no face may stay a reference."""
    out = tmp_path / "embedded.pdf"

    code, payload = run_script("make_pdf.py", tmp_path, "--out", out.name, "--text", "# Отчёт\n\n**bold** and *italic*")

    assert (code, payload["status"]) == (0, "ok")
    for entry in font_entries(out):
        descendants = entry.get("/DescendantFonts")
        descriptor = (descendants[0].get_object() if descendants else entry)["/FontDescriptor"]
        assert any("FontFile" in str(key) for key in descriptor), entry["/BaseFont"]


def test_bold_and_italic_are_distinct_faces(run_script: RunScript, tmp_path: pathlib.Path) -> None:
    out = tmp_path / "faces.pdf"

    code, payload = run_script("make_pdf.py", tmp_path, "--out", out.name, "--text", "**bold** and *italic*")

    assert (code, payload["status"]) == (0, "ok")
    assert {"Helvetica", "Helvetica-Bold", "Helvetica-Oblique"} <= embedded_faces(out)


def test_italic_inside_a_heading_is_the_bold_italic_face(run_script: RunScript, tmp_path: pathlib.Path) -> None:
    # Headings render bold, so emphasis inside one needs the fourth face, not plain italic.
    out = tmp_path / "heading.pdf"

    code, payload = run_script("make_pdf.py", tmp_path, "--out", out.name, "--text", "# Отчёт *важно*")

    assert (code, payload["status"]) == (0, "ok")
    assert "Helvetica-BoldOblique" in embedded_faces(out)


def test_user_text_is_drawn_literally_never_as_markup(run_script: RunScript, tmp_path: pathlib.Path) -> None:
    out = tmp_path / "markup.pdf"
    text = 'see <img src="http://127.0.0.1:9/x.png" width="10" height="10"/> and <font color="red">red</font>'

    code, payload = run_script("make_pdf.py", tmp_path, "--out", out.name, "--text", text)

    assert (code, payload["status"]) == (0, "ok")
    assert '<font color="red">red</font>' in " ".join(read_pdf_text(out).split())


def test_a_backslash_keeps_markup_characters_literal(run_script: RunScript, tmp_path: pathlib.Path) -> None:
    out = tmp_path / "escaped.pdf"
    text = "2 \\* 3 \\* 4\n\n1\\. Item\n\n\\# no heading\n\npath C:\\\\dir and a \\| b\n\n**bold \\*star\\***"

    code, payload = run_script("make_pdf.py", tmp_path, "--out", out.name, "--text", text)

    assert (code, payload["status"]) == (0, "ok")
    extracted = read_pdf_text(out)
    for fragment in ("2 * 3 * 4", "1. Item", "# no heading", "C:\\dir and a | b", "bold *star*"):
        assert fragment in extracted
    # Escaped stars are text: no italic face was drawn.
    assert "Helvetica-Oblique" not in embedded_faces(out)


def test_a_row_of_single_dashes_is_data_not_a_header_separator(run_script: RunScript, tmp_path: pathlib.Path) -> None:
    out = tmp_path / "dashes.pdf"
    text = "| Region | Total |\n| - | - |\n| Moscow | 10 |"

    code, payload = run_script("make_pdf.py", tmp_path, "--out", out.name, "--text", text)

    assert (code, payload["status"]) == (0, "ok")
    assert "-" in read_pdf_text(out)


@pytest.mark.parametrize("name", ["sub/escaped.pdf", "../escaped.pdf"], ids=["subdirectory", "parent directory"])
def test_refuses_a_destination_that_is_not_a_bare_file_name(
    run_script: RunScript, tmp_path: pathlib.Path, name: str
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    code, payload = run_script("make_pdf.py", workspace, "--out", name, "--text", "text")

    assert code == 2
    assert "not a bare file name" in payload["error"]
    assert not list(tmp_path.rglob("escaped.pdf"))


def test_refuses_to_run_without_a_working_directory(
    run_script: RunScript, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Outside skill_exec there is no workdir; the current directory must not become the fallback.
    monkeypatch.chdir(tmp_path)

    code, payload = run_script("make_pdf.py", None, "--out", "stray.pdf", "--text", "text")

    assert code == 2
    assert "OUROBOROS_SKILL_WORKDIR" in payload["error"]
    assert not (tmp_path / "stray.pdf").exists()


@pytest.mark.parametrize("text_args", [(), ("--text", "   ")], ids=["no text", "blank text"])
def test_refuses_empty_input(run_script: RunScript, tmp_path: pathlib.Path, text_args: tuple[str, ...]) -> None:
    code, payload = run_script("make_pdf.py", tmp_path, "--out", "empty.pdf", *text_args)

    assert code == 2
    assert payload["status"] == "error"


def test_joins_several_text_parts_in_order(run_script: RunScript, tmp_path: pathlib.Path) -> None:
    out = tmp_path / "parts.pdf"

    code, payload = run_script(
        "make_pdf.py", tmp_path, "--out", out.name, "--text", "# Title", "--text", "First.\n\nSecond."
    )

    assert (code, payload["status"]) == (0, "ok")
    extracted = read_pdf_text(out)
    assert extracted.index("Title") < extracted.index("First.") < extracted.index("Second.")


def test_strips_control_characters_from_the_source(run_script: RunScript, tmp_path: pathlib.Path) -> None:
    out = tmp_path / "control.pdf"

    code, payload = run_script("make_pdf.py", tmp_path, "--out", out.name, "--text", "text\x0chere\x01")

    assert (code, payload["status"]) == (0, "ok")
    assert "texthere" in read_pdf_text(out)


def test_renders_a_pipe_table_instead_of_raw_markup(run_script: RunScript, tmp_path: pathlib.Path) -> None:
    out = tmp_path / "table.pdf"
    text = "| Region | Total |\n| --- | --- |\n| Moscow | 10 |"

    code, payload = run_script("make_pdf.py", tmp_path, "--out", out.name, "--text", text)

    assert (code, payload["status"]) == (0, "ok")

    extracted = read_pdf_text(out)
    for fragment in ("Region", "Total", "Moscow", "10"):
        assert fragment in extracted
    assert "|" not in extracted
    assert "---" not in extracted


def test_a_long_document_flows_onto_more_pages(run_script: RunScript, tmp_path: pathlib.Path) -> None:
    out = tmp_path / "long.pdf"
    text = "\n\n".join(f"Paragraph {index} " + "word " * 60 for index in range(60))

    code, payload = run_script("make_pdf.py", tmp_path, "--out", out.name, "--text", text)

    assert (code, payload["status"]) == (0, "ok")
    assert payload["pages"] > 1
    extracted = read_pdf_text(out)
    assert "Paragraph 0" in extracted
    assert "Paragraph 59" in extracted


def test_refuses_a_table_cell_taller_than_a_page(run_script: RunScript, tmp_path: pathlib.Path) -> None:
    # A table row never splits across pages, so a cell it cannot fit on one page has nowhere to go.
    out = tmp_path / "tall.pdf"
    text = "| Note | Total |\n| " + " ".join(["word"] * 3000) + " | 10 |"

    code, payload = run_script("make_pdf.py", tmp_path, "--out", out.name, "--text", text)

    assert (code, payload["status"]) == (2, "error")
    assert "taller than a page" in payload["error"]
    assert not out.exists()
