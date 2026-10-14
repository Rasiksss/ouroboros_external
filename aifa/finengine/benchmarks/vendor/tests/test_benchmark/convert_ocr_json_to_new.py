"""Convert legacy flat OCR JSON fixtures to normalized markdown-table fixtures.

The old bank benchmark JSON files contain `ocr_result.text` with inline
markdown tables plus a separate duplicated `ocr_result.tables` list. Current
production OCR normalization keeps a cleaned full markdown text and stores
normalized table metadata separately. This script rebuilds that shape without
calling OCR again and writes sibling `*_new.json` files.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from finengine.skills.aifa_baseline.idp_ocr_client import (  # noqa: E402
    _attach_table_positions_and_context,
    _normalize_tables_in_text,
)


DEFAULT_DIRS = (
    ROOT / "валидация_банки_ру_годовые",
    ROOT / "валидация_банки_мсфо",
)
DEFAULT_SKIP = {"metrics_ifrs_ru_pending.json"}


def _output_path(path: Path) -> Path:
    return path.with_name(f"{path.stem}_new{path.suffix}")


def _old_table_page(old_tables: list[Any], idx: int) -> int | None:
    if idx >= len(old_tables):
        return None
    table = old_tables[idx]
    if not isinstance(table, dict):
        return None
    page = table.get("page")
    try:
        return int(page) if page not in {None, ""} else None
    except (TypeError, ValueError):
        return None


def convert_one(path: Path, *, context_lines: int = 5, overwrite: bool = False) -> dict[str, Any]:
    if path.stem.lower().endswith("_new"):
        return {"path": str(path), "skipped": True, "reason": "already_new"}
    out_path = _output_path(path)
    if out_path.exists() and not overwrite:
        return {"path": str(path), "output": str(out_path), "skipped": True, "reason": "exists"}

    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"Expected object JSON: {path}")
    ocr = data.get("ocr_result")
    if not isinstance(ocr, dict):
        raise ValueError(f"Missing object ocr_result: {path}")
    text = str(ocr.get("text") or "")
    if not text.strip():
        raise ValueError(f"Empty ocr_result.text: {path}")

    normalized_text, extracted_tables = _normalize_tables_in_text(text)
    old_tables = list(ocr.get("tables") or [])
    full_tables: list[dict[str, Any]] = []
    for idx, table in enumerate(extracted_tables):
        table_text = str(table.get("text") or "")
        if not table_text.strip():
            continue
        full_tables.append(
            {
                "idx": len(full_tables),
                "page": _old_table_page(old_tables, idx),
                "text": table_text,
                "original_text": str(table.get("original_text") or table_text),
            }
        )

    _attach_table_positions_and_context(
        normalized_text,
        full_tables,
        pre_lines=context_lines,
        post_lines=context_lines,
    )

    normalized_ocr = dict(ocr)
    normalized_ocr["text"] = normalized_text
    normalized_ocr["tables"] = [
        {
            "idx": table.get("idx"),
            "page": table.get("page"),
            "text": table.get("text", ""),
            "formatted_str": table.get("text", ""),
            "original_text": table.get("original_text", ""),
            "position_range": table.get("position_range"),
            "before_context": table.get("before_context", ""),
            "after_context": table.get("after_context", ""),
        }
        for table in full_tables
    ]
    normalized_ocr["full_text"] = normalized_text
    normalized_ocr["full_tables"] = full_tables
    normalized_ocr["normalization"] = {
        "name": "idp_markdown_table_normalized_v2",
        "source": path.name,
        "context_lines": context_lines,
        "original_text_chars": len(text),
        "normalized_text_chars": len(normalized_text),
        "original_tables": len(old_tables),
        "normalized_tables": len(full_tables),
    }

    out = dict(data)
    out["ocr_result"] = normalized_ocr
    out_path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    return {
        "path": str(path),
        "output": str(out_path),
        "skipped": False,
        "text_chars": len(text),
        "normalized_text_chars": len(normalized_text),
        "old_tables": len(old_tables),
        "new_tables": len(full_tables),
    }


def iter_inputs(paths: list[Path]) -> list[Path]:
    out: list[Path] = []
    for path in paths:
        if path.is_dir():
            out.extend(sorted(p for p in path.glob("*.json") if p.name not in DEFAULT_SKIP))
        elif path.is_file() and path.suffix.lower() == ".json":
            if path.name not in DEFAULT_SKIP:
                out.append(path)
        else:
            raise FileNotFoundError(path)
    return [path for path in out if not path.stem.lower().endswith("_new")]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", type=Path, nargs="*", default=list(DEFAULT_DIRS))
    parser.add_argument("--context-lines", type=int, default=5)
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args(argv)

    results = [convert_one(path, context_lines=args.context_lines, overwrite=args.overwrite) for path in iter_inputs(args.paths)]
    for result in results:
        status = "skip" if result.get("skipped") else "ok"
        print(
            f"{status}: {Path(str(result.get('path'))).name} -> "
            f"{Path(str(result.get('output', ''))).name if result.get('output') else result.get('reason', '')} "
            f"tables={result.get('new_tables', '')}/{result.get('old_tables', '')}"
        )
    print(f"converted={sum(1 for r in results if not r.get('skipped'))} total={len(results)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
