---
name: pdf-tools
description: Build a PDF with headings, lists, tables and emphasis from text, rendering Russian and English correctly, and extract the text of a PDF this skill built. A PDF the user attached is read with attachment_read.
version: 1.0.0
type: script
runtime: python3
# net: the scripts make no network calls; without the grant hosts that block
# unshare cannot enforce network=deny and refuse to run them (fail closed).
permissions: [fs, net]
timeout_sec: 120
scripts:
  - name: make_pdf.py
    description: Build a PDF from plain or lightly structured text.
  - name: read_pdf.py
    description: Extract the text of a PDF this skill built.
env_from_settings: []
when_to_use: The user asks for a PDF, a printable document or a report exported to PDF, or asks to change a PDF this skill produced; Russian equivalents include сделай PDF, сохрани в пдф, версия для печати.
---

# PDF builder and reader

Build the file by name, take its drive path from `artifacts` in the result, and
deliver that path with `send_file`:

```
skill_exec(skill="distribution:pdf-tools", script="make_pdf.py",
           args=["--out", "report.pdf", "--text", "# Title\n\nBody."])
→ "artifacts": ["uploads/task_artifacts/<task_id>/report.pdf"]
send_file(file_path="uploads/task_artifacts/<task_id>/report.pdf")
```

`--out` is a bare file name ending in `.pdf`; the script never sees paths. A long
document goes in several `--text` parts, each a run of whole paragraphs, joined in
order: the OS refuses a single command-line argument over 128 KB. Pages are A4.

## Supported markup

- `#` to `####` — headings
- blank line — new paragraph
- `- ` or `* ` — bulleted item, `1. ` — numbered item; a list that follows another
  block starts at 1 again
- `| a | b |` — table; a row of `---` cells under the first row makes it a header
- `**bold**`, `*italic*`
- `\` before `\ * | # - . )` keeps the character literal: `2 \* 3` is a product,
  `1\. Предмет` is a paragraph, not a list item; `read_pdf.py` writes text this way

## Fonts

Text is drawn with the renderer's built-in faces (Nimbus Sans: regular, bold,
italic), and the glyphs used are embedded into the file, so Russian and English
render the same on any host. Nothing is installed on the pod and there is no
font option.

## Reading and editing

`read_pdf.py --in uploads/report.pdf` takes the drive path from an earlier result
and returns the extracted text in `text` and the page count in `pages`. PDF keeps
layout, not structure: headings, lists and tables come back as plain lines with
markup characters escaped, so an edit is read, restore the markup you need, build
a new file. Only files this skill built are readable; a PDF the user attached is
read with `attachment_read`. The text is capped at 100 000 characters;
`truncated: true` and a trailing `[TRUNCATED]` mark the cut. A password-protected
PDF is refused. Text only: images, headers and footers, page numbering and form
filling are not carried either way.

## Results and refusals

The script prints one JSON object. `status: "ok"` carries the file name, size
and page count; characters below U+0020 have no glyph and are dropped silently.
Tell the user the document exists only after `send_file` has delivered it.
Every refusal is explicit and none of them writes a usable file:

| `status` | Meaning |
|---|---|
| `error` | `--out` is not a bare `.pdf` name, the input text is empty, a table cell is taller than one page, or `--in` is not an artifact of this task |
| `invalid_input` | the file given to `read_pdf.py` is not a readable PDF or is encrypted |
| `input_too_large` | the file given to `read_pdf.py` is over 50 MB |
