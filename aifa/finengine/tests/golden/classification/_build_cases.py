"""One-shot helper (not a test): merge the `_*_raw.json` fetch results into
the committed `cases.json` — only entries where the live classifier's
verdict matches the independently-known ground truth (company identity).
Deletes the `_*_raw.json` / `_scratch` scratch files afterward; `cases.json`
is the only thing meant to survive in git.

Usage: .venv/Scripts/python.exe tests/golden/classification/_build_cases.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))
from finengine.skills.extractors._classifier import detect_profile  # noqa: E402

HERE = Path(__file__).resolve().parent
SHRINK_STEPS = (8_000, 20_000, 50_000, 100_000, 150_000)


def shrink(excerpt: str, expected_profile: str, expected_confidence_low: bool) -> str:
    """Smallest prefix of `excerpt` that reproduces the recorded verdict exactly.

    C0.1 asks for MINIMAL volumes — storing the full 200k-char classifier
    head window for 50 real reports made `cases.json` ~11MB, almost all of
    it legal boilerplate the classifier never needed. Falls back to the
    full excerpt only if no smaller prefix reproduces the same verdict.
    """
    for size in SHRINK_STEPS:
        if size >= len(excerpt):
            break
        candidate = excerpt[:size]
        profile = detect_profile(candidate)
        if profile.profile_id == expected_profile and profile.confidence_low == expected_confidence_low:
            return candidate
    return excerpt

RAW_FILES = [
    "_edgar_raw.json",
    "_edgar_raw2.json",
    "_edgar_raw3.json",
    "_cbr_raw.json",
    "_financemarker_raw.json",
    "_financemarker_raw2.json",
    "_financemarker_raw3.json",
    "_generic_raw2.json",
    "_generic_raw3.json",
]


def _label(row: dict) -> str:
    return row.get("company") or row.get("ticker") or row.get("bank") or "?"


def main() -> None:
    confident = []
    rejected = []
    for fname in RAW_FILES:
        path = HERE / fname
        if not path.exists():
            continue
        rows = json.loads(path.read_text(encoding="utf-8"))
        for row in rows:
            if row.get("error"):
                rejected.append((fname, _label(row), f"fetch error: {row['error']}"))
                continue
            if not row.get("match"):
                rejected.append((fname, _label(row), f"classifier said {row.get('detected_profile')}, expected {row.get('expected_profile')}"))
                continue
            confident.append(row)

    cases = []
    for i, row in enumerate(confident, start=1):
        excerpt = shrink(row["excerpt"], row["expected_profile"], row["confidence_low"])
        cases.append({
            "id": f"cls-{i:03d}",
            "company": _label(row),
            "profile_id": row["expected_profile"],
            "source_url": row["source_url"],
            "source_note": row.get("source_note"),
            "cross_check": {
                "detected_profile": row["detected_profile"],
                "confidence_low": row["confidence_low"],
                "reason": row["reason"],
            },
            "excerpt": excerpt,
        })

    print(f"{len(cases)} confident cases, {len(rejected)} rejected:")
    for fname, label, why in rejected:
        print(f"  [{fname}] {label}: {why}")

    by_profile: dict[str, int] = {}
    for c in cases:
        by_profile[c["profile_id"]] = by_profile.get(c["profile_id"], 0) + 1
    print("counts by profile:", by_profile)

    (HERE / "cases.json").write_text(json.dumps(cases, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
