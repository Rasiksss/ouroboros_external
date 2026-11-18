#!/usr/bin/env python3
"""Build a PDF from plain or lightly structured text."""

from __future__ import annotations

import argparse
import re
import sys

import common
import pymupdf

_PAGE_WIDTH, _PAGE_HEIGHT = pymupdf.paper_size("a4")
_MARGIN_X = 57.0  # 20 mm
_MARGIN_Y = 51.0  # 18 mm
_TEXT_WIDTH = _PAGE_WIDTH - 2 * _MARGIN_X
_TEXT_HEIGHT = _PAGE_HEIGHT - 2 * _MARGIN_Y
_BODY_SIZE = 10.0
_HEADING_SIZES = {1: 18.0, 2: 14.0, 3: 12.0, 4: 10.0}
_LIST_INDENT = 18.0
_CELL_PAD = 3.0
_GRID_COLOR = (0.5, 0.5, 0.5)

# MuPDF substitutes its bundled Nimbus Sans for the Base-14 Helvetica names and embeds
# the used subset, so Cyrillic renders the same on a host with no fonts installed.
_FACES = {
    "plain": pymupdf.Font("helv"),
    "bold": pymupdf.Font("hebo"),
    "italic": pymupdf.Font("heit"),
    "bold-italic": pymupdf.Font("hebi"),
}

Piece = tuple[str, str]
Word = list[Piece]
Line = list[Piece]


def _line_height(size: float) -> float:
    return size * 1.2


def _width(pieces: list[Piece], size: float) -> float:
    return sum(_FACES[style].text_length(text, size) for text, style in pieces)


def words_of(text: str, *, bold: bool = False) -> list[Word]:
    """Split emphasised text into words; one word may mix styles (`**bo**ld`), its pieces stay glued."""
    words: list[Word] = []
    glued = False
    for fragment, style in common.split_emphasis(text):
        if bold:
            style = "bold-italic" if style == "italic" else "bold"
        for token in re.split(r"(\s+)", fragment):
            if not token:
                continue
            if token.isspace():
                glued = False
                continue
            if glued and words:
                words[-1].append((token, style))
            else:
                words.append([(token, style)])
            glued = True
    return words


def _hard_break(word: Word, size: float, width: float) -> list[Word]:
    """Split a word wider than the line by characters, so it cannot run past the margin."""
    chunks: list[Word] = [[]]
    x = 0.0
    for text, style in word:
        for char in text:
            char_width = _FACES[style].text_length(char, size)
            if chunks[-1] and x + char_width > width:
                chunks.append([])
                x = 0.0
            chunk = chunks[-1]
            if chunk and chunk[-1][1] == style:
                chunk[-1] = (chunk[-1][0] + char, style)
            else:
                chunk.append((char, style))
            x += char_width
    return chunks


def wrap(words: list[Word], size: float, width: float) -> list[Line]:
    """Greedy word wrap; the spaces between words on one line become explicit pieces."""
    space = _FACES["plain"].text_length(" ", size)
    lines: list[Line] = [[]]
    x = 0.0
    for whole in words:
        parts = [whole] if _width(whole, size) <= width else _hard_break(whole, size, width)
        for word in parts:
            word_width = _width(word, size)
            if lines[-1] and x + space + word_width > width:
                lines.append([])
                x = 0.0
            if lines[-1]:
                lines[-1].append((" ", "plain"))
                x += space
            lines[-1].extend(word)
            x += word_width
    return lines


class Composer:
    """Owns the document, the open page and the vertical cursor; text goes through one TextWriter per page."""

    def __init__(self) -> None:
        self.doc = pymupdf.open()
        self._start_page()

    def _start_page(self) -> None:
        self.page = self.doc.new_page(width=_PAGE_WIDTH, height=_PAGE_HEIGHT)
        self.writer = pymupdf.TextWriter(self.page.rect)
        self.y = _MARGIN_Y

    def flush(self) -> None:
        self.writer.write_text(self.page)

    def fit(self, height: float) -> None:
        """Open a fresh page when *height* does not fit below the cursor."""
        if self.y + height > _PAGE_HEIGHT - _MARGIN_Y and self.y > _MARGIN_Y:
            self.flush()
            self._start_page()

    def draw_pieces(self, pieces: Line, x: float, baseline: float, size: float) -> None:
        for text, style in pieces:
            _, last = self.writer.append((x, baseline), text, font=_FACES[style], fontsize=size)
            x = last.x

    def write_line(self, line: Line, x: float, size: float, *, marker: str = "") -> None:
        self.fit(_line_height(size))
        baseline = self.y + size
        if marker:
            self.draw_pieces([(marker, "plain")], _MARGIN_X, baseline, size)
        self.draw_pieces(line, x, baseline, size)
        self.y += _line_height(size)


def render_heading(comp: Composer, block: common.Heading) -> None:
    size = _HEADING_SIZES[block.level]
    comp.y += size * 0.6
    for line in wrap(words_of(block.text, bold=True), size, _TEXT_WIDTH):
        comp.write_line(line, _MARGIN_X, size)
    comp.y += 4


def render_paragraph(comp: Composer, text: str) -> None:
    for line in wrap(words_of(text), _BODY_SIZE, _TEXT_WIDTH):
        comp.write_line(line, _MARGIN_X, _BODY_SIZE)
    comp.y += 4


def render_list_item(comp: Composer, text: str, marker: str) -> None:
    for index, line in enumerate(wrap(words_of(text), _BODY_SIZE, _TEXT_WIDTH - _LIST_INDENT)):
        comp.write_line(line, _MARGIN_X + _LIST_INDENT, _BODY_SIZE, marker=marker if index == 0 else "")
    comp.y += 2


def render_table(comp: Composer, block: common.Table) -> None:
    columns = max(len(row) for row in block.rows)
    column_width = _TEXT_WIDTH / columns
    cell_width = column_width - 2 * _CELL_PAD
    comp.y += 6
    for position, row in enumerate(block.rows):
        header = block.header and position == 0
        padded = [row[index] if index < len(row) else "" for index in range(columns)]
        cells = [wrap(words_of(cell, bold=header), _BODY_SIZE, cell_width) for cell in padded]
        height = max(len(lines) for lines in cells) * _line_height(_BODY_SIZE) + 2 * _CELL_PAD
        if height > _TEXT_HEIGHT:
            common.fail("a table row never splits across pages: split the table cell that is taller than a page")
        comp.fit(height)
        top = comp.y
        for index, lines in enumerate(cells):
            x = _MARGIN_X + index * column_width
            comp.page.draw_rect(pymupdf.Rect(x, top, x + column_width, top + height), color=_GRID_COLOR, width=0.4)
            baseline = top + _CELL_PAD + _BODY_SIZE
            for line in lines:
                comp.draw_pieces(line, x + _CELL_PAD, baseline, _BODY_SIZE)
                baseline += _line_height(_BODY_SIZE)
        comp.y = top + height
    comp.y += 8


def render(blocks: list[common.Block]) -> pymupdf.Document:
    comp = Composer()
    ordinal = 0
    for block in blocks:
        if isinstance(block, common.Heading):
            ordinal = 0
            render_heading(comp, block)
        elif isinstance(block, common.Table):
            ordinal = 0
            render_table(comp, block)
        elif isinstance(block, common.ListItem):
            ordinal = ordinal + 1 if block.ordered else 0
            render_list_item(comp, block.text, f"{ordinal}." if block.ordered else "•")
        else:
            ordinal = 0
            render_paragraph(comp, block.text)
    comp.flush()
    return comp.doc


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Build a PDF from text.")
    parser.add_argument("--out", required=True, help="file name of the PDF to build, without directories")
    parser.add_argument(
        "--text",
        action="append",
        default=[],
        help="document text; repeat for a long document, parts are joined in order",
    )
    args = parser.parse_args(argv)

    blocks = common.parse_blocks(common.clean_text(args.text))
    if not blocks:
        common.fail("nothing to render: the input text is empty")

    out_path = common.workdir_file(args.out, must_exist=False)
    if out_path.suffix.lower() != ".pdf":
        common.fail(f"destination must end with .pdf: {out_path.name}")

    document = render(blocks)
    document.set_metadata({"title": out_path.stem})
    pages = document.page_count
    try:
        document.ez_save(str(out_path))
    except OSError as exc:
        common.fail(f"cannot write {out_path.name}: {exc}")
    document.close()

    common.emit(
        {
            "status": "ok",
            "file": out_path.name,
            "bytes": out_path.stat().st_size,
            "pages": pages,
        }
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
