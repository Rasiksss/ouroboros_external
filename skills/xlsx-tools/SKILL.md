---
name: xlsx-tools
description: Build an Excel workbook (.xlsx) from JSON rows or the tab-separated dump attachment_read prints, and read an .xlsx this skill built back into those rows. A sheet the user attached is read with attachment_read.
version: 1.0.0
type: script
runtime: python3
# net: the scripts make no network calls; without the grant hosts that block
# unshare cannot enforce network=deny and refuse to run them (fail closed).
permissions: [fs, net]
timeout_sec: 120
scripts:
  - name: make_xlsx.py
    description: Build an .xlsx workbook from JSON or TSV rows.
  - name: read_xlsx.py
    description: Read an .xlsx this skill built into the JSON rows make_xlsx.py accepts.
env_from_settings: []
when_to_use: The user asks for a spreadsheet, a table, a report or a data extract as Excel/.xlsx, or asks to change an .xlsx this skill produced; Russian equivalents include сделай таблицу в Excel, выгрузи в xlsx, сведи в эксель.
---

# Excel workbook builder and reader

Pass the data as JSON, build the file by name, take its drive path from
`artifacts` in the result, and deliver that path with `send_file`:

```
skill_exec(skill="distribution:xlsx-tools", script="make_xlsx.py",
           args=["--out", "data.xlsx", "--text", '[["Region","Total"],["Moscow",1250.5]]'])
→ "artifacts": ["uploads/task_artifacts/<task_id>/data.xlsx"]
send_file(file_path="uploads/task_artifacts/<task_id>/data.xlsx")
```

`--out` is a bare file name ending in `.xlsx`; the script never sees paths. A
large table goes in several `--text` parts joined in order (JSON may be split
anywhere between two tokens, TSV only between two lines): the OS refuses a
single command-line argument over 128 KB.

## Input shape

A bare list of rows becomes a single sheet named `Sheet1`. For several sheets or
a bold header row use the full form:

```json
{"sheets": [{"name": "Sales", "header": true, "rows": [["Month", "Total"], ["January", 10]]}]}
```

Keep numbers as JSON numbers so they stay numeric in Excel; a number sent as a
string stays text and will not sum. Sheet names lose the characters Excel
forbids (`[]:*?/\`) and are cut to 31 characters; columns are widened to their
longest value, up to 60 characters.

`--format tsv` accepts the tab-separated form `attachment_read` prints for a
spreadsheet: cells separated by tabs, one row per line, an optional
`## Sheet: Name` line opening each sheet. Plain decimals (`10`, `-3`, `1.5`)
become numbers; anything else, including `007`, `+79001234567` and `1,5`, stays
text so that codes and phone numbers survive.

## Reading and editing

`read_xlsx.py --in uploads/data.xlsx` takes the drive path from an earlier result
and returns `sheets` in exactly the shape `make_xlsx.py` takes, so an edit is
read, change the rows, build again; a workbook is never changed in place. Only
files this skill built are readable. A workbook the user attached is read with
`attachment_read`, whose output goes to
`make_xlsx.py --format tsv`. Dates come back as ISO strings and a formula as the
value Excel last cached for it (`null` if the file was never opened in Excel).
Rows stop at roughly 100 000 characters of JSON; `truncated: true` marks the
cut. Values only, both ways: text is always written as text, so a cell starting
with `=` stays visible text and is never executed by Excel; formulas, charts,
pivot tables and conditional formatting are not carried either way.

## Results and refusals

The script prints one JSON object. `status: "ok"` carries the file name and size;
characters below U+0020, which Excel rejects, are dropped silently. Tell the user
the workbook exists only after `send_file` has delivered it. Every refusal is
explicit and none of them writes a file:

| `status` | Meaning |
|---|---|
| `invalid_input` | the JSON is malformed, has no `sheets`, or rows are not lists; the file given to `read_xlsx.py` is not a readable .xlsx |
| `input_too_large` | the file given to `read_xlsx.py` is over 50 MB, or its package would inflate past the archive limits |
| `error` | `--out` is not a bare `.xlsx` name, the input is empty, or `--in` is not an artifact of this task |
