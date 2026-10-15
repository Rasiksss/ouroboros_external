"""Capture the current two-stage extraction prompts without calling a model.

This replaces the deleted legacy snapshot harness, which explicitly forced the
obsolete ``full_context`` engine.  The capture now follows the same
``extract_stream_from_text`` entry point as the station and intercepts the
actual ``complete_with_usage`` boundary used by the production two-stage
engine.

The batch controls are pinned to their production defaults so an environment
override cannot silently rewrite the golden set.  The fake model returns a
shape-valid empty answer; no credentials, network traffic, or model spend are
involved.

    .venv/Scripts/python benchmarks/snap_extract_prompt.py
    .venv/Scripts/python benchmarks/snap_extract_prompt.py --out DIR
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

DOC = (
    "===PAGE 1===\n"
    "Consolidated statement of financial position\n"
    "as at 31 December 2024 (RUB million)\n"
    "| Metric | 2024 | 2023 |\n"
    "| Total assets | 1 000 | 900 |\n"
    "| Total equity | 400 | 350 |\n\n"
    "===PAGE 2===\n"
    "Statement of profit or loss for the year ended 31 December 2024\n"
    "| Metric | 2024 | 2023 |\n"
    "| Revenue | 500 | 450 |\n"
    "| Net income | 50 | 40 |\n"
)

CELLS = [
    ("ifrs_nf_ru", "non_financial"),
    ("ifrs_nf_en", "non_financial"),
    ("us_gaap_nf_en", "non_financial"),
    ("ind_as_nf_en", "non_financial"),
    ("ifrs_bank_ru", "bank"),
    ("ifrs_bank_en", "bank"),
    ("us_gaap_bank_en", "bank"),
    ("ind_gaap_bank_en", "bank"),
]

_ENUM_RE = re.compile(r'^\s*"enum":\s*"([A-Z0-9_]+)"', re.MULTILINE)
_SETTINGS = {
    "extract_batch_jobs": 1,
    "extract_batch_size": 10,
    "extract_batch_attempts": 1,
    "extract_batch_strategy": "balanced",
    "extract_ask_without_evidence": True,
    "extract_segment_batch_size": 5,
}


def _stage2_answer(prompt: str) -> str:
    """Return every requested enum once so the real validator accepts it."""
    enums = list(dict.fromkeys(_ENUM_RE.findall(prompt)))
    if not enums:
        raise AssertionError("two-stage extraction prompt contains no metric enums")
    return json.dumps(
        {enum: {"values": {}, "unit_text": "", "page": None} for enum in enums},
        ensure_ascii=False,
    )


async def _capture(profile_id: str, company_type: str) -> list[tuple[str, str]]:
    """Capture ordered model requests from the production two-stage path."""
    from finengine.config import settings
    from finengine.llm import openrouter as llm
    from finengine.skills import extract_text as et
    from finengine.skills import two_stage_extractor as ts
    from finengine.skills.metric_schema import registry_for_profile

    seen: list[tuple[str, str]] = []
    extraction_no = 0

    async def _fake_complete_with_usage(
        prompt: str = "", system: str | None = None, **_kwargs
    ) -> tuple[str, dict]:
        nonlocal extraction_no
        if prompt.startswith("## Task\nExtract reported financial metric values"):
            extraction_no += 1
            stage = f"main-extract-{extraction_no:03d}"
            response = _stage2_answer(prompt)
        elif "compressed evidence" in prompt.lower() or "context compression" in prompt.lower():
            stage = "main-compression"
            response = "{}"
        else:
            raise AssertionError("unexpected model call outside the main two-stage chain")
        seen.append((stage, f"## SYSTEM\n{system or ''}\n\n## USER\n{prompt}"))
        return response, {"prompt_tokens": 0, "completion_tokens": 0}

    original_complete = llm.complete_with_usage
    original_settings = {name: getattr(settings, name) for name in _SETTINGS}
    original_cached = ts._cache.compression_cached
    original_remember = ts._cache.remember_compression
    llm.complete_with_usage = _fake_complete_with_usage
    ts._cache.compression_cached = lambda *_args, **_kwargs: None
    ts._cache.remember_compression = lambda *_args, **_kwargs: None
    for name, value in _SETTINGS.items():
        setattr(settings, name, value)
    try:
        async for _ in et.extract_stream_from_text(
            DOC,
            company_type=company_type,
            role="other",
            include_segments=False,
            include_consolidated_group=False,
            registry_override=registry_for_profile(profile_id),
        ):
            pass
    finally:
        llm.complete_with_usage = original_complete
        ts._cache.compression_cached = original_cached
        ts._cache.remember_compression = original_remember
        for name, value in original_settings.items():
            setattr(settings, name, value)

    if not seen or seen[0][0] != "main-compression" or extraction_no == 0:
        raise AssertionError(f"{profile_id}: incomplete two-stage capture: {[stage for stage, _ in seen]}")
    return seen


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=REPO / "tests" / "golden" / "extract")
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)

    expected: set[Path] = set()
    total = 0
    for profile_id, company_type in CELLS:
        prompts = asyncio.run(_capture(profile_id, company_type))
        for stage, prompt in prompts:
            path = args.out / f"{profile_id}.{stage}.txt"
            path.write_text(prompt, encoding="utf-8")
            expected.add(path.resolve())
            total += 1
        print(f"  {profile_id}: calls {len(prompts)}, chars {sum(len(p) for _, p in prompts)}")

    for stale in args.out.glob("*.txt"):
        if stale.resolve() not in expected:
            stale.unlink()
    print(f"\ncaptured files: {total} -> {args.out}")


if __name__ == "__main__":
    main()
