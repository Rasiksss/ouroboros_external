---
name: docx-tools
description: Build a Word document (.docx) with headings, lists, tables and emphasis from text, and read a .docx this skill built back into that markup. A document the user attached is read with attachment_read.
version: 1.0.0
type: script
runtime: python3
# net: the scripts make no network calls; without the grant hosts that block
# unshare cannot enforce network=deny and refuse to run them (fail closed).
permissions: [fs, net]
timeout_sec: 120
scripts:
  - name: make_docx.py
    description: Build a .docx from plain or lightly structured text.
  - name: read_docx.py
    description: Read a .docx this skill built back into the same markup.
env_from_settings: []
when_to_use: The user asks for a Word file, a letter, a contract, a memo or a report as .docx, or asks to change a .docx this skill produced; Russian equivalents include сделай документ Word, оформи в docx, договор, служебная записка.
---

# Word document builder and reader

Build the file by name, take its drive path from `artifacts` in the result, and
deliver that path with `send_file`:

```
skill_exec(skill="distribution:docx-tools", script="make_docx.py",
           args=["--out", "report.docx", "--text", "# Title\n\nBody."])
→ "artifacts": ["uploads/task_artifacts/<task_id>/report.docx"]
send_file(file_path="uploads/task_artifacts/<task_id>/report.docx")
```

`--out` is a bare file name ending in `.docx`; the script never sees paths. A long
document goes in several `--text` parts, each a run of whole paragraphs, joined in
order: the OS refuses a single command-line argument over 128 KB.

## Supported markup

- `#` to `####` — headings
- blank line — new paragraph
- `- ` or `* ` — bulleted item, `1. ` — numbered item; a list that follows another
  block starts at 1 again
- `| a | b |` — table; a row of `---` cells under the first row makes it a header
- `**bold**`, `*italic*`
- `\` before `\ * | # - . )` keeps the character literal: `2 \* 3` is a product,
  `1\. Предмет` is a paragraph, not a list item; `read_docx.py` writes text this way

## Reading and editing

`read_docx.py --in uploads/report.docx` takes the drive path from an earlier
result and returns the document as the markup above in `text`, so an edit is read,
change the text, build again; a package is never changed in place. Only files this
skill built are readable. A document the user attached is read with
`attachment_read`, whose plain-text output is valid input for `make_docx.py`. The
text is capped at 100 000 characters; `truncated: true` and a trailing
`[TRUNCATED]` mark the cut. Text content only: macros, tracked changes, headers
and footers, templates and images are not carried either way.

## Results and refusals

The script prints one JSON object. `status: "ok"` carries the file name and size;
characters below U+0020, which Word cannot store, are dropped silently. Tell the
user the document exists only after `send_file` has delivered it. Every refusal is
explicit and none of them writes a file:

| `status` | Meaning |
|---|---|
| `error` | `--out` is not a bare `.docx` name, the input text is empty, or `--in` is not an artifact of this task |
| `invalid_input` | the file given to `read_docx.py` is not a readable .docx |
| `input_too_large` | the file given to `read_docx.py` is over 50 MB, or its package would inflate past the archive limits |
