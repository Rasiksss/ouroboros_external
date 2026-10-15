"""One-shot helper (not a test): fetch a real URL (PDF or HTML), extract
text, and cross-check against the live classifier. Generic version of
`_fetch_edgar.py`/`_fetch_financemarker.py` for one-off international
sources that don't share a predictable URL pattern.

Usage: .venv/Scripts/python.exe tests/golden/classification/_fetch_generic.py
"""
from __future__ import annotations

import json
import re
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))

import fitz  # PyMuPDF  # noqa: E402

from finengine.skills.extractors._classifier import detect_profile  # noqa: E402

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
HERE = Path(__file__).resolve().parent
HEAD_WINDOW = 200_000

MANIFEST = [
    ("Nestle", "https://www.nestle.com/sites/default/files/2025-02/2024-financial-statements-en.pdf", "ifrs_nf_en"),
    ("Anglo American", "https://www.angloamerican.com/~/media/Files/A/Anglo-American-Group-v9/PLC/investors/annual-reporting/2024/aa-annual-report-full-2024.pdf", "ifrs_nf_en"),
    ("HSBC", "https://www.hsbc.com/-/files/hsbc/investors/hsbc-results/2024/annual/pdfs/hsbc-holdings-plc/250227-financial-statements-2024.pdf", "ifrs_bank_en"),
    ("Standard Chartered", "https://www.sc.com/en/uploads/sites/66/content/docs/annual-report-2024-financial-statements.pdf", "ifrs_bank_en"),
    ("Barclays", "https://home.barclays/content/dam/home-barclays/documents/investor-relations/reports-and-events/annual-reports/2024/Barclays-PLC-Annual-Report-2024.pdf", "ifrs_bank_en"),
    ("Reliance Industries", "https://www.ril.com/ar2024-25/pdf/Consolidated-Financial-Statements.pdf", "ind_as_nf_en"),
    ("Axis Bank", "https://www.axis.bank.in/annual-reports/2024-2025/pdf/Financial%20statements.pdf", "ind_gaap_bank_en"),
    ("Deutsche Bank", "https://investor-relations.db.com/files/documents/annual-reports/2024/Annual-Financial-Statements-of-Deutsche-Bank-AG-2024.pdf", "ifrs_bank_en"),
    ("Kotak Mahindra Bank", "https://www.kotak.bank.in/content/dam/Kotak/investor-relation/Financial-Result/Annual-Reports/FY-2024/kotak-mahindra-bank/Kotak-Consolidated-Financial-Statements_FY%202023-24.pdf", "ind_gaap_bank_en"),
    ("LTIMindtree", "https://www.ltimindtree.com/annual-report-html/pdf/financial-statements.pdf", "ind_as_nf_en"),
]


def _get(url: str) -> bytes:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": UA,
            "Accept": "application/pdf,text/html,application/xhtml+xml,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read()


def strip_tags(html: bytes) -> str:
    text = html.decode("utf-8", errors="replace")
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", text, flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"&nbsp;|&#160;", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text


def extract(raw: bytes) -> str:
    if raw[:4] == b"%PDF":
        scratch = HERE / "_scratch"
        scratch.mkdir(exist_ok=True)
        tmp = scratch / "_generic_tmp.pdf"
        tmp.write_bytes(raw)
        doc = fitz.open(str(tmp))
        text = "\n".join(page.get_text("text") for page in doc)
        doc.close()
        tmp.unlink(missing_ok=True)
        return text
    return strip_tags(raw)


def main() -> None:
    out = []
    for name, url, expected_profile in MANIFEST:
        try:
            raw = _get(url)
            text = extract(raw)
            excerpt = text[:HEAD_WINDOW]
            profile = detect_profile(excerpt)
            match = profile.profile_id == expected_profile
            print(
                f"{name:22s} -> {profile.profile_id:16s} (expected {expected_profile}) "
                f"confidence_low={profile.confidence_low} match={match} chars={len(excerpt)}"
            )
            out.append({
                "company": name,
                "expected_profile": expected_profile,
                "detected_profile": profile.profile_id,
                "confidence_low": profile.confidence_low,
                "match": match,
                "reason": profile.reason,
                "source_url": url,
                "excerpt": excerpt,
            })
        except Exception as exc:  # noqa: BLE001
            print(f"{name:22s} -> ERROR {exc}")
            out.append({"company": name, "expected_profile": expected_profile, "source_url": url, "error": str(exc)})
        time.sleep(0.3)
    (HERE / "_generic_raw3.json").write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
